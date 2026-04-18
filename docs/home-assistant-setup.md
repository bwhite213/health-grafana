# Home Assistant setup (near-live Apple HealthKit + home automation)

Home Assistant (HA) is an optional fourth service in this stack that
gives you:

1. **Near-live iPhone HealthKit data** via the iOS Companion app
   (updates every minute-ish vs. the monthly manual export flow in
   `docs/apple-health-import.md`).
2. An extension point for **bedroom environment sensors**, Tesla /
   Rivian data, smart thermostat readings, whatever else you want
   correlated against your health metrics.
3. The usual home-automation stuff (lights, locks, cameras,
   thermostats, 3000+ integrations) if you want to grow into it.

All of HA's data lands in a **separate `home_assistant` InfluxDB
database** on the same InfluxDB instance this project already uses.
Grafana gets a second datasource (`HomeAssistant-InfluxDB`) pointed
at that database — both datasources exist side-by-side and panels
can mix them via Grafana's "Mixed" datasource.

## One-time setup

### 1. Create the InfluxDB database HA will write to

HA's InfluxDB integration expects the database to already exist. Run
this once against the existing `influxdb` container:

```bash
docker compose exec influxdb influx \
  -execute "CREATE DATABASE home_assistant"
```

Verify:

```bash
docker compose exec influxdb influx \
  -execute "SHOW DATABASES"
# should list GarminStats AND home_assistant
```

(Auth is off on this InfluxDB by design — see `compose-example.yml`.
HA doesn't need credentials.)

### 2. Start the container

```bash
docker compose up -d homeassistant
docker compose logs -f homeassistant  # watch the cold boot (~90s)
```

You should see `Home Assistant initialized in ...` followed by
`Hass is Starting ... Setup of integration influxdb took ... seconds`.
The healthcheck needs ~2 min on first boot because HA downloads
integration metadata.

### 3. Finish the UI wizard

Open `http://<server-ip>:8123` on a browser on the same network (or
through your VPN).

- **Create account** — admin username + password for you. This is the
  only irreducibly manual step; HA doesn't support unattended
  provisioning of the first user.
- **Location & timezone** — set to match where you live. HA uses this
  for sunrise/sunset automations etc.
- **"What's on your network?"** — you can skip this screen; the iOS
  Companion app registers itself when it connects in the next step.

### 4. Install the iOS Companion app

On your iPhone:

1. App Store → search "Home Assistant" → install.
2. Open it, tap **Continue** → enter the URL of your HA server
   (`http://<server-ip>:8123` from the same network, or your Tailscale
   / WireGuard hostname if remote).
3. Log in with the admin credentials from step 3.
4. The app asks for a device name — keep the default or set it to
   "<your name> iPhone".
5. Grant notification + location permissions when prompted (optional
   but the location sensors are useful).

### 5. Enable HealthKit sensors

Still in the iOS Companion app:

1. Tap **Settings** (gear icon, bottom-right).
2. **Companion App → Sensors**.
3. Toggle on the HealthKit categories you want streamed. Useful ones:
   - **Active Energy** (burned kcal today)
   - **Steps**
   - **Heart Rate**
   - **Resting Heart Rate**
   - **Heart Rate Variability (SDNN)**
   - **Walking + Running Distance**
   - **Sleep Analysis**
   - **Blood Oxygen**
   - **VO2 Max**
4. iOS will prompt for HealthKit permission on each — allow.
5. **Force-sync** by pulling down on the Companion app's Overview
   screen; the sensors show up in HA under **Settings → Devices &
   Services → Mobile App → <your device>**.

### 6. Confirm data is flowing to InfluxDB

```bash
docker compose exec influxdb influx \
  -database home_assistant \
  -execute 'SHOW MEASUREMENTS' | head
```

You should see entries like `sensor.heart_rate`, `sensor.steps`,
`device_tracker.<your-phone>`, etc. within a few minutes of enabling
the sensors.

### 7. Find the data in Grafana

Open Grafana → **Explore** (compass icon) → pick
**HomeAssistant-InfluxDB** as the datasource → run:

```sql
SELECT mean("value") FROM "bpm"
WHERE $timeFilter AND "entity_id" = 'heart_rate'
GROUP BY time(5m) fill(null)
```

(HA's InfluxDB integration groups measurements by the quantity unit —
`bpm` for heart rate, `steps` for steps, etc. — and tags rows with
`entity_id`. Exact measurement / field names appear once your first
samples land.)

## How HA HealthKit data coexists with the manual Apple import

Two independent paths land in the same InfluxDB instance:

| Path                                                        | Lag              | Tags                                  | Measurement      |
| ----------------------------------------------------------- | ---------------- | ------------------------------------- | ---------------- |
| `docs/apple-health-import.md` (manual `export.zip`)         | Monthly-ish      | `Source=Apple`, `Device=iOS`          | `Unified*` + `AppleHealth*` |
| iOS Companion → HA → InfluxDB (this doc)                    | ~1 min           | `entity_id=heart_rate`, per-sensor    | HA native (e.g. `bpm`, `steps`, `%`) |

They don't overwrite each other — different databases, different
measurement shapes. Treat them as complementary: live trends via HA,
historical depth + the curated Apple Dashboard via the import.

## Adding HA data to the Multi-Source dashboard (optional)

The Multi-Source dashboard queries `Unified*` measurements with a
`Source` tag. HA writes raw sensor values that don't conform to that
schema. Two options:

- **Separate HA dashboard** (simplest): build a new
  `Home-Assistant-Overview.json` querying `home_assistant` directly
  for phone battery, live HR, location, environment sensors.
- **Automation → Unified bridge** (future): add an HA automation that
  writes the normalized `Unified*` points into the `GarminStats`
  database via InfluxDB's HTTP API when HealthKit sensors update.
  Makes live HR show up in the Multi-Source view alongside Garmin /
  Oura without any schema divergence. Not implemented yet — see the
  Phase 5 stretch note in the implementation plan.

## Operational notes

- **Restart + auto-deploy**: HA is in the restart policy loop and in
  `scripts/deploy.sh`'s health gate — a bad push that breaks HA will
  trigger the stack-wide rollback along with any other service.
- **State persistence**: the UI-created state (integrations, users,
  automations, SQLite history DB) lives in `homeassistant-config/`
  and is gitignored. Only `configuration.yaml` is tracked.
- **Switching to host networking** (later, when adding Zigbee/Matter
  via a USB stick): change `network_mode: host`, remove the `ports:`
  block, and change the `influxdb` host in `configuration.yaml` to
  `localhost` or the host's LAN IP. `ports: 8086` on the `influxdb`
  service may need to be published to the host for HA to reach it.
- **Upgrades**: HA releases monthly. `docker compose pull
  homeassistant && docker compose up -d homeassistant` applies the
  latest stable image. Review the breaking-changes notice before
  upgrading major versions.
- **Backups**: back up `homeassistant-config/` along with your
  InfluxDB volumes. HA's built-in **Settings → System → Backups**
  also produces a self-contained tarball.

## Troubleshooting

- **HA unhealthy at boot** — extend `start_period` in `docker-compose.yml`
  if your hardware is slower; 120s is enough for most modern boxes.
- **InfluxDB integration silently no-op** — double check the
  `home_assistant` database exists (step 1). HA logs the integration
  but doesn't loudly fail if the DB is missing.
- **iOS Companion "couldn't connect"** — phone and server must be on
  the same LAN, or the phone must be on your VPN. Local mDNS
  discovery doesn't work in bridge networking mode; type the URL
  manually.
- **Sensors not appearing in InfluxDB** — `exclude:` in
  `configuration.yaml` may be filtering them. Loosen the excludes or
  add `include:` stanzas for the specific entities you want.
