# CLAUDE.md

Guidance for Claude Code (and any LLM assistant) working in this repository.

## What this project is

`health-grafana` pulls personal health data from multiple wearable sources
into a local InfluxDB database and visualizes it in Grafana. Historically it
was Garmin-only (a fork of
[garmin-grafana](https://github.com/arpanghosh8453/garmin-grafana)); it has
been extended to also ingest **Oura Ring** data and to produce a cross-source
"unified" view with discrepancy detection.

The goal is a single local dashboard showing every metric from every device
the user owns, with the ability to:

- view each source individually
- overlay multiple sources on the same graph
- compute an average/unified value across all available sources per day
- flag discrepancies between sources (e.g., Garmin says 7h sleep, Oura says 6h)

Apple Watch / HealthKit is **not yet** supported — the architecture leaves a
clean extension point (see "Adding a new source" below).

## Stack

- **Language**: Python 3.13, managed with `uv`
- **Storage**: InfluxDB (v1 recommended; v3 supported)
- **Visualization**: Grafana with auto-provisioned datasource and dashboards
- **Runtime**: Docker Compose (3 services: fetcher, influxdb, grafana)

## Repository layout

```
src/garmin_grafana/
├── __init__.py              # Package entry point → orchestrator.main
├── orchestrator.py          # Top-level fetch loop (Garmin + Oura + discrepancy)
├── garmin_fetch.py          # Garmin Connect fetcher (original, ~1600 lines)
├── garmin_bulk_importer.py  # Bulk import from Garmin account export ZIPs
├── fit_activity_importer.py # Local FIT file parser
├── influxdb_exporter.py     # Export InfluxDB data to CSV
├── discrepancy.py           # Cross-source diff computation
└── sources/
    ├── __init__.py
    ├── oura_fetch.py        # Oura Cloud API v2 client + daily fetcher
    └── unified_schema.py    # Normalizers + point builders for Unified* measurements

Grafana_Dashboard/
├── Garmin-Grafana-Dashboard.json          # Original curated Garmin dashboard
├── Multi-Source-Health-Dashboard.json     # Unified multi-source dashboard
└── *.yaml                                  # Dashboard provisioning config

Grafana_Datasource/
└── influxdb.yaml            # Datasource provisioning

docs/
├── manual-import-instructions.md
├── oura-setup.md            # How to get an Oura PAT
└── ubuntu-deployment.md     # Local Ubuntu server install guide

compose-example.yml          # Docker Compose template
Dockerfile                   # Multi-stage Python 3.13 build
pyproject.toml               # uv project config
```

## Storage model

Two layers of measurements live in InfluxDB:

**1. Raw per-source measurements (source of truth)**

- Garmin: `DailyStats`, `SleepSummary`, `HeartRateIntraday`, `StepsIntraday`,
  … (30+ measurements, owned by `garmin_fetch.py` — do not rename or break).
- Oura: `OuraSleep`, `OuraDailyActivity`, `OuraReadiness`, `OuraHRIntraday`.

**2. Unified measurements (cross-source analytics layer)**

Every source also writes to a normalized set of measurements tagged by
`Source`. These are what the Multi-Source Health dashboard queries.

| Measurement           | Tags                          | Key fields                                                                     |
| --------------------- | ----------------------------- | ------------------------------------------------------------------------------ |
| `UnifiedSleep`        | `Source`, `Device`, `User_ID` | `duration_s`, `deep_s`, `light_s`, `rem_s`, `awake_s`, `hrv_avg`, `rhr`, `efficiency`, `score` |
| `UnifiedHeartRate`    | `Source`, `Device`, `User_ID` | `rhr`, `hr_avg`, `hr_max`, `hr_min`                                            |
| `UnifiedHRIntraday`   | `Source`, `Device`, `User_ID` | `hr`                                                                           |
| `UnifiedActivity`     | `Source`, `Device`, `User_ID` | `steps`, `calories_active`, `calories_total`, `distance_m`, `active_minutes`   |
| `UnifiedReadiness`    | `Source`, `Device`, `User_ID` | `score` (0–100: Oura Readiness, Garmin Body Battery, etc.)                     |
| `SourceDiscrepancy`   | `Metric`, `Source_A`, `Source_B` | `value_a`, `value_b`, `abs_diff`, `pct_diff`                                |

`Source` values currently used: `Garmin`, `Oura`. Future: `Apple`.

Averaging across sources is done **at query time** in Grafana (`SELECT
mean(...) FROM "UnifiedSleep" WHERE time > now() - 30d GROUP BY time(1d)`),
not by pre-computing a "merged" series. This makes missing-source days
graceful and lets the user toggle which sources count without code changes.

## Configuration

All config is via environment variables. Local overrides go in
`override-default-vars.env` (gitignored). The new variables added for
multi-source support:

| Variable                         | Purpose                                                   |
| -------------------------------- | --------------------------------------------------------- |
| `OURA_PERSONAL_ACCESS_TOKEN`     | Oura Cloud API v2 PAT (from cloud.ouraring.com)           |
| `OURA_ENABLED`                   | Set `False` to skip Oura fetching (default `True` if token set) |
| `ENABLE_UNIFIED_MEASUREMENTS`    | Write `Unified*` mirrors (default `True`)                 |
| `DISCREPANCY_LOOKBACK_DAYS`      | Days to recompute discrepancies each cycle (default `7`)  |
| `FETCH_SELECTION`                | Existing Garmin selector; unaffected by Oura              |

Existing Garmin / InfluxDB variables (`GARMINCONNECT_*`, `INFLUXDB_*`,
`UPDATE_INTERVAL_SECONDS`, `USER_TIMEZONE`, `TAG_MEASUREMENTS_WITH_USER_EMAIL`)
are reused unchanged.

## Running locally (dev)

```bash
# One-time setup
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --locked

# Run the orchestrator
uv run python -m garmin_grafana.orchestrator
```

The orchestrator expects InfluxDB reachable at `$INFLUXDB_HOST:$INFLUXDB_PORT`.
For dev it's easiest to start just the InfluxDB + Grafana containers via
`docker compose up influxdb grafana` and point the local Python process at
`localhost`.

## Running in production (user's Ubuntu server)

See `docs/ubuntu-deployment.md`. Summary: `docker compose up -d` inside a
cloned repo with `override-default-vars.env` populated.

## Adding a new data source

Follow the Oura pattern:

1. Create `src/garmin_grafana/sources/<newsource>_fetch.py` that exposes:
   - `get_daily_points(date_str) -> list[dict]`  (raw `<NewSource>*` points)
   - `get_unified_points(date_str) -> list[dict]` (calls into `unified_schema`)
2. Add env vars (`<NEWSOURCE>_API_TOKEN`, `<NEWSOURCE>_ENABLED`) with defaults.
3. In `unified_schema.py`, add a normalizer function that maps the source's
   native fields into the unified schema and stamps `Source=<NewSource>`.
4. Register the fetcher in `orchestrator.py` so it runs each cycle.
5. Add the new `Source` value to the dashboard `source` variable's options
   in `Multi-Source-Health-Dashboard.json`.
6. Update `discrepancy.py`'s source list if you want pairwise diffs against
   the new source.
7. Update this file and `README.md` with the new source.

## Things to preserve

- **`garmin_fetch.py` stability**: The original ~1600-line Garmin fetcher is
  battle-tested. When touching it, make *additive* changes only (e.g., emit
  unified points after each successful raw write). Do not refactor.
- **Original Garmin dashboard**: `Garmin-Grafana-Dashboard.json` is left
  untouched so upstream updates can be merged cleanly.
- **Backward-compat env vars**: Never rename an existing `GARMIN*` or
  `INFLUXDB*` variable.
- **Docker user**: Fetcher runs as UID/GID 1000. If you mount token storage,
  it must be owned by 1000:1000.

## Development workflow for assistants

- Before editing, read the file — do not guess.
- Use the `sources/` package for new fetchers; do not cram new providers into
  `garmin_fetch.py`.
- Unified-schema helpers live in `sources/unified_schema.py`. Reuse them
  rather than duplicating point-building logic.
- Commit with clear messages. The `claude/add-wearable-device-support-*`
  branch is the current working branch for multi-source work.
