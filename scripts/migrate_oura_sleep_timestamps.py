#!/usr/bin/env python3
"""
One-off migration: wipe raw Oura* measurements in InfluxDB so the orchestrator
can re-backfill them from the Oura Cloud API under the current (noon-UTC)
timestamp convention.

Background
----------
Early OuraSleep points were stamped at ``bedtime_end`` (local wake time),
which buckets into the wrong UTC day for users outside UTC and caused
``last()`` queries on Grafana stat panels to return stale or wrong values
depending on refresh timing. The fetcher now stamps every daily Oura*
measurement at noon UTC of the record's ``day`` field, matching the
Oura app's day attribution. This script removes legacy points so the
next backfill writes a clean single-row-per-day history.

It also drops per-day measurements whose schema has grown (e.g. OuraSleep
picked up average_heart_rate / lowest_heart_rate / average_hrv /
average_breath / restless_periods / latency / sleep_score fields) so the
backfilled rows don't mix with partial legacy rows.

Procedure
---------
1. Stop the fetcher so it doesn't race with deletes::

       docker compose stop health-fetch-data

2. Dry-run to see row counts::

       uv run python scripts/migrate_oura_sleep_timestamps.py --dry-run

3. Execute::

       uv run python scripts/migrate_oura_sleep_timestamps.py --yes

   By default all raw daily Oura measurements are dropped:
   OuraSleep, OuraReadiness, OuraDailyActivity, OuraSpO2, OuraStress,
   OuraVO2Max. OuraHRIntraday and OuraWorkout/OuraTags are NOT touched
   by default (they aren't affected by the timestamp bug). Pass
   ``--measurement`` to override the list.

4. Backfill: in ``override-default-vars.env`` temporarily set
   ``MANUAL_START_DATE`` / ``MANUAL_END_DATE`` to the window you want
   (e.g. ``2026-01-01`` → today) and start the fetcher::

       docker compose up health-fetch-data

   When the bulk run finishes (it exits on its own per
   ``orchestrator.py``'s MANUAL_* branch), unset the MANUAL_* vars and
   restart the container to resume continuous fetching.

InfluxDB v3 is not supported by this script — v3 uses a different delete
API. On v3, issue the equivalent ``DROP TABLE`` / SQL delete via your
v3 tooling.

Environment variables (same as the main fetcher):
    INFLUXDB_HOST, INFLUXDB_PORT, INFLUXDB_USERNAME, INFLUXDB_PASSWORD,
    INFLUXDB_DATABASE, INFLUXDB_VERSION, INFLUXDB_ENDPOINT_IS_HTTP
"""

from __future__ import annotations

import argparse
import os
import sys

from influxdb import InfluxDBClient
from influxdb.exceptions import InfluxDBClientError

DEFAULT_MEASUREMENTS = [
    "OuraSleep",
    "OuraReadiness",
    "OuraDailyActivity",
    "OuraSpO2",
    "OuraStress",
    "OuraVO2Max",
]


def _connect() -> InfluxDBClient:
    version = os.getenv("INFLUXDB_VERSION", "1")
    if version != "1":
        print(
            f"ERROR: INFLUXDB_VERSION={version}; this script only supports v1.",
            file=sys.stderr,
        )
        sys.exit(2)

    host = os.getenv("INFLUXDB_HOST")
    port = int(os.getenv("INFLUXDB_PORT", "8086"))
    username = os.getenv("INFLUXDB_USERNAME")
    password = os.getenv("INFLUXDB_PASSWORD")
    database = os.getenv("INFLUXDB_DATABASE", "GarminStats")
    use_http = os.getenv("INFLUXDB_ENDPOINT_IS_HTTP", "True").lower() not in (
        "false",
        "f",
        "no",
        "0",
    )

    if not host or not username or not password:
        print(
            "ERROR: INFLUXDB_HOST / INFLUXDB_USERNAME / INFLUXDB_PASSWORD must be set.",
            file=sys.stderr,
        )
        sys.exit(2)

    client = InfluxDBClient(
        host=host,
        port=port,
        username=username,
        password=password,
        ssl=not use_http,
        verify_ssl=not use_http,
    )
    client.switch_database(database)
    return client


def _count(client: InfluxDBClient, measurement: str) -> int:
    """Return total point count for ``measurement``, or 0 if missing."""
    try:
        result = client.query(f'SELECT count(*) FROM "{measurement}"')
    except InfluxDBClientError as err:
        # Measurement not present — InfluxDB returns empty, but some errors
        # (e.g. measurement not found) surface as exceptions on older clients.
        print(f"  ! query failed for {measurement}: {err}", file=sys.stderr)
        return 0
    total = 0
    for series in result.raw.get("series", []) or []:
        # count(*) returns one row per field — sum across fields to get
        # a rough total. (A single daily measurement with N fields reports
        # N × rows; we print the max across fields as "rows".)
        cols = series.get("columns", [])
        for row in series.get("values", []) or []:
            for col, val in zip(cols, row, strict=False):
                if col == "time":
                    continue
                if isinstance(val, int | float) and val > total:
                    total = int(val)
    return total


def _drop(client: InfluxDBClient, measurement: str) -> None:
    client.query(f'DROP MEASUREMENT "{measurement}"')


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Drop raw Oura* measurements in InfluxDB to prepare for a clean "
            "re-backfill. See the module docstring for the full procedure."
        ),
    )
    parser.add_argument(
        "--measurement",
        action="append",
        dest="measurements",
        metavar="NAME",
        help=(
            "Measurement to drop. May be passed multiple times. "
            f"Default: {', '.join(DEFAULT_MEASUREMENTS)}"
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Show row counts but don't drop anything.",
    )
    group.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the drop. Without this (and without --dry-run) the "
        "script prints the plan and exits without modifying InfluxDB.",
    )
    args = parser.parse_args()

    measurements = args.measurements or DEFAULT_MEASUREMENTS

    client = _connect()

    print(
        f"Target InfluxDB: {os.getenv('INFLUXDB_HOST')}:"
        f"{os.getenv('INFLUXDB_PORT', '8086')} "
        f"db={os.getenv('INFLUXDB_DATABASE', 'GarminStats')}"
    )
    print("Measurements:")
    totals: dict[str, int] = {}
    for m in measurements:
        n = _count(client, m)
        totals[m] = n
        print(f"  {m:<22} rows={n}")

    if args.dry_run:
        print("\n--dry-run: no changes made.")
        return 0

    if not args.yes:
        print(
            "\nNothing dropped. Re-run with --yes to execute, or --dry-run to "
            "see counts again."
        )
        return 0

    print("\nDropping measurements...")
    for m in measurements:
        try:
            _drop(client, m)
            print(f"  dropped {m}")
        except InfluxDBClientError as err:
            print(f"  ! failed to drop {m}: {err}", file=sys.stderr)
            return 1

    print(
        "\nDone. Next: set MANUAL_START_DATE / MANUAL_END_DATE in "
        "override-default-vars.env and start the fetcher to backfill."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
