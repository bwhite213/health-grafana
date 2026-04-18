# CLAUDE.md

Guidance for Claude Code (and any LLM assistant) working in this repository.

## What this project is

`health-grafana` pulls personal health data from multiple wearable sources
into a local InfluxDB database and visualizes it in Grafana. Historically it
was Garmin-only (a fork of
[garmin-grafana](https://github.com/arpanghosh8453/garmin-grafana)); it has
been extended to also ingest **Oura Ring** and **Apple Health / HealthKit**
data and to produce a cross-source "unified" view with discrepancy
detection.

The goal is a single local dashboard showing every metric from every device
the user owns, with the ability to:

- view each source individually
- overlay multiple sources on the same graph
- compute an average/unified value across all available sources per day
- flag discrepancies between sources (e.g., Garmin says 7h sleep, Oura says 6h)

Apple Health is ingested one-shot from the iPhone's "Export All Health
Data" zip — there's no live server-side HealthKit API. See
`docs/apple-health-import.md`. Garmin and Oura are pulled continuously
by the orchestrator's fetch loop.

An **optional Home Assistant** service (container `homeassistant`)
provides the near-live alternative: the iOS Companion app streams
HealthKit sensors to HA, which forwards them to a separate
`home_assistant` InfluxDB database on the same InfluxDB instance.
Grafana has a second datasource (`HomeAssistant-InfluxDB`) pre-wired
to that database. See `docs/home-assistant-setup.md`. HA data does
**not** feed the `Unified*` measurements today — it's a parallel
stream with its own schema.

## Stack

- **Language**: Python 3.13, managed with `uv`
- **Storage**: InfluxDB (v1 recommended; v3 supported) — one instance,
  two databases: `GarminStats` (Garmin/Oura/Apple-import) and
  `home_assistant` (HA-streamed live sensors, created on first HA setup).
- **Visualization**: Grafana with two auto-provisioned datasources (one per DB) and dashboards
- **Runtime**: Docker Compose (3 services always + optional `homeassistant`:
  fetcher, influxdb, grafana, homeassistant)

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
| `UnifiedSleep`        | `Source`, `Device`, `User_ID` | `duration_s`, `deep_s`, `light_s`, `rem_s`, `awake_s`, `hrv_avg`, `rhr`, `efficiency`, `score`, `spo2_avg` |
| `UnifiedHeartRate`    | `Source`, `Device`, `User_ID` | `rhr`, `hr_avg`, `hr_max`, `hr_min`                                            |
| `UnifiedHRIntraday`   | `Source`, `Device`, `User_ID` | `hr`                                                                           |
| `UnifiedActivity`     | `Source`, `Device`, `User_ID` | `steps`, `calories_active`, `calories_total`, `distance_m`, `active_minutes`   |
| `UnifiedReadiness`    | `Source`, `Device`, `User_ID` | `score` (0–100: prefers Garmin Training Readiness, falls back to Body Battery; Oura Readiness) |
| `UnifiedVO2Max`       | `Source`, `Device`, `User_ID` | `vo2_max`                                                                      |
| `UnifiedWorkout`      | `Source`, `Device`, `User_ID`, `Activity` | `duration_s`, `calories`, `distance_m`, `hr_avg`, `hr_max`, `intensity` |
| `UnifiedStress`       | `Source`, `Device`, `User_ID` | `stress_high_s`, `stress_avg`, `recovery_high_s`                               |
| `SourceDiscrepancy`   | `Metric`, `Source_A`, `Source_B` | `value_a`, `value_b`, `abs_diff`, `pct_diff`                                |

Raw per-source measurements also include `OuraSpO2`, `OuraStress`, and
`OuraTags`. `OuraTags` drives Grafana annotations (vertical lines on every
timeseries panel) via the annotation query defined in the dashboard JSON —
log a tag like "sick" or "travel" in the Oura app and it shows up on the
charts the moment the fetcher syncs.

`Source` values currently used: `Garmin`, `Oura`, `Apple`. Raw per-source
measurements also include `AppleHealthActivity`, `AppleHealthSleep`,
`AppleHealthHeartRate`, `AppleHealthVO2Max`, `AppleHealthWorkout` (written
by the one-shot importer at `src/garmin_grafana/sources/apple_healthkit.py`
— run via `python -m garmin_grafana.sources.apple_healthkit <export.zip>`;
see `docs/apple-health-import.md`). Apple raw points are tagged with the
per-device `sourceName` (e.g. `Device="Apple Watch"`) for audit; Unified*
points get a single `Device=iOS` tag with values chosen by the importer's
`--prefer-source` priority pick to avoid multi-device double-counting.

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
cp override-default-vars.env.example override-default-vars.env
nano override-default-vars.env   # fill in Garmin/Oura credentials

# Optional but strongly recommended — installs a pre-commit hook that
# blocks accidental commits of live credentials:
uv tool install pre-commit
pre-commit install

# Run the orchestrator
uv run python -m garmin_grafana.orchestrator
```

The orchestrator expects InfluxDB reachable at `$INFLUXDB_HOST:$INFLUXDB_PORT`.
For dev it's easiest to start just the InfluxDB + Grafana containers via
`docker compose up influxdb grafana` and point the local Python process at
`localhost`.

### CI checks (run locally before pushing)

The same checks CI runs can all be invoked locally:

```bash
uv sync --locked                               # lockfile drift
uv run python -m compileall -q src/garmin_grafana   # syntax
uvx --from 'ruff==0.6.9' ruff check .          # lint (config in pyproject.toml)
uv run python scripts/check_secrets.py         # credential scan
```

`scripts/check_secrets.py` is the same script that runs in pre-commit and
CI. New files with placeholder-shaped credentials should be added to its
`ALLOWLIST` rather than weakening the regex.

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
6. Add a **per-source dashboard** at `Grafana_Dashboard/<NewSource>-Dashboard.json`
   that queries the raw `<NewSource>*` measurements directly (no `Source` tag
   filtering needed since each dashboard is single-source). Mirror the section
   layout used by `Oura-Dashboard.json`: Overview (stat panels) → Sleep →
   Readiness/Recovery → Activity → Heart Rate. Every timeseries panel (except
   stacked breakdowns and intraday streams) must include a "Your typical (14d)"
   target built with `moving_average(mean("field"), 14)` and a field override
   matching alias `/Your typical.*/` that renders it as a dashed gray line.
   For metrics with clinical norms defined in the catalog in
   `src/garmin_grafana/normal_ranges.py` (RHR, sleep duration,
   sleep efficiency, daily steps), do NOT hand-write threshold values —
   instead add a panel-level `"_normalRangeMetric": "<metric_key>"`
   marker and leave `thresholds.steps` as a single-entry
   `[{"color": "green", "value": null}]` placeholder with
   `thresholdsStyle.mode: "off"`. The fetcher runs
   `normal_ranges.stamp_dashboards_from_env()` on startup and rewrites
   those panels' thresholds in place based on `USER_AGE` / `USER_SEX`
   env vars. To teach it about a new metric, add a catalog entry in
   `normal_ranges.py` with a citation and add the key to the `resolve()`
   mapping. The existing `Garmin-Grafana-Dashboard.yaml` provider
   already auto-provisions every `.json` file in this directory — no
   yaml changes needed.
7. Update `discrepancy.py`'s source list if you want pairwise diffs against
   the new source.
8. Update this file and `README.md` with the new source.

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
