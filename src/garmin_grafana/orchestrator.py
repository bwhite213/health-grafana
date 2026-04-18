"""
Multi-source health orchestrator.

Wraps the existing Garmin fetch loop and adds:

1. An Oura Ring fetch step that runs alongside each Garmin cycle.
2. Unified-schema mirroring (handled inside ``garmin_fetch.daily_fetch_write``
   via the ``unified_schema`` import it picked up).
3. A post-cycle discrepancy-detection pass that writes ``SourceDiscrepancy``
   points across all sources present.

The original ``garmin_fetch.py`` ``__main__`` block is intentionally
preserved for standalone operation — running ``python -m
garmin_grafana.orchestrator`` is the new preferred entry point, but
``python -m garmin_grafana.garmin_fetch`` still works as before.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytz

# Importing garmin_fetch is what actually connects to InfluxDB, logs in to
# Garmin, and defines the per-day fetch functions. We intentionally reuse
# its module-level state rather than duplicating it.
from . import discrepancy, garmin_fetch, health_summary, normal_ranges
from .sources import oura_fetch

_log = logging.getLogger(__name__)

OURA_TOKEN = os.getenv("OURA_PERSONAL_ACCESS_TOKEN", "").strip()
OURA_ENABLED = os.getenv("OURA_ENABLED", "True" if OURA_TOKEN else "False").lower() in (
    "true",
    "1",
    "yes",
    "t",
    "y",
)
DISCREPANCY_LOOKBACK_DAYS = int(os.getenv("DISCREPANCY_LOOKBACK_DAYS", "7"))

# Dead-man's switch. Set to a healthchecks.io check URL (or compatible).
# Orchestrator hits "${URL}/start" at cycle begin, the bare URL at cycle end,
# and "${URL}/fail" on unhandled exceptions. If no ping arrives within the
# check's grace window, healthchecks.io notifies the configured channels.
HEALTHCHECK_PING_URL = os.getenv("HEALTHCHECK_PING_URL", "").strip().rstrip("/")

# Docker HEALTHCHECK (see Dockerfile) reads this file's mtime to decide
# whether the loop is alive. Touching it is the in-container equivalent of
# the external healthchecks.io ping.
HEARTBEAT_FILE = Path(os.getenv("FETCHER_HEARTBEAT_FILE", "/tmp/fetcher_heartbeat"))


def _touch_heartbeat() -> None:
    try:
        HEARTBEAT_FILE.touch()
    except OSError as err:  # noqa: BLE001
        _log.warning("Failed to update heartbeat file %s: %s", HEARTBEAT_FILE, err)


def _ping_healthcheck(suffix: str = "") -> None:
    """Fire a best-effort HTTP GET at the configured dead-man's switch URL.

    suffix: "", "/start", or "/fail" (healthchecks.io convention). Silent
    on failure — a transient network blip must not break the fetch loop.
    """
    if not HEALTHCHECK_PING_URL:
        return
    url = f"{HEALTHCHECK_PING_URL}{suffix}"
    try:
        with urllib.request.urlopen(url, timeout=10):
            pass
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        _log.debug("Healthcheck ping to %s failed: %s", url, err)


def _build_oura_client() -> oura_fetch.OuraClient | None:
    if not OURA_ENABLED:
        _log.info("Oura integration disabled (OURA_ENABLED=False or no token)")
        return None
    if not OURA_TOKEN:
        _log.warning(
            "OURA_ENABLED is True but OURA_PERSONAL_ACCESS_TOKEN is empty — skipping Oura"
        )
        return None
    try:
        return oura_fetch.OuraClient(OURA_TOKEN)
    except Exception as err:  # noqa: BLE001
        _log.error("Failed to initialize Oura client: %s", err)
        return None


def _oura_fetch_range(client: oura_fetch.OuraClient, start_date: str, end_date: str) -> None:
    """Fetch Oura data for every day in ``[start_date, end_date]`` inclusive."""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if start > end:
        start, end = end, start
    day = end
    while day >= start:
        date_str = day.strftime("%Y-%m-%d")
        try:
            points = oura_fetch.fetch_day(
                client=client,
                date_str=date_str,
                database_name=garmin_fetch.INFLUXDB_DATABASE,
            )
            if points:
                garmin_fetch.write_points_to_influxdb(points)
        except Exception as err:  # noqa: BLE001
            _log.warning("Oura fetch failed for %s: %s", date_str, err)
        day -= timedelta(days=1)


def _run_discrepancy_pass() -> None:
    try:
        if garmin_fetch.INFLUXDB_VERSION == "1":
            query_fn = discrepancy.make_influxdb_v1_query_fn(garmin_fetch.influxdbclient)
        else:
            query_fn = discrepancy.make_influxdb_v3_query_fn(garmin_fetch.influxdbclient)

        points = discrepancy.compute_discrepancy_points(
            query_daily_by_source=query_fn,
            database_name=garmin_fetch.INFLUXDB_DATABASE,
            lookback_days=DISCREPANCY_LOOKBACK_DAYS,
        )
        if points:
            garmin_fetch.write_points_to_influxdb(points)
    except Exception as err:  # noqa: BLE001
        _log.warning("Discrepancy pass failed: %s", err)


def _run_health_summaries() -> None:
    try:
        health_summary.generate_summaries(garmin_fetch.influxdbclient)
    except Exception as err:  # noqa: BLE001
        _log.warning("AI health summary generation failed: %s", err)


DASHBOARD_DIR = os.getenv("DASHBOARD_DIR", "/app/Grafana_Dashboard")


def main() -> None:
    normal_ranges.stamp_dashboards_from_env(DASHBOARD_DIR)

    oura_client = _build_oura_client()

    # --- Login to Garmin (reuses the existing flow) ---
    garmin_fetch.garmin_obj = garmin_fetch.garmin_login()

    # --- Manual bulk mode: run once across a date range, then exit ---
    if garmin_fetch.MANUAL_START_DATE:
        garmin_fetch.fetch_write_bulk(
            garmin_fetch.MANUAL_START_DATE, garmin_fetch.MANUAL_END_DATE
        )
        if oura_client is not None:
            _oura_fetch_range(
                oura_client, garmin_fetch.MANUAL_START_DATE, garmin_fetch.MANUAL_END_DATE
            )
        _run_discrepancy_pass()
        _run_health_summaries()
        _log.info(
            "Bulk update success : fetched all available health metrics for %s to %s",
            garmin_fetch.MANUAL_START_DATE,
            garmin_fetch.MANUAL_END_DATE,
        )
        return

    # --- Continuous mode: mirror garmin_fetch.py's loop, adding Oura + discrepancy ---
    try:
        if garmin_fetch.INFLUXDB_VERSION == "1":
            last_row = list(
                garmin_fetch.influxdbclient.query(
                    "SELECT * FROM HeartRateIntraday ORDER BY time DESC LIMIT 1"
                ).get_points()
            )[0]
            last_influxdb_sync_time_UTC = pytz.utc.localize(
                datetime.strptime(last_row["time"], "%Y-%m-%dT%H:%M:%SZ")
            )
        else:
            last_row = garmin_fetch.influxdbclient.query(
                query="SELECT * FROM HeartRateIntraday ORDER BY time DESC LIMIT 1",
                language="influxql",
            ).to_pylist()[0]
            last_influxdb_sync_time_UTC = pytz.utc.localize(last_row["time"])
    except Exception as err:  # noqa: BLE001
        _log.warning(
            "No previously synced data found in local InfluxDB (%s). "
            "Defaulting to 7-day initial fetch — set MANUAL_START_DATE to bulk backfill.",
            err,
        )
        last_influxdb_sync_time_UTC = (datetime.today() - timedelta(days=7)).astimezone(
            pytz.utc
        )

    try:
        if garmin_fetch.USER_TIMEZONE:
            local_timediff = datetime.now(
                tz=pytz.timezone(garmin_fetch.USER_TIMEZONE)
            ).utcoffset()
        else:
            last_activity = garmin_fetch.garmin_obj.get_last_activity()
            local_timediff = datetime.strptime(
                last_activity["startTimeLocal"], "%Y-%m-%d %H:%M:%S"
            ) - datetime.strptime(last_activity["startTimeGMT"], "%Y-%m-%d %H:%M:%S")
        _log.info("Using user's local timezone offset: %s", local_timediff)
    except (KeyError, TypeError) as err:
        _log.warning(
            "Unable to determine user's timezone (%s). Defaulting to UTC. "
            "Set USER_TIMEZONE to silence this.",
            err,
        )
        local_timediff = timedelta(hours=0)

    while True:
        _ping_healthcheck("/start")
        try:
            last_watch_sync_time_UTC = datetime.fromtimestamp(
                int(garmin_fetch.garmin_obj.get_device_last_used().get("lastUsedDeviceUploadTime") / 1000)
            ).astimezone(pytz.utc)
        except Exception as err:  # noqa: BLE001
            _log.error("Failed to read last device sync time: %s", err)
            _ping_healthcheck("/fail")
            time.sleep(garmin_fetch.UPDATE_INTERVAL_SECONDS)
            continue

        if last_influxdb_sync_time_UTC < last_watch_sync_time_UTC:
            _log.info("Update found : watch sync time is %s UTC", last_watch_sync_time_UTC)
            start_str = (last_influxdb_sync_time_UTC + local_timediff).strftime("%Y-%m-%d")
            end_str = (last_watch_sync_time_UTC + local_timediff).strftime("%Y-%m-%d")
            garmin_fetch.fetch_write_bulk(start_str, end_str)
            if oura_client is not None:
                _oura_fetch_range(oura_client, start_str, end_str)
            _run_discrepancy_pass()
            _run_health_summaries()
            last_influxdb_sync_time_UTC = last_watch_sync_time_UTC
        else:
            _log.info(
                "No new Garmin data found. Watch/InfluxDB sync time: %s UTC",
                last_watch_sync_time_UTC,
            )
            # Still run Oura daily — the user may have worn the ring without the watch.
            if oura_client is not None:
                today_local = (datetime.now(tz=pytz.utc) + local_timediff).strftime(
                    "%Y-%m-%d"
                )
                yday_local = (
                    datetime.now(tz=pytz.utc) + local_timediff - timedelta(days=1)
                ).strftime("%Y-%m-%d")
                _oura_fetch_range(oura_client, yday_local, today_local)
                _run_discrepancy_pass()
                _run_health_summaries()

        _touch_heartbeat()
        _ping_healthcheck()
        _log.info(
            "Waiting for %s seconds before next automatic update",
            garmin_fetch.UPDATE_INTERVAL_SECONDS,
        )
        time.sleep(garmin_fetch.UPDATE_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
