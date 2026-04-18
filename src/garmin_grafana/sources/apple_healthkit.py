"""
Apple Health one-shot importer.

Apple HealthKit has no server-side API, so the supported flow is:

1. On iPhone: Health app -> Profile (top-right avatar) -> Export All Health Data
2. AirDrop / share / rsync the resulting ``export.zip`` to the server.
3. Run this module, pointing it at the zip (or the extracted ``export.xml``).

The importer streams the XML with ``iterparse`` (the real export can exceed
200 MB and holds years of data), aggregates samples to per-day summaries,
and writes both raw ``AppleHealth*`` measurements (source of truth) and
the normalized ``Unified*`` points (for cross-source overlays on the
Multi-Source dashboard) via the same ``garmin_fetch.write_points_to_influxdb``
helper the Oura fetcher uses.

Re-running against a fresh export is safe: InfluxDB overwrites points with
the same measurement + tags + timestamp, so a monthly export pulls in
the new days without duplicating old ones.

Example invocations::

    # Inside the fetcher container, pointing at a file on a mounted volume:
    docker compose exec health-fetch-data python -m \
        garmin_grafana.sources.apple_healthkit /data/export.zip

    # With a date filter (useful on initial import to bound the run):
    python -m garmin_grafana.sources.apple_healthkit export.zip \
        --from 2026-01-01 --to 2026-04-15

    # Parse + summarize without writing (CI smoke, offline inspection):
    python -m garmin_grafana.sources.apple_healthkit export.zip --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from . import unified_schema

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type + unit helpers
# ---------------------------------------------------------------------------

# Apple emits datetimes like "2026-04-15 10:30:00 -0700".
_APPLE_DT_FMT = "%Y-%m-%d %H:%M:%S %z"

# Unit conversions to the project-wide canonical units (meters, seconds,
# ml/kg/min, kcal). Apple exports in whatever the phone happens to use
# based on locale, so unit attribute inspection is required — you cannot
# assume the iPhone always exports km.
_DISTANCE_UNIT_M = {
    "m": 1.0,
    "km": 1000.0,
    "mi": 1609.344,
    "ft": 0.3048,
}

_TIME_UNIT_S = {
    "s": 1.0,
    "min": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
}

_ENERGY_UNIT_KCAL = {
    "kcal": 1.0,
    "cal": 0.001,
    "kJ": 0.239005736,
    "J": 0.000239005736,
}

# Sleep stage values as written by iOS. Pre-iOS-16 devices emit only
# "AsleepUnspecified"; iOS 16+ watches write the three-stage breakdown.
_SLEEP_STAGE_DEEP = {"HKCategoryValueSleepAnalysisAsleepDeep"}
_SLEEP_STAGE_LIGHT = {
    "HKCategoryValueSleepAnalysisAsleepCore",
    "HKCategoryValueSleepAnalysisAsleep",  # legacy alias
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
}
_SLEEP_STAGE_REM = {"HKCategoryValueSleepAnalysisAsleepREM"}
_SLEEP_STAGE_AWAKE = {"HKCategoryValueSleepAnalysisAwake"}
_SLEEP_STAGE_INBED = {"HKCategoryValueSleepAnalysisInBed"}


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, _APPLE_DT_FMT)
    except ValueError:
        return None


def _convert(value: float, unit: str | None, table: dict[str, float]) -> float | None:
    if unit is None:
        return None
    factor = table.get(unit)
    if factor is None:
        return None
    return value * factor


def _strip_prefix(s: str, prefix: str) -> str:
    return s[len(prefix):] if s.startswith(prefix) else s


def _friendly_workout_type(raw: str | None) -> str | None:
    """Turn ``HKWorkoutActivityTypeRunning`` into ``running``."""
    if not raw:
        return None
    return _strip_prefix(raw, "HKWorkoutActivityType").lower() or None


# ---------------------------------------------------------------------------
# Aggregator: accumulates per-day summaries while the XML is streamed
# ---------------------------------------------------------------------------


def _new_activity_bucket() -> dict[str, float]:
    return {
        "steps": 0.0,
        "calories_active": 0.0,
        "calories_basal": 0.0,
        "distance_m": 0.0,
        "active_minutes": 0.0,
    }


def _new_sleep_bucket() -> dict[str, float]:
    return {
        "deep_s": 0.0,
        "light_s": 0.0,
        "rem_s": 0.0,
        "awake_s": 0.0,
        "in_bed_s": 0.0,
        "_hrv_sum": 0.0,
        "_hrv_count": 0,
    }


def _new_hr_bucket() -> dict[str, float]:
    return {"_sum": 0.0, "_count": 0, "hr_max": 0.0, "hr_min": 1e9}


class Aggregator:
    """Per-(source, day) summaries across every sourceName seen in the export.

    Apple Health consolidates samples from multiple devices (Apple Watch,
    iPhone, third-party apps) and dedupes them at display time via a
    per-type priority list that **is not preserved in the export XML**.
    A naive sum across sources double-counts (Apple Watch + iPhone both
    logging the same run's steps is common). So:

    - The aggregator tracks every ``(sourceName, local_date)`` tuple
      independently — no cross-source merging happens at parse time.
    - ``build_points`` picks the highest-priority source per day/metric
      using the chain below, so each day yields exactly one
      ``Unified*`` point per metric.
    - Raw ``AppleHealth*`` points are emitted **per-source** (Device tag
      = sourceName), so anyone auditing can see the per-device totals
      even after the priority pick discarded them.

    Priority chain (first match wins; within a tier, sort alphabetically):

      1. exact match to the ``--prefer-source`` CLI value
         (default ``"Apple Watch"`` — matches "Apple Watch", "Brett's
         Apple Watch", etc. via substring check in
         ``_priority_tier``)
      2. any source name containing ``"Apple Watch"``
      3. any source name containing ``"iPhone"`` or ``"iPad"``
      4. everything else (typically third-party apps)
    """

    def __init__(self) -> None:
        # (source, date_str) -> fields
        self.activity: dict[tuple[str, str], dict[str, float]] = defaultdict(
            _new_activity_bucket
        )
        self.sleep: dict[tuple[str, str], dict[str, float]] = defaultdict(
            _new_sleep_bucket
        )
        self.hr_daily: dict[tuple[str, str], dict[str, float]] = defaultdict(
            _new_hr_bucket
        )
        self.rhr: dict[tuple[str, str], float] = {}
        self.vo2_max: dict[tuple[str, str], float] = {}
        # Intraday HR is (intentionally) not split by source — the only
        # realistic source is the Watch anyway, and writing the same
        # sample twice under different Device tags bloats InfluxDB.
        # Each sample carries its source only on the raw emit path.
        self.hr_samples: list[tuple[datetime, float, str]] = []
        self.workouts: list[dict[str, Any]] = []
        self.records_seen = 0
        self.records_kept = 0

    # -- activity ----------------------------------------------------------
    def add_steps(self, source: str, day: str, value: float) -> None:
        self.activity[(source, day)]["steps"] += value

    def add_active_energy(self, source: str, day: str, kcal: float) -> None:
        self.activity[(source, day)]["calories_active"] += kcal

    def add_basal_energy(self, source: str, day: str, kcal: float) -> None:
        self.activity[(source, day)]["calories_basal"] += kcal

    def add_distance(self, source: str, day: str, m: float) -> None:
        self.activity[(source, day)]["distance_m"] += m

    def add_exercise_minutes(self, source: str, day: str, minutes: float) -> None:
        self.activity[(source, day)]["active_minutes"] += minutes

    # -- heart rate --------------------------------------------------------
    def add_hr_sample(self, source: str, ts: datetime, bpm: float) -> None:
        self.hr_samples.append((ts, bpm, source))
        day = ts.date().isoformat()
        bucket = self.hr_daily[(source, day)]
        bucket["_sum"] += bpm
        bucket["_count"] += 1
        if bpm > bucket["hr_max"]:
            bucket["hr_max"] = bpm
        if bpm < bucket["hr_min"]:
            bucket["hr_min"] = bpm

    def set_rhr(self, source: str, day: str, bpm: float) -> None:
        # Apple emits at most one RHR per (source, day); the last wins
        # if a source logs multiple (unlikely but benign).
        self.rhr[(source, day)] = bpm

    def add_hrv(self, source: str, day: str, ms: float) -> None:
        bucket = self.sleep[(source, day)]
        bucket["_hrv_sum"] += ms
        bucket["_hrv_count"] += 1

    def set_vo2_max(self, source: str, day: str, value: float) -> None:
        self.vo2_max[(source, day)] = value

    # -- sleep -------------------------------------------------------------
    def add_sleep_stage(self, source: str, day: str, stage_value: str, seconds: float) -> None:
        bucket = self.sleep[(source, day)]
        if stage_value in _SLEEP_STAGE_DEEP:
            bucket["deep_s"] += seconds
        elif stage_value in _SLEEP_STAGE_LIGHT:
            bucket["light_s"] += seconds
        elif stage_value in _SLEEP_STAGE_REM:
            bucket["rem_s"] += seconds
        elif stage_value in _SLEEP_STAGE_AWAKE:
            bucket["awake_s"] += seconds
        elif stage_value in _SLEEP_STAGE_INBED:
            bucket["in_bed_s"] += seconds
        # Unknown / future stage values: ignore silently — better than
        # forcing the pipeline to fail on a new iOS release.

    # -- workouts ----------------------------------------------------------
    def add_workout(self, workout: dict[str, Any]) -> None:
        self.workouts.append(workout)

    # ---------------------------------------------------------------------
    # Per-(source, day) summaries — these build the raw AppleHealth* points
    # ---------------------------------------------------------------------
    def summarize_activity(self, source: str, day: str) -> dict[str, float] | None:
        raw = self.activity.get((source, day))
        if not raw:
            return None
        total = raw["calories_active"] + raw["calories_basal"]
        out: dict[str, float] = {}
        if raw["steps"]:
            out["steps"] = raw["steps"]
        if raw["calories_active"]:
            out["calories_active"] = raw["calories_active"]
        if total:
            out["calories_total"] = total
        if raw["distance_m"]:
            out["distance_m"] = raw["distance_m"]
        if raw["active_minutes"]:
            out["active_minutes"] = raw["active_minutes"]
        return out or None

    def summarize_sleep(self, source: str, day: str) -> dict[str, float] | None:
        raw = self.sleep.get((source, day))
        if not raw:
            return None
        total_asleep = raw["deep_s"] + raw["light_s"] + raw["rem_s"]
        if not total_asleep and not raw["awake_s"] and not raw["in_bed_s"]:
            return None
        out: dict[str, float] = {}
        if total_asleep:
            out["total_s"] = total_asleep
        if raw["deep_s"]:
            out["deep_s"] = raw["deep_s"]
        if raw["light_s"]:
            out["light_s"] = raw["light_s"]
        if raw["rem_s"]:
            out["rem_s"] = raw["rem_s"]
        if raw["awake_s"]:
            out["awake_s"] = raw["awake_s"]
        if raw["in_bed_s"]:
            out["in_bed_s"] = raw["in_bed_s"]
        denom = raw["in_bed_s"] or (total_asleep + raw["awake_s"])
        if denom and total_asleep:
            out["efficiency"] = round(100.0 * total_asleep / denom, 2)
        if raw["_hrv_count"]:
            out["hrv_avg"] = round(raw["_hrv_sum"] / raw["_hrv_count"], 2)
        return out

    def summarize_heart_rate(self, source: str, day: str) -> dict[str, float] | None:
        raw = self.hr_daily.get((source, day))
        out: dict[str, float] = {}
        if raw and raw["_count"]:
            out["hr_avg"] = round(raw["_sum"] / raw["_count"], 2)
            out["hr_max"] = raw["hr_max"]
            out["hr_min"] = raw["hr_min"]
        rhr = self.rhr.get((source, day))
        if rhr is not None:
            out["rhr"] = rhr
        return out or None

    def all_sources(self) -> set[str]:
        s: set[str] = set()
        for d in (self.activity, self.sleep, self.hr_daily, self.rhr, self.vo2_max):
            s.update(k[0] for k in d.keys())
        s.update(hr[2] for hr in self.hr_samples)
        return s

    def all_days(self) -> set[str]:
        d: set[str] = set()
        for container in (self.activity, self.sleep, self.hr_daily, self.rhr, self.vo2_max):
            d.update(k[1] for k in container.keys())
        return d


# ---------------------------------------------------------------------------
# Priority-based pick for Unified emission
# ---------------------------------------------------------------------------


def _priority_tier(source: str, preferred: str) -> int:
    """Lower is better. Ties broken alphabetically by the caller."""
    if source == preferred:
        return 0
    # Substring match on "Apple Watch" handles "Brett's Apple Watch",
    # "Apple Watch Series 8", etc. without a hardcoded exact-match list.
    if preferred.lower() in source.lower():
        return 0
    if "Apple Watch" in source:
        return 1
    if "iPhone" in source or "iPad" in source:
        return 2
    return 3


def _pick_source(sources: Iterable[str], preferred: str) -> str | None:
    """Return the highest-priority source name, or None if the iterable is empty."""
    candidates = list(sources)
    if not candidates:
        return None
    candidates.sort(key=lambda s: (_priority_tier(s, preferred), s))
    return candidates[0]


# ---------------------------------------------------------------------------
# XML streaming
# ---------------------------------------------------------------------------


def _open_xml_stream(path: Path) -> Any:
    """Return a file-like object yielding the contents of ``export.xml``.

    Accepts either a direct ``.xml`` or a ``.zip`` containing one. The zip
    that Apple ships usually stores the XML at ``apple_health_export/export.xml``
    but historically it has lived at the zip root — scan for whatever
    ends with ``export.xml`` rather than hardcoding.
    """
    if path.suffix == ".zip":
        z = zipfile.ZipFile(path)
        candidate = next(
            (n for n in z.namelist() if n.endswith("/export.xml") or n == "export.xml"),
            None,
        )
        if not candidate:
            raise FileNotFoundError(
                f"{path} does not contain an export.xml — got {z.namelist()[:3]}..."
            )
        _log.info("reading %s from %s", candidate, path)
        return z.open(candidate)
    _log.info("reading %s", path)
    return open(path, "rb")


def _local_day(ts: datetime) -> str:
    """Local calendar date as YYYY-MM-DD (matches Apple Health's bucketing)."""
    return ts.date().isoformat()


def _sleep_attribution_day(start: datetime, end: datetime) -> str:
    """The 'date' a sleep stage belongs to.

    Apple Health shows sleep on the morning-of date — a session running
    11pm -> 7am is reported on the 7am day. We mimic that by using the
    end's local date. For daytime naps the end is in the same local day
    as the start, so this doesn't misattribute them.
    """
    return _local_day(end)


def parse_export(
    path: Path,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Aggregator:
    agg = Aggregator()
    stream = _open_xml_stream(path)
    try:
        # Only listen to `end` events — each <Record>/<Workout> is
        # flushed when its closing tag is seen. Clear only the top-level
        # tags we process: clearing child elements like WorkoutStatistics
        # on their own `end` event would wipe the attribs our Workout
        # handler needs when the parent's `end` fires next.
        for _event, elem in ET.iterparse(stream, events=("end",)):
            if elem.tag == "Record":
                agg.records_seen += 1
                _handle_record(elem, agg, date_from, date_to)
                elem.clear()
            elif elem.tag == "Workout":
                _handle_workout(elem, agg, date_from, date_to)
                elem.clear()
    finally:
        stream.close()
    return agg


def _in_range(d: date, lo: date | None, hi: date | None) -> bool:
    if lo and d < lo:
        return False
    if hi and d > hi:
        return False
    return True


def _handle_record(
    elem: ET.Element,
    agg: Aggregator,
    date_from: date | None,
    date_to: date | None,
) -> None:
    type_ = elem.get("type")
    if not type_:
        return
    start = _parse_dt(elem.get("startDate"))
    end = _parse_dt(elem.get("endDate"))
    if start is None:
        return
    if not _in_range(start.date(), date_from, date_to):
        return
    agg.records_kept += 1

    day = _local_day(start)
    raw_value = elem.get("value")
    unit = elem.get("unit")
    source = elem.get("sourceName") or "Unknown"

    # Quantity-typed records carry a numeric value in `value`.
    # Category records (sleep) carry a stage string in `value`.
    if type_ == "HKQuantityTypeIdentifierStepCount":
        if raw_value:
            agg.add_steps(source, day, float(raw_value))
    elif type_ == "HKQuantityTypeIdentifierActiveEnergyBurned":
        if raw_value and unit:
            kcal = _convert(float(raw_value), unit, _ENERGY_UNIT_KCAL)
            if kcal is not None:
                agg.add_active_energy(source, day, kcal)
    elif type_ == "HKQuantityTypeIdentifierBasalEnergyBurned":
        if raw_value and unit:
            kcal = _convert(float(raw_value), unit, _ENERGY_UNIT_KCAL)
            if kcal is not None:
                agg.add_basal_energy(source, day, kcal)
    elif type_ == "HKQuantityTypeIdentifierDistanceWalkingRunning":
        if raw_value and unit:
            m = _convert(float(raw_value), unit, _DISTANCE_UNIT_M)
            if m is not None:
                agg.add_distance(source, day, m)
    elif type_ == "HKQuantityTypeIdentifierAppleExerciseTime":
        if raw_value and unit:
            s = _convert(float(raw_value), unit, _TIME_UNIT_S)
            if s is not None:
                agg.add_exercise_minutes(source, day, s / 60.0)
    elif type_ == "HKQuantityTypeIdentifierHeartRate":
        if raw_value:
            agg.add_hr_sample(source, start, float(raw_value))
    elif type_ == "HKQuantityTypeIdentifierRestingHeartRate":
        if raw_value:
            agg.set_rhr(source, day, float(raw_value))
    elif type_ == "HKQuantityTypeIdentifierHeartRateVariabilitySDNN":
        # Unit is typically "ms". HRV is usually recorded during the
        # night, so attribute it to the end-of-sleep day for alignment
        # with the sleep record.
        if raw_value and end is not None:
            target_day = _sleep_attribution_day(start, end)
            agg.add_hrv(source, target_day, float(raw_value))
    elif type_ == "HKQuantityTypeIdentifierVO2Max":
        if raw_value:
            agg.set_vo2_max(source, day, float(raw_value))
    elif type_ == "HKCategoryTypeIdentifierSleepAnalysis":
        if raw_value and end is not None:
            target_day = _sleep_attribution_day(start, end)
            seconds = (end - start).total_seconds()
            if seconds > 0:
                agg.add_sleep_stage(source, target_day, raw_value, seconds)
    # All other record types are silently ignored — the MVP intentionally
    # covers the set already reflected in the existing dashboards.


def _handle_workout(
    elem: ET.Element,
    agg: Aggregator,
    date_from: date | None,
    date_to: date | None,
) -> None:
    start = _parse_dt(elem.get("startDate"))
    end = _parse_dt(elem.get("endDate"))
    if start is None:
        return
    if not _in_range(start.date(), date_from, date_to):
        return
    agg.records_kept += 1

    duration_s: int | None = None
    if end is not None:
        duration_s = int((end - start).total_seconds())
    elif elem.get("duration") and elem.get("durationUnit"):
        s = _convert(
            float(elem.get("duration")),  # type: ignore[arg-type]
            elem.get("durationUnit"),
            _TIME_UNIT_S,
        )
        duration_s = int(s) if s is not None else None

    calories: float | None = None
    tec = elem.get("totalEnergyBurned")
    teu = elem.get("totalEnergyBurnedUnit")
    if tec and teu:
        calories = _convert(float(tec), teu, _ENERGY_UNIT_KCAL)

    distance_m: float | None = None
    td = elem.get("totalDistance")
    tdu = elem.get("totalDistanceUnit")
    if td and tdu:
        distance_m = _convert(float(td), tdu, _DISTANCE_UNIT_M)

    hr_avg: float | None = None
    hr_max: float | None = None
    # iOS 13+ writes <WorkoutStatistics> children with HR aggregates.
    for child in elem.findall("WorkoutStatistics"):
        if child.get("type") == "HKQuantityTypeIdentifierHeartRate":
            avg = child.get("average")
            mx = child.get("maximum")
            if avg:
                hr_avg = float(avg)
            if mx:
                hr_max = float(mx)
            break

    agg.add_workout(
        {
            "start": start.astimezone().isoformat(),
            "activity_type": _friendly_workout_type(elem.get("workoutActivityType")),
            "duration_s": duration_s,
            "calories": calories,
            "distance_m": distance_m,
            "hr_avg": hr_avg,
            "hr_max": hr_max,
            "source": elem.get("sourceName"),
        }
    )


# ---------------------------------------------------------------------------
# Build output points (raw AppleHealth* + unified)
# ---------------------------------------------------------------------------


def _tags(device: str, db: str) -> dict[str, str]:
    return {
        "Source": unified_schema.SOURCE_APPLE,
        "Device": device,
        "Database_Name": db,
    }


def _utc_noon(date_str: str) -> str:
    return f"{date_str}T12:00:00+00:00"


def _utc_midnight(date_str: str) -> str:
    return f"{date_str}T00:00:00+00:00"


def build_points(
    agg: Aggregator,
    *,
    device: str,
    database_name: str,
    preferred_source: str = "Apple Watch",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Return ``(unified_points, raw_points)``.

    Raw ``AppleHealth*`` points are emitted **per-source** with the
    sourceName stamped into the Device tag, so per-device audits remain
    possible. Unified points are emitted **once per day**, picking the
    highest-priority source per metric via ``_priority_tier``. The
    Unified device tag is the operator-supplied ``device`` value (default
    "iOS") so the Multi-Source dashboard isn't fragmented by one series
    per device.
    """
    unified: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []

    # ---- Raw, per-source rollups (audit trail) -----------------------------
    # Iterate every (source, day) bucket that has data and emit one raw
    # point per combination. The Apple dashboard filters by a $device
    # template variable (default = the preferred source) so these don't
    # visually double up, but they're all present for ad-hoc queries.
    raw_activity_keys = set(agg.activity.keys())
    for source, day in sorted(raw_activity_keys):
        activity = agg.summarize_activity(source, day)
        if activity:
            raw.append(
                {
                    "measurement": "AppleHealthActivity",
                    "time": _utc_noon(day),
                    "tags": _tags(source, database_name),
                    "fields": {k: float(v) for k, v in activity.items()},
                }
            )
    raw_sleep_keys = set(agg.sleep.keys())
    for source, day in sorted(raw_sleep_keys):
        sleep = agg.summarize_sleep(source, day)
        if sleep:
            raw.append(
                {
                    "measurement": "AppleHealthSleep",
                    "time": _utc_noon(day),
                    "tags": _tags(source, database_name),
                    "fields": {k: float(v) for k, v in sleep.items()},
                }
            )
    raw_hr_keys = set(agg.hr_daily.keys()) | set(agg.rhr.keys())
    for source, day in sorted(raw_hr_keys):
        hr = agg.summarize_heart_rate(source, day)
        if hr:
            raw.append(
                {
                    "measurement": "AppleHealthHeartRate",
                    "time": _utc_noon(day),
                    "tags": _tags(source, database_name),
                    "fields": {k: float(v) for k, v in hr.items()},
                }
            )
    for (source, day), v in sorted(agg.vo2_max.items()):
        raw.append(
            {
                "measurement": "AppleHealthVO2Max",
                "time": _utc_midnight(day),
                "tags": _tags(source, database_name),
                "fields": {"vo2_max": float(v)},
            }
        )

    def _pick_for_day(container, day: str) -> str | None:
        sources = [s for (s, d) in container.keys() if d == day]
        return _pick_source(sources, preferred_source)

    # ---- Unified: one per day, highest-priority source wins ---------------
    for day in sorted(agg.all_days()):
        # Activity / sleep / heart_rate / vo2_max each resolve
        # independently — a day could plausibly have sleep from the watch
        # and steps from the phone; the picker runs per metric.
        activity_source = _pick_for_day(agg.activity, day)
        sleep_source = _pick_for_day(agg.sleep, day)
        hr_source = _pick_for_day(agg.hr_daily, day) or _pick_for_day(agg.rhr, day)
        vo2_source = _pick_for_day(agg.vo2_max, day)

        activity = agg.summarize_activity(activity_source, day) if activity_source else None
        sleep = agg.summarize_sleep(sleep_source, day) if sleep_source else None
        hr_value: dict[str, float] | None = None
        if hr_source:
            hr_value = {}
            hr_stats = agg.summarize_heart_rate(hr_source, day)
            if hr_stats:
                hr_value.update(hr_stats)
            # RHR can live on a different source (e.g. iPhone stores it
            # for days the user didn't wear the watch). Pull it in
            # independently if the picked HR source lacks one.
            if "rhr" not in hr_value:
                rhr_source = _pick_for_day(agg.rhr, day)
                if rhr_source:
                    hr_value["rhr"] = agg.rhr[(rhr_source, day)]
            if not hr_value:
                hr_value = None
        vo2 = agg.vo2_max.get((vo2_source, day)) if vo2_source else None

        unified.extend(
            unified_schema.apple_to_unified(
                date_str=day,
                device_name=device,
                database_name=database_name,
                sleep=sleep,
                activity=activity,
                heart_rate=hr_value,
                vo2_max=vo2,
            )
        )

    # ---- Workouts: one entry per workout (source embedded in Device tag) ---
    for w in agg.workouts:
        source = w.get("source") or device
        wp = unified_schema.unified_workout_point(
            source=unified_schema.SOURCE_APPLE,
            device=device,
            database_name=database_name,
            time=w["start"],
            activity_type=w.get("activity_type"),
            duration_s=w.get("duration_s"),
            calories=w.get("calories"),
            distance_m=w.get("distance_m"),
            hr_avg=w.get("hr_avg"),
            hr_max=w.get("hr_max"),
        )
        if wp is not None:
            unified.append(wp)
        raw_fields = {
            k: float(v)
            for k, v in (
                ("duration_s", w.get("duration_s")),
                ("calories", w.get("calories")),
                ("distance_m", w.get("distance_m")),
                ("hr_avg", w.get("hr_avg")),
                ("hr_max", w.get("hr_max")),
            )
            if v is not None
        }
        if raw_fields:
            raw_tags = _tags(source, database_name)
            if w.get("activity_type"):
                raw_tags["Activity"] = w["activity_type"]
            raw.append(
                {
                    "measurement": "AppleHealthWorkout",
                    "time": w["start"],
                    "tags": raw_tags,
                    "fields": raw_fields,
                }
            )

    # ---- Intraday HR: only the preferred source feeds UnifiedHRIntraday ---
    # This avoids double-sampling if the user has an iPhone logging HR
    # from a paired heart strap *and* a watch. If no sample from the
    # preferred source exists for a given minute, the other sources'
    # samples for that minute are dropped — acceptable loss for de-dup.
    preferred_samples = [
        (ts, hr) for ts, hr, src in agg.hr_samples
        if _priority_tier(src, preferred_source) == 0
    ]
    # Fallback: if the preferred source produced nothing (e.g. user has
    # no Apple Watch and only Withings chest strap), use everything.
    if not preferred_samples:
        preferred_samples = [(ts, hr) for ts, hr, _src in agg.hr_samples]
    unified.extend(
        unified_schema.unified_hr_intraday_points(
            source=unified_schema.SOURCE_APPLE,
            device=device,
            database_name=database_name,
            samples=preferred_samples,
        )
    )
    return unified, raw


# ---------------------------------------------------------------------------
# InfluxDB write + CLI
# ---------------------------------------------------------------------------


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _chunked(points: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    for i in range(0, len(points), size):
        yield points[i : i + size]


def _summarize(points: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for p in points:
        out[p["measurement"]] += 1
    return dict(sorted(out.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import an Apple Health export (.zip or export.xml) into InfluxDB.",
    )
    parser.add_argument("path", help="Path to export.zip or export.xml")
    parser.add_argument(
        "--from",
        dest="date_from",
        type=_parse_date,
        default=None,
        help="Only import records with startDate on or after this day (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        type=_parse_date,
        default=None,
        help="Only import records with startDate on or before this day (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--device-name",
        default="iOS",
        help="Device tag stamped onto the Unified* points (default 'iOS'). "
        "Raw AppleHealth* points retain per-source Device tags regardless.",
    )
    parser.add_argument(
        "--prefer-source",
        default="Apple Watch",
        help="Preferred sourceName when multiple devices write the same "
        "day/metric. Substring match: 'Apple Watch' picks any source with "
        "'Apple Watch' in its name. Default: 'Apple Watch'.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and summarize, but do not write to InfluxDB",
    )
    parser.add_argument(
        "--database-name",
        default=os.getenv("INFLUXDB_DATABASE", "GarminStats"),
        help="InfluxDB database/bucket tag (defaults to $INFLUXDB_DATABASE)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10_000,
        help="Points per InfluxDB write batch",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    src = Path(args.path).expanduser().resolve()
    if not src.exists():
        _log.error("file not found: %s", src)
        return 2

    _log.info(
        "parsing %s (from=%s, to=%s)", src, args.date_from, args.date_to
    )
    agg = parse_export(src, date_from=args.date_from, date_to=args.date_to)
    _log.info(
        "parse complete: %d records seen, %d kept, %d HR samples, %d workouts",
        agg.records_seen,
        agg.records_kept,
        len(agg.hr_samples),
        len(agg.workouts),
    )

    _log.info(
        "sources seen: %s (preferring '%s')",
        sorted(agg.all_sources()),
        args.prefer_source,
    )
    unified, raw = build_points(
        agg,
        device=args.device_name,
        database_name=args.database_name,
        preferred_source=args.prefer_source,
    )
    _log.info("unified points built: %s", _summarize(unified))
    _log.info("raw points built:     %s", _summarize(raw))

    if args.dry_run:
        _log.info("dry-run: skipping InfluxDB write")
        return 0

    # Import garmin_fetch lazily so --dry-run works without InfluxDB env
    # vars present (useful for CI smoke / offline inspection).
    from .. import garmin_fetch  # noqa: PLC0415 — intentional late import

    for batch in _chunked(raw, args.batch_size):
        garmin_fetch.write_points_to_influxdb(batch)
    for batch in _chunked(unified, args.batch_size):
        garmin_fetch.write_points_to_influxdb(batch)

    _log.info(
        "wrote %d raw + %d unified points to InfluxDB",
        len(raw),
        len(unified),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
