#!/usr/bin/env bash
# Self-rollback deploy script for the health-grafana stack.
#
# Flow:
#   1. Record the currently-checked-out commit as the rollback target.
#   2. git fetch + fast-forward to origin/<branch> (default: main).
#   3. docker compose up -d --build.
#   4. Poll `docker inspect` on each service until all three report "healthy"
#      (or the orchestrator is "running" without the heartbeat having gone
#      stale) within HEALTH_TIMEOUT seconds.
#   5. On failure: git reset --hard back to the rollback target and rebuild.
#
# Env vars (all optional):
#   DEPLOY_BRANCH      - branch to deploy (default: main)
#   HEALTH_TIMEOUT     - seconds to wait for healthy containers (default: 180)
#   COMPOSE_FILE       - compose file to use (default: docker-compose.yml)
#
# Exit codes:
#   0  - new version deployed and healthy, OR nothing to deploy
#   1  - deploy failed but rollback succeeded (stack is on the previous SHA)
#   2  - deploy failed AND rollback failed (manual intervention required)

set -euo pipefail

log() { printf '[deploy %(%F %T)T] %s\n' -1 "$*"; }
die() { log "FATAL: $*"; exit 2; }

DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

[[ -f "$COMPOSE_FILE" ]] || die "compose file '$COMPOSE_FILE' not found in $REPO_ROOT"

SERVICES=(health-fetch-data influxdb grafana)

# Refuse to run against a dirty tree — a `git reset --hard` on rollback
# would silently wipe the user's uncommitted edits.
if [[ -n "$(git status --porcelain)" ]]; then
  die "working tree is dirty; commit or stash before deploying"
fi

# Capture whatever ref we're on so we can put the clone back where we
# found it regardless of deploy outcome. `symbolic-ref` gives a branch
# name (e.g. `main`); on a detached HEAD it fails and we fall back to
# the raw SHA.
original_ref="$(git symbolic-ref --short -q HEAD || git rev-parse HEAD)"
rollback_sha="$(git rev-parse HEAD)"
log "starting from ref: $original_ref (sha: $rollback_sha)"

log "fetching origin/$DEPLOY_BRANCH..."
git fetch origin "$DEPLOY_BRANCH"
target_sha="$(git rev-parse "origin/$DEPLOY_BRANCH")"

# Switch to the deploy branch, forcing it to track origin exactly.
# `-B` creates it if missing and resets it to the given commit if it
# exists. Safe because we just verified the tree is clean.
log "checking out $DEPLOY_BRANCH at $target_sha..."
git checkout -B "$DEPLOY_BRANCH" "$target_sha"

if [[ "$rollback_sha" == "$target_sha" ]]; then
  log "already at origin/$DEPLOY_BRANCH ($target_sha) — nothing to deploy"
  # Put the clone back on whatever branch the operator was using.
  if [[ "$original_ref" != "$DEPLOY_BRANCH" ]]; then
    git checkout "$original_ref"
  fi
  exit 0
fi

log "deploying $rollback_sha -> $target_sha"

wait_for_healthy() {
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  while (( $(date +%s) < deadline )); do
    local all_ok=1
    for svc in "${SERVICES[@]}"; do
      local cid status health
      cid="$(docker compose -f "$COMPOSE_FILE" ps -q "$svc" 2>/dev/null || true)"
      if [[ -z "$cid" ]]; then all_ok=0; break; fi
      status="$(docker inspect --format '{{.State.Status}}' "$cid" 2>/dev/null || echo unknown)"
      health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo unknown)"
      if [[ "$status" != "running" ]]; then all_ok=0; break; fi
      # "none" means the image/compose didn't declare a healthcheck — treat
      # running as good enough. Otherwise require "healthy".
      if [[ "$health" != "none" && "$health" != "healthy" ]]; then
        all_ok=0
        break
      fi
    done
    if (( all_ok == 1 )); then
      return 0
    fi
    sleep 5
  done
  return 1
}

rollback() {
  log "ROLLBACK: reverting $DEPLOY_BRANCH to $rollback_sha"
  # We're currently on $DEPLOY_BRANCH (switched above). Reset it back
  # to the pre-deploy SHA so the next `docker compose up --build` picks
  # up the known-good tree.
  if ! git reset --hard "$rollback_sha"; then
    die "git reset --hard $rollback_sha failed"
  fi
  if ! docker compose -f "$COMPOSE_FILE" up -d --build; then
    die "rollback rebuild failed — stack is in an unknown state"
  fi
  if wait_for_healthy; then
    log "rollback OK — stack restored to $rollback_sha"
    exit 1
  else
    die "rollback rebuild started but containers still unhealthy"
  fi
}

trap 'log "ERR trap — attempting rollback"; rollback' ERR

log "building and restarting stack..."
docker compose -f "$COMPOSE_FILE" up -d --build

log "waiting up to ${HEALTH_TIMEOUT}s for containers to become healthy..."
if ! wait_for_healthy; then
  log "health check failed after ${HEALTH_TIMEOUT}s"
  trap - ERR
  rollback
fi

trap - ERR
log "deploy OK — $target_sha is live"

# Leave the clone on the deploy branch. The operator's pre-deploy ref
# was saved as $original_ref; if it differs from the deploy branch,
# we flag it so they can switch back manually (auto-switching would
# risk silently moving the stack's runtime checkout off main).
if [[ "$original_ref" != "$DEPLOY_BRANCH" ]]; then
  log "note: pre-deploy ref was '$original_ref'; leaving clone on '$DEPLOY_BRANCH' for stack runtime"
fi

# Prune old images that no longer have a tag so the disk doesn't fill up
# after many rebuilds. Keeps any image in use by a running container.
docker image prune -f >/dev/null 2>&1 || true
