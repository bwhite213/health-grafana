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

from datetime import datetime
from typing import Any, Iterable

import pytz

# Measurement names (kept as constants so dashboard JSON and code stay in sync)
M_SLEEP = "UnifiedSleep"
M_HEART_RATE = "UnifiedHeartRate"
M_HR_INTRADAY = "UnifiedHRIntraday"
M_ACTIVITY = "UnifiedActivity"
M_READINESS = "UnifiedReadiness"

SOURCE_GARMIN = "Garmin"
SOURCE_OURA = "Oura"


def _base_tags(source: str, device: str, database_name: str) -> dict[str, str]:
    return {
        "Source": source,
        "Device": device or source,
        "Database_Name": database_name,
    }


def _drop_nones(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not None}


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
) -> dict[str, Any]:
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
        }
    )
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
) -> dict[str, Any]:
    return {
        "measurement": M_HEART_RATE,
        "time": _iso(time),
        "tags": _base_tags(source, device, database_name),
        "fields": _drop_nones(
            {
                "rhr": rhr,
                "hr_avg": hr_avg,
                "hr_max": hr_max,
                "hr_min": hr_min,
            }
        ),
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
) -> dict[str, Any]:
    return {
        "measurement": M_ACTIVITY,
        "time": _iso(time),
        "tags": _base_tags(source, device, database_name),
        "fields": _drop_nones(
            {
                "steps": steps,
                "calories_active": calories_active,
                "calories_total": calories_total,
                "distance_m": distance_m,
                "active_minutes": active_minutes,
            }
        ),
    }


def unified_readiness_point(
    *,
    source: str,
    device: str,
    database_name: str,
    time: datetime | str,
    score: float | None,
) -> dict[str, Any]:
    return {
        "measurement": M_READINESS,
        "time": _iso(time),
        "tags": _base_tags(source, device, database_name),
        "fields": _drop_nones({"score": score}),
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
) -> list[dict[str, Any]]:
    """
    Build Unified* points from the raw Garmin Connect dicts that
    ``garmin_fetch.py`` already fetches.

    - ``daily_stats``: return value of ``garmin_obj.get_stats(date_str)``
    - ``sleep_data``:  return value of ``garmin_obj.get_sleep_data(date_str)``
    - ``body_battery_value``: a single 0-100 score (e.g. wake-time body battery)

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
            points.append(
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
                )
            )

    # --- Daily activity + heart rate summary ---
    if daily_stats and daily_stats.get("wellnessStartTimeGmt"):
        start = datetime.strptime(
            daily_stats["wellnessStartTimeGmt"], "%Y-%m-%dT%H:%M:%S.%f"
        )
        start = pytz.utc.localize(start)

        points.append(
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
            )
        )

        points.append(
            unified_heart_rate_point(
                **tag_base,
                time=start,
                rhr=daily_stats.get("restingHeartRate"),
                hr_avg=daily_stats.get("minAvgHeartRate"),  # Garmin's closest "avg"
                hr_max=daily_stats.get("maxHeartRate"),
                hr_min=daily_stats.get("minHeartRate"),
            )
        )

        if body_battery_value is None:
            body_battery_value = daily_stats.get("bodyBatteryAtWakeTime") or daily_stats.get(
                "bodyBatteryHighestValue"
            )
        if body_battery_value is not None:
            points.append(
                unified_readiness_point(
                    **tag_base,
                    time=start,
                    score=float(body_battery_value),
                )
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
) -> list[dict[str, Any]]:
    """
    Build Unified* points from Oura Cloud API v2 response dicts.

    Oura endpoint mapping (see sources/oura_fetch.py):
      - /daily_sleep       -> ``daily_sleep``     (score, contributors)
      - /sleep             -> ``sleep_detail``    (stages, hrv, rhr, efficiency)
      - /daily_activity    -> ``daily_activity``  (steps, calories, active time)
      - /daily_readiness   -> ``daily_readiness`` (readiness score 0-100)
    """
    points: list[dict[str, Any]] = []
    tag_base = {"source": SOURCE_OURA, "device": device_name, "database_name": database_name}

    # --- Sleep ---
    if sleep_detail:
        bedtime_end = sleep_detail.get("bedtime_end") or sleep_detail.get("day")
        total = sleep_detail.get("total_sleep_duration")
        deep = sleep_detail.get("deep_sleep_duration")
        light = sleep_detail.get("light_sleep_duration")
        rem = sleep_detail.get("rem_sleep_duration")
        awake = sleep_detail.get("awake_time")
        hrv = sleep_detail.get("average_hrv")
        rhr = sleep_detail.get("average_heart_rate") or sleep_detail.get("lowest_heart_rate")
        efficiency = sleep_detail.get("efficiency")
        score = None
        if daily_sleep:
            score = daily_sleep.get("score")
        points.append(
            unified_sleep_point(
                **tag_base,
                time=bedtime_end or f"{date_str}T12:00:00+00:00",
                duration_s=total,
                deep_s=deep,
                light_s=light,
                rem_s=rem,
                awake_s=awake,
                hrv_avg=hrv,
                rhr=rhr,
                efficiency=efficiency,
                score=score,
            )
        )

        # Heart rate summary derived from the sleep window (Oura's most reliable HR data).
        points.append(
            unified_heart_rate_point(
                **tag_base,
                time=bedtime_end or f"{date_str}T12:00:00+00:00",
                rhr=sleep_detail.get("lowest_heart_rate"),
                hr_avg=sleep_detail.get("average_heart_rate"),
                hr_max=None,
                hr_min=sleep_detail.get("lowest_heart_rate"),
            )
        )

    # --- Activity ---
    if daily_activity:
        day = daily_activity.get("day") or date_str
        day_ts = f"{day}T12:00:00+00:00"
        points.append(
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
            )
        )

    # --- Readiness ---
    if daily_readiness:
        day = daily_readiness.get("day") or date_str
        day_ts = f"{day}T12:00:00+00:00"
        points.append(
            unified_readiness_point(
                **tag_base,
                time=day_ts,
                score=daily_readiness.get("score"),
            )
        )

    return points
