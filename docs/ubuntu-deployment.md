# Deploying `health-grafana` on a local Ubuntu server

This guide walks you through running the full stack — fetcher, InfluxDB,
Grafana — on your own Ubuntu machine. It assumes Ubuntu 22.04 or later with
sudo access. The stack is entirely self-hosted; no cloud services beyond the
Garmin Connect and Oura Cloud APIs are used.

## 1. Prerequisites

- Ubuntu 22.04+ (works on any Linux with Docker, but commands below use apt)
- At least 2 GB RAM free and ~5 GB of disk for long-term data
- A Garmin Connect account (email + password)
- An Oura Cloud Personal Access Token (see [`oura-setup.md`](./oura-setup.md))

## 2. Install Docker Engine + Compose plugin

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
# Log out and back in for the group change to apply, or run:
newgrp docker
```

Verify:

```bash
docker --version
docker compose version
```

## 3. Clone the repo

```bash
sudo mkdir -p /opt/health-grafana
sudo chown "$USER":"$USER" /opt/health-grafana
git clone https://github.com/bwhite213/health-grafana.git /opt/health-grafana
cd /opt/health-grafana
```

## 4. Configure

Copy the example compose file and create your env overrides:

```bash
cp compose-example.yml docker-compose.yml
cat > override-default-vars.env <<'EOF'
# --- Garmin ---
GARMINCONNECT_EMAIL=you@example.com
# Password must be base64-encoded. Encode once with:
#   echo -n 'your-plaintext-password' | base64
GARMINCONNECT_BASE64_PASSWORD=eW91ci1wbGFpbnRleHQtcGFzc3dvcmQ=

# --- Oura ---
OURA_PERSONAL_ACCESS_TOKEN=paste-your-oura-pat-here
OURA_ENABLED=True

# --- Unified analytics layer ---
ENABLE_UNIFIED_MEASUREMENTS=True
DISCREPANCY_LOOKBACK_DAYS=7

# --- Housekeeping ---
USER_TIMEZONE=America/New_York
LOG_LEVEL=INFO
EOF
chmod 600 override-default-vars.env
```

Also create the Garmin token volume directory with the right ownership
(the fetcher container runs as UID/GID 1000):

```bash
mkdir -p garminconnect-tokens
sudo chown -R 1000:1000 garminconnect-tokens
```

## 5. Firewall (optional but recommended)

If you run `ufw`:

```bash
sudo ufw allow 3000/tcp    # Grafana UI
sudo ufw enable            # if not already enabled
# DO NOT open 8086 (InfluxDB) to the network unless you need remote access.
```

## 6. Build and start the stack

```bash
cd /opt/health-grafana
docker compose up -d --build
```

The first start will:

1. Build the fetcher image from the local `Dockerfile`
2. Start InfluxDB 1.11 and Grafana
3. Boot the `health-fetch-data` container, which performs an interactive
   Garmin login on first run (watch the logs for an MFA prompt if your
   Garmin account has 2FA enabled)

Watch the logs:

```bash
docker compose logs -f health-fetch-data
```

You should see successful Garmin fetches, then Oura fetches, then a
`Discrepancy detector produced N points` line each cycle.

## 7. Open Grafana

Navigate to `http://<your-server-ip>:3000` and log in with
`admin` / `admin`. Grafana will force a password change on first login.

You should see two auto-provisioned dashboards:

- **Garmin Grafana Dashboard** — the original single-source Garmin view.
- **Multi-Source Health** — the new unified dashboard with per-source,
  overlay, cross-source-mean, and discrepancy panels.

Use the `source` dropdown at the top of the Multi-Source Health dashboard to
toggle between `Garmin`, `Oura`, or both (select all).

## 8. Historical backfill (optional)

To pull historical data from before the fetcher started running, set these
temporarily and restart:

```bash
# In override-default-vars.env
MANUAL_START_DATE=2024-01-01
MANUAL_END_DATE=2026-04-14
```

```bash
docker compose restart health-fetch-data
docker compose logs -f health-fetch-data
```

When the logs show `Bulk update success`, remove those two lines and
restart the container to return to continuous mode.

## 9. Persistence, reboots, updates

- **Named volumes** (`influxdb_data`, `grafana_data`) and the bind-mounted
  `garminconnect-tokens/` directory survive reboots.
- `restart: unless-stopped` is set on all three services, so the stack comes
  back automatically after a reboot. You do not need a systemd unit.
- To update the code:
  ```bash
  cd /opt/health-grafana
  git pull
  docker compose up -d --build
  ```

## 10. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Unable to connect with influxdb database` on startup | Make sure the `influxdb` service is healthy (`docker compose ps`) and that `INFLUXDB_HOST=influxdb` in the env block. |
| Garmin login fails with 2FA loop | Run `docker compose exec health-fetch-data python -m garmin_grafana.garmin_fetch` interactively to complete the MFA flow once — tokens will persist in the mounted volume. |
| Oura section empty in dashboard | Verify `OURA_PERSONAL_ACCESS_TOKEN` is set, then `docker compose logs health-fetch-data | grep Oura`. A 401 means the token is wrong or expired. |
| Multi-Source Health dashboard shows no data | The `Unified*` measurements populate only after the fetcher writes at least one full cycle. Give it 5–10 minutes after the first successful Garmin + Oura fetch. |
| Permission denied on `garminconnect-tokens` | `sudo chown -R 1000:1000 garminconnect-tokens` — the container runs as UID 1000. |
| I want to see raw cross-source differences | Query InfluxDB directly: `docker compose exec influxdb influx -database GarminStats -execute 'SELECT * FROM "SourceDiscrepancy" ORDER BY time DESC LIMIT 20'` |
