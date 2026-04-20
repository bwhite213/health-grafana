"""
Unified-schema helpers for multi-source health data.

Every data source in `health-grafana` writes its native measurements (the
source of truth) AND emits a normalized mirror into the Unified* measurements
defined here. The Multi-Source Health Grafana dashboard queries the Unified*
measurements so it can show per-source, overlay, and cross-source-average
views without caring which device produced the data.

This module provides:

- Point builder helpers (``unified_sleep_point``, ``unified_heart_rate_point``,
  ``unified_activity_point``, ``unified_readiness_point``,
  ``unified_hr_intraday_points``) that assemble InfluxDB point dicts with the
  correct measurement name, tag layout, and field types.
- Normalizers that turn native Garmin / Oura dicts into those unified points
  (``garmin_to_unified``, ``oura_to_unified``).

None of these helpers talk to InfluxDB directly — they just return
``list[dict]`` in the same shape used throughout ``garmin_fetch.py`` so the
existing ``write_points_to_influxdb`` helper can persist them.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

import pytz

# Measurement names (kept as constants so dashboard JSON and code stay in sync)
M_SLEEP = "UnifiedSleep"
M_HEART_RATE = "UnifiedHeartRate"
M_HR_INTRADAY = "UnifiedHRIntraday"
M_ACTIVITY = "UnifiedActivity"
M_READINESS = "UnifiedReadiness"
M_VO2_MAX = "UnifiedVO2Max"
M_WORKOUT = "UnifiedWorkout"
M_STRESS = "UnifiedStress"

SOURCE_GARMIN = "Garmin"
SOURCE_OURA = "Oura"
SOURCE_APPLE = "Apple"


def _base_tags(source: str, device: str, database_name: str) -> dict[str, str]:
    return {
        "Source": source,
        "Device": device or source,
        "Database_Name": database_name,
    }


def _drop_nones(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not None}


def _append_if_present(points: list, point: dict | None) -> None:
    """Append a point to ``points`` iff the point builder actually produced one.

    Builders return ``None`` when every field was ``None`` (e.g. Garmin days
    where the watch wasn't worn). Emitting such a point would produce an
    InfluxDB line-protocol line with no fields, which the server rejects as
    ``invalid field format``.
    """
    if point is not None:
        points.append(point)


def _iso(ts: datetime | str | None) -> str | None:
    """Normalize timestamps to UTC ISO-8601 strings."""
    if ts is None:
        return None
    if isinstance(ts, str):
        # Already ISO; trust it (sources emit UTC ISO or GMT ISO).
        return ts
    if ts.tzinfo is None:
        ts = pytz.utc.localize(ts)
    return ts.astimezone(pytz.utc).isoformat()


# ---------------------------------------------------------------------------
# Point builders
# ---------------------------------------------------------------------------


def unified_sleep_point(
    *,
    source: str,
    device: str,
    database_name: str,
    time: datetime | str,
    duration_s: int | None = None,
    deep_s: int | None = None,
    light_s: int | None = None,
    rem_s: int | None = None,
    awake_s: int | None = None,
    hrv_avg: float | None = None,
    rhr: float | None = None,
    efficiency: float | None = None,
    score: float | None = None,
    spo2_avg: float | None = None,
) -> dict[str, Any] | None:
    fields = _drop_nones(
        {
            "duration_s": duration_s,
            "deep_s": deep_s,
            "light_s": light_s,
            "rem_s": rem_s,
            "awake_s": awake_s,
            "hrv_avg": hrv_avg,
            "rhr": rhr,
            "efficiency": efficiency,
            "score": score,
            "spo2_avg": spo2_avg,
        }
    )
    if not fields:
        return None
    return {
        "measurement": M_SLEEP,
        "time": _iso(time),
        "tags": _base_tags(source, device, database_name),
        "fields": fields,
    }


def unified_heart_rate_point(
    *,
    source: str,
    device: str,
    database_name: str,
    time: datetime | str,
    rhr: float | None = None,
    hr_avg: float | None = None,
    hr_max: float | None = None,
    hr_min: float | None = None,
) -> dict[str, Any] | None:
    fields = _drop_nones(
        {
            "rhr": rhr,
            "hr_avg": hr_avg,
            "hr_max": hr_max,
            "hr_min": hr_min,
        }
    )
    if not fields:
        return None
    return {
        "measurement": M_HEART_RATE,
        "time": _iso(time),
        "tags": _base_tags(source, device, database_name),
        "fields": fields,
    }


def unified_hr_intraday_points(
    *,
    source: str,
    device: str,
    database_name: str,
    samples: Iterable[tuple[datetime | str, float]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    tags = _base_tags(source, device, database_name)
    for ts, hr in samples:
        if hr is None:
            continue
        out.append(
            {
                "measurement": M_HR_INTRADAY,
                "time": _iso(ts),
                "tags": tags,
                "fields": {"hr": float(hr)},
            }
        )
    return out


def unified_activity_point(
    *,
    source: str,
    device: str,
    database_name: str,
    time: datetime | str,
    steps: int | None = None,
    calories_active: float | None = None,
    calories_total: float | None = None,
    distance_m: float | None = None,
    active_minutes: float | None = None,
) -> dict[str, Any] | None:
    # Coerce to int so the InfluxDB field type is stable across sources.
    # Garmin returns ints, Oura returns floats; once a field is written as one
    # type, later writes of the other type get silently dropped.
    def _as_int(v):
        return None if v is None else int(round(float(v)))

    fields = _drop_nones(
        {
            "steps": _as_int(steps),
            "calories_active": _as_int(calories_active),
            "calories_total": _as_int(calories_total),
            "distance_m": _as_int(distance_m),
            "active_minutes": _as_int(active_minutes),
        }
    )
    if not fields:
        return None
    return {
        "measurement": M_ACTIVITY,
        "time": _iso(time),
        "tags": _base_tags(source, device, database_name),
        "fields": fields,
    }


def unified_readiness_point(
    *,
    source: str,
    device: str,
    database_name: str,
    time: datetime | str,
    score: float | None,
) -> dict[str, Any] | None:
    fields = _drop_nones({"score": score})
    if not fields:
        return None
    return {
        "measurement": M_READINESS,
        "time": _iso(time),
        "tags": _base_tags(source, device, database_name),
        "fields": fields,
    }


def unified_vo2_max_point(
    *,
    source: str,
    device: str,
    database_name: str,
    time: datetime | str,
    vo2_max: float | None,
) -> dict[str, Any] | None:
    fields = _drop_nones({"vo2_max": vo2_max})
    if not fields:
        return None
    return {
        "measurement": M_VO2_MAX,
        "time": _iso(time),
        "tags": _base_tags(source, device, database_name),
        "fields": fields,
    }


def unified_workout_point(
    *,
    source: str,
    device: str,
    database_name: str,
    time: datetime | str,
    activity_type: str | None = None,
    duration_s: int | None = None,
    calories: float | None = None,
    distance_m: float | None = None,
    hr_avg: float | None = None,
    hr_max: float | None = None,
    intensity: str | None = None,
) -> dict[str, Any] | None:
    fields = _drop_nones(
        {
            "duration_s": duration_s,
            "calories": calories,
            "distance_m": distance_m,
            "hr_avg": hr_avg,
            "hr_max": hr_max,
            # Store intensity as a field (string) so it's still queryable even
            # though it's not numeric.
            "intensity": intensity,
        }
    )
    if not fields:
        return None
    tags = _base_tags(source, device, database_name)
    # activity_type is promoted to a tag so Grafana can filter by sport.
    if activity_type:
        tags["Activity"] = activity_type
    return {
        "measurement": M_WORKOUT,
        "time": _iso(time),
        "tags": tags,
        "fields": fields,
    }


def unified_stress_point(
    *,
    source: str,
    device: str,
    database_name: str,
    time: datetime | str,
    stress_high_s: int | None = None,
    stress_avg: float | None = None,
    recovery_high_s: int | None = None,
) -> dict[str, Any] | None:
    """Daily stress summary, normalized across sources.

    - ``stress_high_s``: seconds spent in "high" stress state during the day.
      Garmin reports this directly as ``highStressDuration``. Oura exposes a
      similar integer on ``/daily_stress.stress_high`` (seconds).
    - ``stress_avg``: 0-100 average stress score when the source provides one
      (Garmin ``stressPercentage``). Oura doesn't expose a single average, so
      this stays None for Oura points.
    - ``recovery_high_s``: seconds in "recovery" state. Oura-only field;
      Garmin has no direct equivalent.
    """
    fields = _drop_nones(
        {
            "stress_high_s": stress_high_s,
            "stress_avg": stress_avg,
            "recovery_high_s": recovery_high_s,
        }
    )
    if not fields:
        return None
    return {
        "measurement": M_STRESS,
        "time": _iso(time),
        "tags": _base_tags(source, device, database_name),
        "fields": fields,
    }


# ---------------------------------------------------------------------------
# Normalizers: native source dict -> list of unified points
# ---------------------------------------------------------------------------


def garmin_to_unified(
    *,
    date_str: str,
    device_name: str,
    database_name: str,
    daily_stats: dict | None = None,
    sleep_data: dict | None = None,
    body_battery_value: float | None = None,
    training_readiness: list | None = None,
    max_metrics: list | None = None,
    activities: list | None = None,
) -> list[dict[str, Any]]:
    """
    Build Unified* points from the raw Garmin Connect dicts that
    ``garmin_fetch.py`` already fetches.

    - ``daily_stats``: return value of ``garmin_obj.get_stats(date_str)``
    - ``sleep_data``:  return value of ``garmin_obj.get_sleep_data(date_str)``
    - ``body_battery_value``: a single 0-100 score (wake-time body battery)
    - ``training_readiness``: return value of ``garmin_obj.get_training_readiness(date_str)``
      (list of readiness dicts). When present and it has a numeric ``score``, it
      is preferred over body battery for ``UnifiedReadiness`` because it's the
      closest Garmin analogue to Oura Readiness.
    - ``max_metrics``: return value of ``garmin_obj.get_max_metrics(date_str)``
      (list of VO2 max dicts). Its ``generic.vo2MaxPreciseValue`` becomes a
      ``UnifiedVO2Max`` point.
    - ``activities``: return value of ``garmin_obj.get_activities_by_date(d, d)``
      (list of activity summary dicts). Each becomes a ``UnifiedWorkout`` point
      tagged by ``activityType.typeKey``.

    All arguments are optional; whichever dicts are provided will be mapped.
    """
    points: list[dict[str, Any]] = []
    tag_base = {"source": SOURCE_GARMIN, "device": device_name, "database_name": database_name}

    # --- Sleep ---
    if sleep_data:
        sleep_dto = sleep_data.get("dailySleepDTO") or {}
        sleep_end_ms = sleep_dto.get("sleepEndTimestampGMT")
        if sleep_end_ms:
            sleep_time = datetime.fromtimestamp(sleep_end_ms / 1000, tz=pytz.utc)
            total = sleep_dto.get("sleepTimeSeconds")
            deep = sleep_dto.get("deepSleepSeconds")
            light = sleep_dto.get("lightSleepSeconds")
            rem = sleep_dto.get("remSleepSeconds")
            awake = sleep_dto.get("awakeSleepSeconds")
            score = ((sleep_dto.get("sleepScores") or {}).get("overall") or {}).get("value")
            efficiency = None
            if total and (deep or light or rem) and awake is not None:
                in_bed = (total or 0) + (awake or 0)
                if in_bed > 0:
                    efficiency = round(100.0 * total / in_bed, 2)
            _append_if_present(
                points,
                unified_sleep_point(
                    **tag_base,
                    time=sleep_time,
                    duration_s=total,
                    deep_s=deep,
                    light_s=light,
                    rem_s=rem,
                    awake_s=awake,
                    hrv_avg=sleep_data.get("avgOvernightHrv"),
                    rhr=sleep_data.get("restingHeartRate"),
                    efficiency=efficiency,
                    score=score,
                    spo2_avg=sleep_dto.get("averageSpO2Value"),
                )
            )

    # --- Daily activity + heart rate summary ---
    if daily_stats and daily_stats.get("wellnessStartTimeGmt"):
        start = datetime.strptime(
            daily_stats["wellnessStartTimeGmt"], "%Y-%m-%dT%H:%M:%S.%f"
        )
        start = pytz.utc.localize(start)

        _append_if_present(
            points,
            unified_activity_point(
                **tag_base,
                time=start,
                steps=daily_stats.get("totalSteps"),
                calories_active=daily_stats.get("activeKilocalories"),
                calories_total=(
                    (daily_stats.get("activeKilocalories") or 0)
                    + (daily_stats.get("bmrKilocalories") or 0)
                )
                or None,
                distance_m=daily_stats.get("totalDistanceMeters"),
                active_minutes=(
                    (daily_stats.get("moderateIntensityMinutes") or 0)
                    + (daily_stats.get("vigorousIntensityMinutes") or 0)
                )
                or None,
            ),
        )

        # Garmin's DailyStats has no true daily-average HR field —
        # `minAvgHeartRate` is the *minimum* of the day's rolling 2-min
        # averages (close to RHR), not an average, so publishing it as
        # `hr_avg` understates the real average by 20-30 bpm. Leave
        # hr_avg unset; consumers that need an average should compute it
        # at query time from HeartRateIntraday.
        _append_if_present(
            points,
            unified_heart_rate_point(
                **tag_base,
                time=start,
                rhr=daily_stats.get("restingHeartRate"),
                hr_max=daily_stats.get("maxHeartRate"),
                hr_min=daily_stats.get("minHeartRate"),
            ),
        )

        # Readiness: prefer Training Readiness (direct analogue to Oura Readiness)
        # when available, fall back to Body Battery otherwise.
        readiness_score = None
        readiness_time = start
        if training_readiness:
            # Garmin returns a list of readiness snapshots through the day; pick
            # the latest one with a numeric score.
            for tr in reversed(training_readiness):
                if isinstance(tr, dict) and tr.get("score") is not None:
                    readiness_score = float(tr["score"])
                    ts = tr.get("timestamp")
                    if ts:
                        try:
                            readiness_time = pytz.utc.localize(
                                datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f")
                            )
                        except (ValueError, TypeError):
                            pass
                    break
        if readiness_score is None:
            if body_battery_value is None:
                body_battery_value = (
                    daily_stats.get("bodyBatteryAtWakeTime")
                    or daily_stats.get("bodyBatteryHighestValue")
                )
            if body_battery_value is not None:
                readiness_score = float(body_battery_value)
        if readiness_score is not None:
            _append_if_present(
                points,
                unified_readiness_point(
                    **tag_base,
                    time=readiness_time,
                    score=readiness_score,
                ),
            )

        # Stress: Garmin already aggregates high/low/medium stress durations
        # in seconds on DailyStats. We only mirror "high" into unified stress
        # since it's the one directly comparable across sources.
        high_stress_seconds = daily_stats.get("highStressDuration")
        stress_pct = daily_stats.get("stressPercentage")
        if high_stress_seconds is not None or stress_pct is not None:
            _append_if_present(
                points,
                unified_stress_point(
                    **tag_base,
                    time=start,
                    stress_high_s=int(high_stress_seconds) if high_stress_seconds is not None else None,
                    stress_avg=float(stress_pct) if stress_pct is not None else None,
                ),
            )

    # --- VO2 max ---
    if max_metrics:
        try:
            generic = (max_metrics[0] or {}).get("generic") or {}
            vo2_max = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
        except (IndexError, AttributeError):
            vo2_max = None
        if vo2_max is not None:
            # Garmin's VO2 max is a daily value; stamp it at 00:00 UTC of the
            # date to match the raw VO2_Max measurement.
            day_ts = datetime.strptime(date_str, "%Y-%m-%d").replace(
                hour=0, tzinfo=pytz.utc
            )
            _append_if_present(
                points,
                unified_vo2_max_point(
                    **tag_base,
                    time=day_ts,
                    vo2_max=float(vo2_max),
                ),
            )

    # --- Workouts ---
    if activities:
        for activity in activities:
            if not isinstance(activity, dict):
                continue
            start_gmt = activity.get("startTimeGMT")
            if not start_gmt:
                continue
            try:
                workout_time = datetime.strptime(
                    start_gmt, "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=pytz.utc)
            except ValueError:
                continue
            activity_type = (activity.get("activityType") or {}).get("typeKey")
            # Garmin stores duration in seconds as a float.
            duration = activity.get("duration") or activity.get("elapsedDuration")
            _append_if_present(
                points,
                unified_workout_point(
                    **tag_base,
                    time=workout_time,
                    activity_type=activity_type,
                    duration_s=int(duration) if duration is not None else None,
                    calories=activity.get("calories"),
                    distance_m=activity.get("distance"),
                    hr_avg=activity.get("averageHR"),
                    hr_max=activity.get("maxHR"),
                    intensity=None,  # Garmin doesn't supply a simple intensity label
                ),
            )

    return points


def oura_to_unified(
    *,
    date_str: str,
    device_name: str,
    database_name: str,
    daily_sleep: dict | None = None,
    sleep_detail: dict | None = None,
    daily_activity: dict | None = None,
    daily_readiness: dict | None = None,
    vo2_max: dict | None = None,
    workouts: list | None = None,
    daily_spo2: dict | None = None,
    daily_stress: dict | None = None,
) -> list[dict[str, Any]]:
    """
    Build Unified* points from Oura Cloud API v2 response dicts.

    Oura endpoint mapping (see sources/oura_fetch.py):
      - /daily_sleep       -> ``daily_sleep``     (score, contributors)
      - /sleep             -> ``sleep_detail``    (stages, hrv, rhr, efficiency)
      - /daily_activity    -> ``daily_activity``  (steps, calories, active time)
      - /daily_readiness   -> ``daily_readiness`` (readiness score 0-100)
      - /vO2_max           -> ``vo2_max``         (single daily VO2 max value)
      - /workout           -> ``workouts``        (list of workout sessions)
      - /daily_spo2        -> ``daily_spo2``      (nightly average SpO2 %)
      - /daily_stress      -> ``daily_stress``    (seconds in high stress)
    """
    points: list[dict[str, Any]] = []
    tag_base = {"source": SOURCE_OURA, "device": device_name, "database_name": database_name}

    # --- Sleep ---
    if sleep_detail:
        # Stamp at noon UTC of Oura's `day` (the wake-up calendar day) so the
        # point lands in the same UTC-day bucket Grafana uses for GROUP BY
        # time(1d), independent of the user's timezone. bedtime_end is a
        # local-time ISO string, which can drift a bucket off for non-UTC
        # users. Matches the convention used for daily_activity/readiness.
        sleep_day = sleep_detail.get("day") or date_str
        sleep_time = f"{sleep_day}T12:00:00+00:00"
        total = sleep_detail.get("total_sleep_duration")
        deep = sleep_detail.get("deep_sleep_duration")
        light = sleep_detail.get("light_sleep_duration")
        rem = sleep_detail.get("rem_sleep_duration")
        awake = sleep_detail.get("awake_time")
        hrv = sleep_detail.get("average_hrv")
        # Oura's "Resting heart rate" in the app is the lowest sleeping HR,
        # not the average. Prefer lowest_heart_rate so the unified RHR
        # matches the number the user sees in the Oura app.
        rhr = sleep_detail.get("lowest_heart_rate") or sleep_detail.get("average_heart_rate")
        efficiency = sleep_detail.get("efficiency")
        score = None
        if daily_sleep:
            score = daily_sleep.get("score")
        # Oura exposes the nightly SpO2 on a separate endpoint (/daily_spo2).
        # Fold it into the UnifiedSleep point so a single query can compare
        # sleep SpO2 across sources. Oura returns it under `spo2_percentage`
        # as a dict {average, ...} depending on API version; accept either a
        # flat float or the nested form.
        spo2_avg = None
        if daily_spo2:
            raw = daily_spo2.get("spo2_percentage")
            if isinstance(raw, dict):
                spo2_avg = raw.get("average")
            elif isinstance(raw, int | float):
                spo2_avg = float(raw)
        _append_if_present(
            points,
            unified_sleep_point(
                **tag_base,
                time=sleep_time,
                duration_s=total,
                deep_s=deep,
                light_s=light,
                rem_s=rem,
                awake_s=awake,
                hrv_avg=hrv,
                rhr=rhr,
                efficiency=efficiency,
                score=score,
                spo2_avg=spo2_avg,
            ),
        )

        # Heart rate summary derived from the sleep window (Oura's most reliable HR data).
        _append_if_present(
            points,
            unified_heart_rate_point(
                **tag_base,
                time=sleep_time,
                rhr=sleep_detail.get("lowest_heart_rate"),
                hr_avg=sleep_detail.get("average_heart_rate"),
                hr_max=None,
                hr_min=sleep_detail.get("lowest_heart_rate"),
            ),
        )

    # --- Activity ---
    if daily_activity:
        day = daily_activity.get("day") or date_str
        day_ts = f"{day}T12:00:00+00:00"
        _append_if_present(
            points,
            unified_activity_point(
                **tag_base,
                time=day_ts,
                steps=daily_activity.get("steps"),
                calories_active=daily_activity.get("active_calories"),
                calories_total=daily_activity.get("total_calories"),
                distance_m=daily_activity.get("equivalent_walking_distance"),
                active_minutes=(
                    (daily_activity.get("medium_activity_time") or 0) / 60
                    + (daily_activity.get("high_activity_time") or 0) / 60
                )
                or None,
            ),
        )

    # --- Readiness ---
    if daily_readiness:
        day = daily_readiness.get("day") or date_str
        day_ts = f"{day}T12:00:00+00:00"
        _append_if_present(
            points,
            unified_readiness_point(
                **tag_base,
                time=day_ts,
                score=daily_readiness.get("score"),
            ),
        )

    # --- VO2 max ---
    if vo2_max:
        day = vo2_max.get("day") or date_str
        day_ts = f"{day}T00:00:00+00:00"
        _append_if_present(
            points,
            unified_vo2_max_point(
                **tag_base,
                time=day_ts,
                vo2_max=vo2_max.get("vo2_max"),
            ),
        )

    # --- Workouts ---
    if workouts:
        for w in workouts:
            if not isinstance(w, dict):
                continue
            start_ts = w.get("start_datetime") or w.get("day")
            if not start_ts:
                continue
            end_ts = w.get("end_datetime")
            duration_s: int | None = None
            if start_ts and end_ts:
                try:
                    start_dt = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
                    end_dt = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
                    duration_s = int((end_dt - start_dt).total_seconds())
                except ValueError:
                    duration_s = None
            _append_if_present(
                points,
                unified_workout_point(
                    **tag_base,
                    time=start_ts,
                    activity_type=w.get("activity"),
                    duration_s=duration_s,
                    calories=w.get("calories"),
                    distance_m=w.get("distance"),
                    hr_avg=w.get("average_heart_rate"),
                    hr_max=w.get("max_heart_rate"),
                    intensity=w.get("intensity"),
                ),
            )

    # --- Stress ---
    if daily_stress:
        day = daily_stress.get("day") or date_str
        day_ts = f"{day}T12:00:00+00:00"
        _append_if_present(
            points,
            unified_stress_point(
                **tag_base,
                time=day_ts,
                stress_high_s=daily_stress.get("stress_high"),
                recovery_high_s=daily_stress.get("recovery_high"),
            ),
        )

    return points


def apple_to_unified(
    *,
    date_str: str,
    device_name: str,
    database_name: str,
    sleep: dict | None = None,
    activity: dict | None = None,
    heart_rate: dict | None = None,
    vo2_max: float | None = None,
    workouts: list | None = None,
) -> list[dict[str, Any]]:
    """Build Unified* points from the per-day aggregates produced by
    ``sources.apple_healthkit`` after it streams the iPhone ``export.xml``.

    Unlike Garmin/Oura (which receive full native response dicts from each
    endpoint), Apple's export is a flat stream of `<Record>` elements. The
    importer is responsible for grouping records by local day and sensor
    type before calling this normalizer — so the inputs here are
    already-rolled-up daily summaries.

    Expected shape of each arg (all keys optional):

    - ``sleep``: ``{"total_s", "deep_s", "light_s", "rem_s", "awake_s",
      "hrv_avg", "efficiency"}`` (Apple has no sleep score equivalent)
    - ``activity``: ``{"steps", "calories_active", "calories_total",
      "distance_m", "active_minutes"}``
    - ``heart_rate``: ``{"rhr", "hr_avg", "hr_max", "hr_min"}``
    - ``vo2_max``: single float (ml/kg/min)
    - ``workouts``: list of ``{"activity_type", "start", "duration_s",
      "calories", "distance_m", "hr_avg", "hr_max"}`` dicts

    Apple intentionally has no ``daily_readiness`` or ``daily_stress``
    equivalent — skip those rather than synthesize values.
    """
    points: list[dict[str, Any]] = []
    tag_base = {"source": SOURCE_APPLE, "device": device_name, "database_name": database_name}

    # Apple timestamps land at noon UTC on the local day, matching the Oura
    # convention — this keeps cross-source overlays aligned on the same day
    # bucket regardless of timezone.
    day_ts = f"{date_str}T12:00:00+00:00"

    if sleep:
        _append_if_present(
            points,
            unified_sleep_point(
                **tag_base,
                time=day_ts,
                duration_s=sleep.get("total_s"),
                deep_s=sleep.get("deep_s"),
                light_s=sleep.get("light_s"),
                rem_s=sleep.get("rem_s"),
                awake_s=sleep.get("awake_s"),
                hrv_avg=sleep.get("hrv_avg"),
                efficiency=sleep.get("efficiency"),
            ),
        )

    if activity:
        _append_if_present(
            points,
            unified_activity_point(
                **tag_base,
                time=day_ts,
                steps=activity.get("steps"),
                calories_active=activity.get("calories_active"),
                calories_total=activity.get("calories_total"),
                distance_m=activity.get("distance_m"),
                active_minutes=activity.get("active_minutes"),
            ),
        )

    if heart_rate:
        _append_if_present(
            points,
            unified_heart_rate_point(
                **tag_base,
                time=day_ts,
                rhr=heart_rate.get("rhr"),
                hr_avg=heart_rate.get("hr_avg"),
                hr_max=heart_rate.get("hr_max"),
                hr_min=heart_rate.get("hr_min"),
            ),
        )

    if vo2_max is not None:
        _append_if_present(
            points,
            unified_vo2_max_point(
                **tag_base,
                time=f"{date_str}T00:00:00+00:00",
                vo2_max=float(vo2_max),
            ),
        )

    if workouts:
        for w in workouts:
            if not isinstance(w, dict):
                continue
            start = w.get("start")
            if not start:
                continue
            _append_if_present(
                points,
                unified_workout_point(
                    **tag_base,
                    time=start,
                    activity_type=w.get("activity_type"),
                    duration_s=w.get("duration_s"),
                    calories=w.get("calories"),
                    distance_m=w.get("distance_m"),
                    hr_avg=w.get("hr_avg"),
                    hr_max=w.get("hr_max"),
                ),
            )

    return points
