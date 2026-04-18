# Importing Apple Health data

Apple Health has no server-side API, so the supported flow is a
**one-shot manual import** from the iPhone's built-in "Export All
Health Data" feature. Re-running the import after a new export is
idempotent — InfluxDB overwrites same-timestamp points, so the
dashboard picks up the new days without duplicating old ones.

## 1. Export from your iPhone

1. Open the **Health** app.
2. Tap your profile avatar (top-right corner of the Summary tab).
3. Scroll to the bottom, tap **Export All Health Data**. Confirm.
4. Wait. The export takes a few minutes and produces an
   `export.zip` around 50–300 MB for most users.
5. Share the zip to the server. Whatever's convenient — AirDrop to a
   Mac and `scp` over, iCloud Drive + download, direct USB transfer,
   or the Files app sharing a folder mounted via SMB.

The zip typically contains `apple_health_export/export.xml`. The
importer detects the XML wherever it lives inside the zip, so you
don't need to unpack anything.

## 2. Place the file somewhere the fetcher container can read

Any directory bind-mounted into the fetcher works. For example, mount
`/home/beezy/apple-health` to `/data/apple-health` inside the
container by adding this to the `health-fetch-data` service in
`docker-compose.yml`:

```yaml
    volumes:
      - ./garminconnect-tokens:/home/appuser/.garminconnect
      - ./Grafana_Dashboard:/app/Grafana_Dashboard
      - /home/beezy/apple-health:/data/apple-health:ro   # <— new
```

`docker compose up -d` to apply the change. Then drop the export zip
into `/home/beezy/apple-health/` on the host.

## 3. Run the importer

From anywhere on the server:

```bash
docker compose exec health-fetch-data python -m \
    garmin_grafana.sources.apple_healthkit /data/apple-health/export.zip
```

Useful flags:

| Flag                           | Purpose                                                   |
| ------------------------------ | --------------------------------------------------------- |
| `--from YYYY-MM-DD`            | Skip records before this day (speeds up an incremental sync)  |
| `--to YYYY-MM-DD`              | Skip records after this day                               |
| `--prefer-source "Apple Watch"`| Sourcename priority when multiple devices logged the same day (default "Apple Watch"; substring match, so "Brett's Apple Watch" wins over "iPhone") |
| `--device-name iOS`            | Device tag stamped onto `Unified*` points (default `iOS`) |
| `--dry-run`                    | Parse + summarize, do not write                           |

The importer logs a per-measurement count at the end, e.g.:

```
unified points built: {'UnifiedActivity': 430, 'UnifiedHRIntraday': 618394, 'UnifiedHeartRate': 428, 'UnifiedSleep': 412, 'UnifiedVO2Max': 88, 'UnifiedWorkout': 56}
raw points built:     {'AppleHealthActivity': 820, 'AppleHealthHeartRate': 800, 'AppleHealthSleep': 412, 'AppleHealthVO2Max': 88, 'AppleHealthWorkout': 56}
```

## 4. See the data

Open Grafana (`http://localhost:3000`):

- **Apple Health** dashboard — dedicated per-source view (Overview,
  Sleep, Heart Rate, Activity, Fitness, Workouts).
- **Multi-Source Health Dashboard** — pick `Apple` (or `All`) in the
  `$source` dropdown to overlay Apple data against Garmin / Oura.
- **Source Discrepancies** row on the Multi-Source dashboard — shows
  where Apple disagrees with the other devices, once more than one
  source has data for a day.

## How multi-source picking works

Apple Health aggregates samples from every device that writes to it
(Apple Watch, iPhone, third-party apps) but its **per-type priority
list is not preserved in the export XML**. A naive sum across all
sources double-counts (e.g. an Apple Watch run + iPhone pedometer
both logging the same steps).

The importer handles this by:

1. **Per-source parsing.** Every record is bucketed by its
   `sourceName` before any aggregation.
2. **Priority pick per day/metric.** For each day and each metric
   (steps, calories, sleep, HR, VO2), the importer picks the
   highest-priority source that has data:
   - exact or substring match to `--prefer-source`
   - any source with "Apple Watch" in the name
   - any source with "iPhone" / "iPad" in the name
   - everything else (third-party apps)
3. **Audit trail.** The pick only applies to the `Unified*`
   measurements. The raw `AppleHealth*` points are emitted per-source
   with `Device = <sourceName>`, so you can see every device's own
   totals via the InfluxDB query editor.
4. **Intraday HR de-dup.** Only samples from the preferred source
   feed `UnifiedHRIntraday`. Without this, a Withings strap logging
   the same minute as an Apple Watch would double each point on the
   chart. If the preferred source contributes nothing, the importer
   falls back to every other source.

## Re-running

Safe any time. InfluxDB's line protocol treats
(measurement, tags, timestamp) as the primary key, so re-inserting
the same day overwrites. Do weekly or monthly exports and re-run
the importer — it won't duplicate.

## Troubleshooting

- **"export.xml not found"** — the zip is malformed or was partially
  downloaded. Re-export from the phone.
- **Apple sleep shows but no stages** — pre-iOS-16 devices only write
  `AsleepUnspecified` (treated as "light" in the importer). Upgrade
  the paired Watch to watchOS 9+ for deep/REM breakdown.
- **Activity double-counts on some days** — check the raw
  `AppleHealthActivity` points; you may have an extra third-party
  app (like an old MyFitnessPal integration) writing steps. Use
  `--prefer-source` to lock to a specific device, or uninstall the
  stale app's Health integration on the phone.
- **Memory error on huge exports** — use `--from YYYY-MM-DD` to
  bound the first import to a recent window, then widen it on
  subsequent runs. The parser streams the XML with `iterparse` so
  memory is proportional to *intraday-HR sample count*, not file
  size.
