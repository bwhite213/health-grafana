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
M_VO2_MAX = "UnifiedVO2Max"
M_WORKOUT = "UnifiedWorkout"

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
    fields = _drop_nones(
        {
            "steps": steps,
            "calories_active": calories_active,
            "calories_total": calories_total,
            "distance_m": distance_m,
            "active_minutes": active_minutes,
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

        _append_if_present(
            points,
            unified_heart_rate_point(
                **tag_base,
                time=start,
                rhr=daily_stats.get("restingHeartRate"),
                hr_avg=daily_stats.get("minAvgHeartRate"),  # Garmin's closest "avg"
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
        _append_if_present(
            points,
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
            ),
        )

        # Heart rate summary derived from the sleep window (Oura's most reliable HR data).
        _append_if_present(
            points,
            unified_heart_rate_point(
                **tag_base,
                time=bedtime_end or f"{date_str}T12:00:00+00:00",
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

    return points
