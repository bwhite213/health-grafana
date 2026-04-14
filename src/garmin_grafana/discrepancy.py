"""
Cross-source discrepancy detection.

After each fetch cycle the orchestrator queries the Unified* measurements for
the last N days, finds metrics that have values from more than one source on
the same day, and writes pairwise diff records into the ``SourceDiscrepancy``
measurement for the Grafana dashboard to visualize.

This module is intentionally query-tool agnostic — the caller passes a
callable that returns rows, so both InfluxDB v1 and v3 are supported without
importing either client here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from itertools import combinations
from typing import Any, Callable

import pytz

_log = logging.getLogger(__name__)

# Metrics we compare across sources. Each entry: (unified_measurement, field,
# human_label). Adding new metrics is additive — no dashboard change required
# beyond the metric dropdown.
COMPARED_METRICS: list[tuple[str, str, str]] = [
    ("UnifiedSleep", "duration_s", "sleep_duration_s"),
    ("UnifiedSleep", "hrv_avg", "sleep_hrv_avg"),
    ("UnifiedSleep", "rhr", "sleep_rhr"),
    ("UnifiedHeartRate", "rhr", "daily_rhr"),
    ("UnifiedActivity", "steps", "steps"),
    ("UnifiedActivity", "calories_active", "active_calories"),
    ("UnifiedReadiness", "score", "readiness_score"),
]


QueryFn = Callable[[str, str, str, str], dict[str, float]]
# Signature: (measurement, field, start_iso, end_iso) -> {source: value}


def compute_discrepancy_points(
    *,
    query_daily_by_source: QueryFn,
    database_name: str,
    lookback_days: int = 7,
    today: datetime | None = None,
) -> list[dict[str, Any]]:
    """
    Build ``SourceDiscrepancy`` points by diffing each metric's values across
    sources for each day in the lookback window.

    ``query_daily_by_source`` must execute something equivalent to:

        SELECT mean({field}) FROM "{measurement}"
        WHERE time >= '{start_iso}' AND time < '{end_iso}'
        GROUP BY "Source"

    and return a ``{source: value}`` dict (values must be numeric; missing
    sources omitted).
    """
    today = today or datetime.now(tz=pytz.utc)
    points: list[dict[str, Any]] = []

    for day_offset in range(lookback_days):
        day = (today - timedelta(days=day_offset)).date()
        start = datetime(day.year, day.month, day.day, tzinfo=pytz.utc)
        end = start + timedelta(days=1)
        start_iso = start.isoformat()
        end_iso = end.isoformat()

        for measurement, field, label in COMPARED_METRICS:
            try:
                per_source = query_daily_by_source(measurement, field, start_iso, end_iso)
            except Exception as err:  # noqa: BLE001
                _log.warning(
                    "Discrepancy query failed for %s.%s on %s: %s",
                    measurement,
                    field,
                    day,
                    err,
                )
                continue
            # Drop Nones and non-numeric; need >=2 sources to compare
            numeric = {k: float(v) for k, v in per_source.items() if v is not None}
            if len(numeric) < 2:
                continue

            for source_a, source_b in combinations(sorted(numeric.keys()), 2):
                va = numeric[source_a]
                vb = numeric[source_b]
                abs_diff = abs(va - vb)
                denom = max(abs(va), abs(vb), 1e-9)
                pct_diff = round(100.0 * abs_diff / denom, 3)
                points.append(
                    {
                        "measurement": "SourceDiscrepancy",
                        "time": start.isoformat(),
                        "tags": {
                            "Database_Name": database_name,
                            "Metric": label,
                            "Source_A": source_a,
                            "Source_B": source_b,
                        },
                        "fields": {
                            "value_a": va,
                            "value_b": vb,
                            "abs_diff": round(abs_diff, 3),
                            "pct_diff": pct_diff,
                        },
                    }
                )

    _log.info(
        "Discrepancy detector produced %d points over %d-day window",
        len(points),
        lookback_days,
    )
    return points


# ---------------------------------------------------------------------------
# Default InfluxDB v1 query function factory
# ---------------------------------------------------------------------------


def make_influxdb_v1_query_fn(influxdb_client) -> QueryFn:
    """
    Return a ``query_daily_by_source`` function bound to an InfluxDB v1 client.
    The client must already have its database selected (``switch_database``).
    """

    def _fn(measurement: str, field: str, start_iso: str, end_iso: str) -> dict[str, float]:
        q = (
            f'SELECT mean("{field}") AS v FROM "{measurement}" '
            f"WHERE time >= '{start_iso}' AND time < '{end_iso}' "
            f'GROUP BY "Source"'
        )
        result = influxdb_client.query(q)
        out: dict[str, float] = {}
        # result.items() yields ((measurement, tags), generator_of_rows)
        for (meas, tags), rows in result.items():
            source = (tags or {}).get("Source")
            if not source:
                continue
            for row in rows:
                v = row.get("v")
                if v is not None:
                    out[source] = v
        return out

    return _fn


def make_influxdb_v3_query_fn(influxdb_client) -> QueryFn:
    """
    Return a ``query_daily_by_source`` function bound to an InfluxDB v3 client.
    Uses InfluxQL for cross-compat with v1 schemas.
    """

    def _fn(measurement: str, field: str, start_iso: str, end_iso: str) -> dict[str, float]:
        q = (
            f'SELECT mean("{field}") AS v FROM "{measurement}" '
            f"WHERE time >= '{start_iso}' AND time < '{end_iso}' "
            f'GROUP BY "Source"'
        )
        rows = influxdb_client.query(query=q, language="influxql").to_pylist()
        out: dict[str, float] = {}
        for row in rows:
            source = row.get("Source")
            v = row.get("v")
            if source and v is not None:
                out[source] = v
        return out

    return _fn
