# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.6.17-python3.13-bookworm-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git \
 && rm -rf /var/lib/apt/lists/*
RUN uv sync --locked

FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN groupadd --gid 1000 appuser && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

COPY --chown=appuser:appuser --from=build /app/.venv /app/.venv
COPY --chown=appuser:appuser src /app/

USER appuser

# Fail the healthcheck if the orchestrator hasn't touched its heartbeat file
# in the last 30 min. The loop writes /tmp/fetcher_heartbeat at the end of
# every cycle (see orchestrator.main). start-period covers cold boot,
# initial Garmin login, and first full fetch.
HEALTHCHECK --interval=60s --timeout=10s --start-period=300s --retries=3 \
  CMD test -f /tmp/fetcher_heartbeat \
   && test $(( $(date +%s) - $(stat -c %Y /tmp/fetcher_heartbeat) )) -lt 1800 \
   || exit 1

CMD ["python", "-m", "garmin_grafana.orchestrator"]
