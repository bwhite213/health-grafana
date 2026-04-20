"""
Oura Ring data fetcher.

Pulls daily summary data (sleep, activity, readiness) and intraday heart rate
from the Oura Cloud API v2 and returns them as:

- Raw ``Oura*`` InfluxDB point dicts (source of truth per-source view)
- Unified ``Unified*`` points via ``unified_schema.oura_to_unified``

The fetcher is intentionally minimal — it uses ``requests`` directly to avoid
adding a new heavy dependency. Auth is a single Personal Access Token (PAT)
from https://cloud.ouraring.com/personal-access-tokens, supplied via the
``OURA_PERSONAL_ACCESS_TOKEN`` environment variable.

Oura's rate limit is 5000 requests per 5 minutes — effectively unlimited for
our use case — so no throttling logic is needed beyond sensible per-cycle
fetching.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import pytz
import requests

from . import unified_schema

OURA_BASE_URL = "https://api.ouraring.com/v2/usercollection"
OURA_DEVICE_NAME = "Oura Ring"

_log = logging.getLogger(__name__)


class OuraClient:
    """Thin wrapper around Oura Cloud API v2 endpoints we consume."""

    def __init__(self, personal_access_token: str, timeout: int = 30):
        if not personal_access_token:
            raise ValueError("OURA_PERSONAL_ACCESS_TOKEN is required")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {personal_access_token}",
                "Accept": "application/json",
            }
        )
        self._timeout = timeout

    # --- internal ---
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{OURA_BASE_URL}{path}"
        resp = self._session.get(url, params=params or {}, timeout=self._timeout)
        if resp.status_code == 401:
            raise RuntimeError(
                "Oura authentication failed (401). Verify OURA_PERSONAL_ACCESS_TOKEN."
            )
        resp.raise_for_status()
        return resp.json()

    # --- daily summaries (return the single record for `date_str` or None) ---
    def _daily_single(self, path: str, date_str: str) -> dict[str, Any] | None:
        # Oura's daily endpoints use inclusive date range. end_date=date_str+1 gives us
        # the record for date_str.
        end = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        data = self._get(path, {"start_date": date_str, "end_date": end})
        for item in data.get("data", []):
            if item.get("day") == date_str:
                return item
        return None

    def get_daily_sleep(self, date_str: str) -> dict[str, Any] | None:
        return self._daily_single("/daily_sleep", date_str)

    def get_daily_activity(self, date_str: str) -> dict[str, Any] | None:
        return self._daily_single("/daily_activity", date_str)

    def get_daily_readiness(self, date_str: str) -> dict[str, Any] | None:
        return self._daily_single("/daily_readiness", date_str)

    def get_sleep_detail(self, date_str: str) -> dict[str, Any] | None:
        """Return the main long_sleep record whose day == date_str."""
        end = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        data = self._get("/sleep", {"start_date": date_str, "end_date": end})
        candidates = [
            s
            for s in data.get("data", [])
            if s.get("day") == date_str
            and s.get("type") in ("long_sleep", "sleep", None)
        ]
        if not candidates:
            return None
        # Pick the longest (main) sleep session for the day.
        candidates.sort(key=lambda s: s.get("total_sleep_duration") or 0, reverse=True)
        return candidates[0]

    def get_vo2_max(self, date_str: str) -> dict[str, Any] | None:
        """Return Oura's daily VO2 max record for ``date_str``, if present."""
        return self._daily_single("/vO2_max", date_str)

    def get_workouts(self, date_str: str) -> list[dict[str, Any]]:
        """Return workouts whose ``day`` matches ``date_str``."""
        end = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        data = self._get("/workout", {"start_date": date_str, "end_date": end})
        return [w for w in data.get("data", []) if w.get("day") == date_str]

    def get_daily_spo2(self, date_str: str) -> dict[str, Any] | None:
        """Return Oura's daily SpO2 record for ``date_str``, if present."""
        return self._daily_single("/daily_spo2", date_str)

    def get_daily_stress(self, date_str: str) -> dict[str, Any] | None:
        """Return Oura's daily stress summary for ``date_str``, if present."""
        return self._daily_single("/daily_stress", date_str)

    def get_enhanced_tags(self, date_str: str) -> list[dict[str, Any]]:
        """Return user-logged tags whose ``day`` matches ``date_str``.

        Uses the newer ``/enhanced_tag`` endpoint (the legacy ``/tag`` endpoint
        is deprecated). Tags are free-form user events like "sick", "alcohol",
        "travel" — they're what surfaces as Grafana annotations on the
        dashboards.
        """
        end = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        data = self._get(
            "/enhanced_tag", {"start_date": date_str, "end_date": end}
        )
        return [t for t in data.get("data", []) if t.get("day") == date_str]

    def get_heartrate_intraday(
        self, date_str: str
    ) -> list[tuple[datetime, float]]:
        start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=pytz.utc)
        end_dt = start_dt + timedelta(days=1)
        params = {
            "start_datetime": start_dt.isoformat(),
            "end_datetime": end_dt.isoformat(),
        }
        data = self._get("/heartrate", params)
        samples: list[tuple[datetime, float]] = []
        for item in data.get("data", []):
            ts = item.get("timestamp")
            bpm = item.get("bpm")
            if ts is None or bpm is None:
                continue
            try:
                # Oura returns ISO-8601 with offset
                parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            samples.append((parsed, float(bpm)))
        return samples


# ---------------------------------------------------------------------------
# Raw (per-source) point builders
# ---------------------------------------------------------------------------


def _raw_tags(database_name: str) -> dict[str, str]:
    return {"Device": OURA_DEVICE_NAME, "Database_Name": database_name, "Source": "Oura"}


def _iso(ts: datetime | str | None, fallback: str) -> str:
    if ts is None:
        return fallback
    if isinstance(ts, str):
        return ts
    return ts.astimezone(pytz.utc).isoformat()


def build_raw_oura_points(
    *,
    database_name: str,
    date_str: str,
    daily_sleep: dict | None,
    sleep_detail: dict | None,
    daily_activity: dict | None,
    daily_readiness: dict | None,
    hr_samples: list[tuple[datetime, float]] | None,
    vo2_max: dict | None = None,
    workouts: list | None = None,
    daily_spo2: dict | None = None,
    daily_stress: dict | None = None,
    enhanced_tags: list | None = None,
) -> list[dict[str, Any]]:
    """Emit the per-source ``Oura*`` measurements."""
    out: list[dict[str, Any]] = []
    tags = _raw_tags(database_name)
    day_ts = f"{date_str}T12:00:00+00:00"

    if sleep_detail:
        # Field names mirror the Oura API verbatim so audit queries map 1:1
        # to what the app shows:
        #   "Time Asleep"     -> total_sleep_duration
        #   "Total duration"  -> time_in_bed
        #   "Awake"           -> awake_time
        #   "Deep/Light/REM"  -> *_sleep_duration
        fields = {
            k: sleep_detail.get(k)
            for k in (
                "total_sleep_duration",
                "time_in_bed",
                "deep_sleep_duration",
                "light_sleep_duration",
                "rem_sleep_duration",
                "awake_time",
                "efficiency",
                "latency",
                "average_heart_rate",
                "lowest_heart_rate",
                "average_hrv",
                "average_breath",
                "restless_periods",
            )
            if sleep_detail.get(k) is not None
        }
        if daily_sleep and daily_sleep.get("score") is not None:
            fields["sleep_score"] = daily_sleep["score"]
        if fields:
            # Stamp at noon UTC of Oura's `day` (the wake-up calendar day the
            # app attributes the sleep to) rather than bedtime_end. Grafana's
            # GROUP BY time(1d) buckets in UTC, so a point stamped at a local
            # wake-up time near midnight can land in the wrong day bucket for
            # users outside UTC. Noon-UTC keeps the point in the same calendar
            # day as the Oura app regardless of the user's timezone, matching
            # the convention already used for daily_activity/readiness/spo2.
            out.append(
                {
                    "measurement": "OuraSleep",
                    "time": day_ts,
                    "tags": tags,
                    "fields": fields,
                }
            )

    if daily_activity:
        fields = {
            k: daily_activity.get(k)
            for k in (
                "steps",
                "active_calories",
                "total_calories",
                "equivalent_walking_distance",
                "high_activity_time",
                "medium_activity_time",
                "low_activity_time",
                "non_wear_time",
                "resting_time",
                "sedentary_time",
                "score",
            )
            if daily_activity.get(k) is not None
        }
        if fields:
            out.append(
                {
                    "measurement": "OuraDailyActivity",
                    "time": day_ts,
                    "tags": tags,
                    "fields": fields,
                }
            )

    if daily_readiness:
        fields = {k: daily_readiness.get(k) for k in ("score", "temperature_deviation", "temperature_trend_deviation") if daily_readiness.get(k) is not None}
        if fields:
            out.append(
                {
                    "measurement": "OuraReadiness",
                    "time": day_ts,
                    "tags": tags,
                    "fields": fields,
                }
            )

    if hr_samples:
        for ts, bpm in hr_samples:
            out.append(
                {
                    "measurement": "OuraHRIntraday",
                    "time": _iso(ts, day_ts),
                    "tags": tags,
                    "fields": {"heartRate": float(bpm)},
                }
            )

    if vo2_max and vo2_max.get("vo2_max") is not None:
        out.append(
            {
                "measurement": "OuraVO2Max",
                "time": day_ts,
                "tags": tags,
                "fields": {"vo2_max": float(vo2_max["vo2_max"])},
            }
        )

    if daily_spo2:
        spo2_raw = daily_spo2.get("spo2_percentage")
        if isinstance(spo2_raw, dict):
            spo2_avg = spo2_raw.get("average")
        elif isinstance(spo2_raw, int | float):
            spo2_avg = float(spo2_raw)
        else:
            spo2_avg = None
        if spo2_avg is not None:
            fields: dict[str, Any] = {"spo2_avg": float(spo2_avg)}
            # breathing_disturbance_index is sometimes reported alongside SpO2.
            bdi = daily_spo2.get("breathing_disturbance_index")
            if bdi is not None:
                fields["breathing_disturbance_index"] = float(bdi)
            out.append(
                {
                    "measurement": "OuraSpO2",
                    "time": day_ts,
                    "tags": tags,
                    "fields": fields,
                }
            )

    if daily_stress:
        fields = {
            k: daily_stress.get(k)
            for k in ("stress_high", "recovery_high", "day_summary")
            if daily_stress.get(k) is not None
        }
        if fields:
            out.append(
                {
                    "measurement": "OuraStress",
                    "time": day_ts,
                    "tags": tags,
                    "fields": fields,
                }
            )

    if workouts:
        for w in workouts:
            if not isinstance(w, dict):
                continue
            start = w.get("start_datetime") or day_ts
            fields = {
                k: w.get(k)
                for k in (
                    "calories",
                    "distance",
                    "load",
                    "average_heart_rate",
                    "max_heart_rate",
                )
                if w.get(k) is not None
            }
            if w.get("intensity"):
                fields["intensity"] = w["intensity"]
            if not fields:
                continue
            workout_tags = {**tags}
            if w.get("activity"):
                workout_tags["Activity"] = w["activity"]
            out.append(
                {
                    "measurement": "OuraWorkout",
                    "time": _iso(start, day_ts),
                    "tags": workout_tags,
                    "fields": fields,
                }
            )

    if enhanced_tags:
        # Emit one OuraTags point per logged user event so Grafana annotation
        # queries can surface them as vertical lines on any timeseries panel.
        # Each point's `text` field is what Grafana renders in the annotation
        # tooltip; the tag_type_code + comment go into secondary fields.
        for t in enhanced_tags:
            if not isinstance(t, dict):
                continue
            text = t.get("text") or t.get("tag_type_code")
            if not text:
                continue
            fields = {"text": str(text)}
            if t.get("comment"):
                fields["comment"] = str(t["comment"])
            if t.get("tag_type_code"):
                fields["tag_type_code"] = str(t["tag_type_code"])
            # Use start_time if present (some tags are timed), otherwise noon
            # of the day so the annotation lands mid-panel.
            ts = t.get("start_time") or day_ts
            out.append(
                {
                    "measurement": "OuraTags",
                    "time": _iso(ts, day_ts) if not isinstance(ts, str) else ts,
                    "tags": tags,
                    "fields": fields,
                }
            )

    return out


# ---------------------------------------------------------------------------
# Top-level daily fetcher
# ---------------------------------------------------------------------------


def fetch_day(
    client: OuraClient,
    date_str: str,
    database_name: str,
    include_intraday_hr: bool = True,
) -> list[dict[str, Any]]:
    """
    Fetch everything Oura has for ``date_str`` and return a combined list
    of InfluxDB point dicts covering both raw ``Oura*`` measurements and the
    Unified* mirrors.
    """
    try:
        daily_sleep = client.get_daily_sleep(date_str)
    except Exception as err:  # noqa: BLE001
        _log.warning("Oura daily_sleep fetch failed for %s: %s", date_str, err)
        daily_sleep = None
    try:
        sleep_detail = client.get_sleep_detail(date_str)
    except Exception as err:  # noqa: BLE001
        _log.warning("Oura sleep fetch failed for %s: %s", date_str, err)
        sleep_detail = None
    try:
        daily_activity = client.get_daily_activity(date_str)
    except Exception as err:  # noqa: BLE001
        _log.warning("Oura daily_activity fetch failed for %s: %s", date_str, err)
        daily_activity = None
    try:
        daily_readiness = client.get_daily_readiness(date_str)
    except Exception as err:  # noqa: BLE001
        _log.warning("Oura daily_readiness fetch failed for %s: %s", date_str, err)
        daily_readiness = None

    hr_samples: list[tuple[datetime, float]] | None = None
    if include_intraday_hr:
        try:
            hr_samples = client.get_heartrate_intraday(date_str)
        except Exception as err:  # noqa: BLE001
            _log.warning("Oura heartrate fetch failed for %s: %s", date_str, err)
            hr_samples = None

    try:
        vo2_max = client.get_vo2_max(date_str)
    except Exception as err:  # noqa: BLE001
        # 404 is expected for users whose ring doesn't have VO2 max yet; log quietly.
        _log.debug("Oura vO2_max fetch skipped for %s: %s", date_str, err)
        vo2_max = None
    try:
        workouts = client.get_workouts(date_str)
    except Exception as err:  # noqa: BLE001
        _log.warning("Oura workouts fetch failed for %s: %s", date_str, err)
        workouts = None
    try:
        daily_spo2 = client.get_daily_spo2(date_str)
    except Exception as err:  # noqa: BLE001
        _log.debug("Oura daily_spo2 fetch skipped for %s: %s", date_str, err)
        daily_spo2 = None
    try:
        daily_stress = client.get_daily_stress(date_str)
    except Exception as err:  # noqa: BLE001
        _log.debug("Oura daily_stress fetch skipped for %s: %s", date_str, err)
        daily_stress = None
    try:
        enhanced_tags = client.get_enhanced_tags(date_str)
    except Exception as err:  # noqa: BLE001
        _log.debug("Oura enhanced_tag fetch skipped for %s: %s", date_str, err)
        enhanced_tags = None

    points: list[dict[str, Any]] = []
    points.extend(
        build_raw_oura_points(
            database_name=database_name,
            date_str=date_str,
            daily_sleep=daily_sleep,
            sleep_detail=sleep_detail,
            daily_activity=daily_activity,
            daily_readiness=daily_readiness,
            hr_samples=hr_samples,
            vo2_max=vo2_max,
            workouts=workouts,
            daily_spo2=daily_spo2,
            daily_stress=daily_stress,
            enhanced_tags=enhanced_tags,
        )
    )

    points.extend(
        unified_schema.oura_to_unified(
            date_str=date_str,
            device_name=OURA_DEVICE_NAME,
            database_name=database_name,
            daily_sleep=daily_sleep,
            sleep_detail=sleep_detail,
            daily_activity=daily_activity,
            daily_readiness=daily_readiness,
            vo2_max=vo2_max,
            workouts=workouts,
            daily_spo2=daily_spo2,
            daily_stress=daily_stress,
        )
    )

    if hr_samples:
        points.extend(
            unified_schema.unified_hr_intraday_points(
                source=unified_schema.SOURCE_OURA,
                device=OURA_DEVICE_NAME,
                database_name=database_name,
                samples=hr_samples,
            )
        )

    _log.info(
        "Oura: %d points for %s (sleep=%s activity=%s readiness=%s vo2=%s "
        "workouts=%d spo2=%s stress=%s tags=%d hr_samples=%d)",
        len(points),
        date_str,
        bool(sleep_detail),
        bool(daily_activity),
        bool(daily_readiness),
        bool(vo2_max),
        len(workouts or []),
        bool(daily_spo2),
        bool(daily_stress),
        len(enhanced_tags or []),
        len(hr_samples or []),
    )
    return points
