# Auto-deploy + travel-safe resilience

Three layers cover the "push to main → stack redeploys itself; don't wake me
up while I'm on a plane" story:

1. **Container restart policies + healthchecks** (already committed) —
   Docker restarts crashed containers and knows which ones are actually
   serving.
2. **CI-gated self-hosted deploy with rollback** — a GitHub Actions runner
   on your server pulls and rebuilds only after the cloud CI workflow is
   green, and reverts to the previous commit if the new stack fails its
   health gate.
3. **Dead-man's switch** — the fetcher pings healthchecks.io every cycle.
   If pings stop, you get notified within minutes.

You need all three for the system to stay up while you're away.

---

## 1. Self-hosted GitHub Actions runner (one-time)

The runner is what actually executes the deploy on the Ubuntu box. It
makes an **outbound** long-poll to GitHub, so no inbound ports or
port-forwarding are needed.

### 1a. Register the runner

1. On GitHub, open the repo → **Settings → Actions → Runners → New
   self-hosted runner**. Pick Linux / x64.
2. Follow the shown commands on the server. When the installer asks for
   runner labels, add **`health-grafana-server`** alongside the defaults
   (`self-hosted`, `Linux`, `X64`). The deploy workflow targets that
   label.
3. Register the runner's working directory **as the repo checkout the
   compose stack already uses** — that way `./scripts/deploy.sh` runs
   against the same clone Docker Compose is reading. Easiest:

   ```bash
   mkdir -p /opt/actions-runner
   cd /opt/actions-runner
   # ...run the ./config.sh command GitHub gave you, then:
   ./config.sh --work /home/beezy/workspace/health-grafana
   ```

   (`--work` is the root under which each job's `GITHUB_WORKSPACE`
   lives; the deploy script doesn't rely on `GITHUB_WORKSPACE` directly
   — it `cd`s to its own `scripts/..` — but using your existing clone
   means there's no second checkout to keep in sync.)

### 1b. Install as a systemd service

So it survives reboots:

```bash
cd /opt/actions-runner
sudo ./svc.sh install beezy          # run as your user, not root
sudo ./svc.sh start
sudo ./svc.sh status                 # sanity check
```

### 1c. Give the runner user Docker access

The runner user needs to run `docker compose` without sudo:

```bash
sudo usermod -aG docker beezy
# log out and back in, or restart the runner service
```

### 1d. Verify

Push a trivial commit to main (or open the Actions tab and
**Run workflow** on the `Deploy` workflow manually). The runner should
pick it up, run `./scripts/deploy.sh`, and land on the new SHA.

---

## 2. Dead-man's switch via healthchecks.io

1. Sign up at <https://healthchecks.io> (free tier covers 20 checks).
2. Create a check:
   - **Name**: `health-grafana fetcher`
   - **Schedule**: *Simple* — period 5 min (match
     `UPDATE_INTERVAL_SECONDS`), grace 15 min (generous — Garmin API can
     be slow).
3. Copy the ping URL (looks like `https://hc-ping.com/<uuid>`) into your
   `override-default-vars.env`:

   ```env
   HEALTHCHECK_PING_URL=https://hc-ping.com/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   ```

4. Restart the stack (`docker compose up -d`). Within a cycle you should
   see the check turn green on healthchecks.io.
5. On the **Integrations** tab in healthchecks.io, wire up at least
   email — or Slack / Pushover / Discord if you'd rather get a push
   notification.

The orchestrator fires three ping types:

| Endpoint     | When                                                        |
| ------------ | ----------------------------------------------------------- |
| `$URL/start` | At the start of each cycle (so slow runs don't flap)        |
| `$URL`       | On a successful cycle                                       |
| `$URL/fail`  | When reading the Garmin device sync time raises             |

---

## 3. Operational notes

### Rollback behaviour

`scripts/deploy.sh`:

1. Records the current HEAD as the rollback SHA.
2. Fast-forwards to `origin/main` (aborts if the branch diverged —
   never clobbers local state).
3. `docker compose up -d --build`.
4. Polls `docker inspect` for up to `HEALTH_TIMEOUT` seconds (default
   240s in CI, 180s locally) waiting for every service with a
   healthcheck to report `healthy` and every service without one to
   report `running`.
5. On timeout or any earlier failure: `git reset --hard` back to the
   rollback SHA and rebuild.

Exit codes tell you what happened:

- `0` — new version live and healthy (or nothing to deploy).
- `1` — new version was unhealthy; stack is back on the previous SHA.
  GitHub will show the deploy job as failed, which is exactly what you
  want — rollback succeeded, but you still need to look at why.
- `2` — both the rollout **and** the rollback failed. Manual
  intervention required. Check `docker compose logs` and `git log`.

### Manually triggering a deploy

From GitHub: **Actions → Deploy → Run workflow** (ignores CI gating).
From the server:

```bash
cd /home/beezy/workspace/health-grafana
./scripts/deploy.sh
```

### Testing the rollback locally

```bash
# On a throwaway branch, introduce a deliberate crash (e.g. a syntax
# error in orchestrator.py), commit, push. The Deploy workflow runs,
# fails the health gate, rolls back. Delete the branch when done.
```

### When to bypass auto-deploy

If you need to pin the server to a specific SHA (e.g. during debugging):

```bash
sudo systemctl stop actions.runner.<org>-<repo>.<name>.service
# ...hack, fix, restart manually with ./scripts/deploy.sh or docker compose...
sudo systemctl start actions.runner.<org>-<repo>.<name>.service
```
