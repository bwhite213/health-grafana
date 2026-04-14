# Oura Ring setup

`health-grafana` consumes Oura Cloud data via the official Oura API v2 using a
**Personal Access Token (PAT)**. Your token stays on your own machine — no
third-party service is involved.

## 1. Create a Personal Access Token

1. Sign in to Oura Cloud at <https://cloud.ouraring.com/>.
2. Open **Account → Personal Access Tokens** (direct link:
   <https://cloud.ouraring.com/personal-access-tokens>).
3. Click **Create New Personal Access Token**.
4. Give it a descriptive note (e.g. `health-grafana-home-server`) and create it.
5. Copy the token string immediately — Oura only shows it once.

## 2. Configure the fetcher

Open your `override-default-vars.env` (or the `environment:` block in
`docker-compose.yml`) and set:

```env
OURA_PERSONAL_ACCESS_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OURA_ENABLED=True
```

Then restart the fetcher:

```bash
docker compose up -d health-fetch-data
docker compose logs -f health-fetch-data
```

You should see lines like:

```
Oura: 42 points for 2026-04-13 (sleep=True activity=True readiness=True hr_samples=288)
```

## 3. What data gets collected

The fetcher pulls the following Oura Cloud API v2 endpoints once per cycle
(defaults to every 5 minutes, controlled by `UPDATE_INTERVAL_SECONDS`):

| Endpoint                        | Purpose                                        |
| ------------------------------- | ---------------------------------------------- |
| `/v2/usercollection/daily_sleep`    | Nightly sleep score and contributors       |
| `/v2/usercollection/sleep`          | Detailed sleep stages, HRV, RHR, efficiency |
| `/v2/usercollection/daily_activity` | Steps, calories, active time               |
| `/v2/usercollection/daily_readiness`| Readiness score (0–100)                    |
| `/v2/usercollection/heartrate`      | Intraday heart rate samples                |

Data is written to two places in InfluxDB:

- **Raw** — measurements named `OuraSleep`, `OuraDailyActivity`,
  `OuraReadiness`, `OuraHRIntraday` (Oura-only fields, full fidelity).
- **Unified** — mirror points tagged `Source=Oura` inside `UnifiedSleep`,
  `UnifiedHeartRate`, `UnifiedHRIntraday`, `UnifiedActivity`,
  `UnifiedReadiness`. These are what the **Multi-Source Health** Grafana
  dashboard queries.

## 4. Rate limits

Oura allows 5,000 requests per 5 minutes per token — effectively unlimited
for this use case. No throttling is needed on the fetcher side.

## 5. Disabling Oura

Set `OURA_ENABLED=False` in your env file, or simply leave
`OURA_PERSONAL_ACCESS_TOKEN` empty. The orchestrator will log a message and
skip Oura fetching; Garmin data still flows normally.

## 6. Historical backfill

Backfilling Oura data works via the same `MANUAL_START_DATE` /
`MANUAL_END_DATE` variables used for Garmin bulk imports:

```env
MANUAL_START_DATE=2024-01-01
MANUAL_END_DATE=2026-04-14
```

Start the container, let it run to completion (check logs for a "Bulk update
success" line), then unset the variables and restart for normal continuous
mode.
