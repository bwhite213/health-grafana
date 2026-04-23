"""
Tiny HTTP endpoint that force-regenerates the AI wellness summaries.

Lives inside the fetcher container so it can call the existing
``health_summary.generate_summaries(influxdbclient, force=True)`` helper
without any cross-container plumbing. Exposed on a compose-published
port so a dashboard link can hit it from a browser on the same LAN/VPN.

Why this module exists: the summary panels show *last-generated*
timestamps but regenerating on demand previously required SSHing to
the host and running
``docker exec health-fetch-data python -m garmin_grafana.health_summary``.
A click-to-refresh link in the Grafana panel itself is a better UX
while traveling.

Security model: network isolation. The bound port (8765) is only
reachable from the same LAN/VPN boundary that already fronts Grafana
on 3000 — if an attacker is on your network, they already have the
dashboard. A 60-second rate-limit in the handler caps worst-case
Anthropic-API-cost abuse at about 1 call per minute, which is a
couple cents per hour even under sustained hammering. No token. If
you need one (e.g. because the port is internet-exposed), set
``SUMMARY_WEBHOOK_TOKEN`` and append ``?token=<value>`` to the
dashboard links.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

_log = logging.getLogger(__name__)

# Binding config — port is published in docker-compose.yml. Bind to
# 0.0.0.0 inside the container so the published port actually routes
# traffic in; external exposure is scoped by the host's network (same
# LAN/VPN boundary that already protects Grafana on 3000).
_PORT = int(os.getenv("SUMMARY_WEBHOOK_PORT", "8765"))
_TOKEN = os.getenv("SUMMARY_WEBHOOK_TOKEN", "").strip()
_MIN_SECONDS_BETWEEN_CALLS = int(os.getenv("SUMMARY_WEBHOOK_MIN_INTERVAL_S", "60"))

_last_call_at = 0.0
_rate_lock = threading.Lock()


_SUCCESS_HTML = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Summary regenerated</title>
<style>body{font-family:system-ui,sans-serif;background:#1f1f1f;color:#e0e0e0;
padding:3rem;max-width:600px;margin:0 auto}
a{color:#5aa0ff}.ok{color:#65c466}</style></head>
<body><h2 class="ok">\xe2\x9c\x93 AI summaries regenerated</h2>
<p>%d of 4 dashboards updated. Refresh your Grafana tab to see the new text.</p>
<p><small>You can close this tab.</small></p>
</body></html>
"""

_FAIL_HTML = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Regenerate failed</title>
<style>body{font-family:system-ui,sans-serif;background:#1f1f1f;color:#e0e0e0;
padding:3rem;max-width:600px;margin:0 auto}
.err{color:#e06060}</style></head>
<body><h2 class="err">\xe2\x9c\x97 Regenerate failed</h2>
<p>%s</p>
<p>Check <code>docker logs health-fetch-data</code> for details.</p>
</body></html>
"""

_RATE_LIMIT_HTML = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Slow down</title>
<style>body{font-family:system-ui,sans-serif;background:#1f1f1f;color:#e0e0e0;
padding:3rem;max-width:600px;margin:0 auto}
.warn{color:#e0b060}</style></head>
<body><h2 class="warn">\xe2\x8f\xb3 Just regenerated \xe2\x80\x94 please wait</h2>
<p>The AI summaries were regenerated less than a minute ago. Try again
in <strong>%d</strong> seconds.</p>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    # Suppress the default per-request stderr logging; our own _log line
    # is plenty. BaseHTTPRequestHandler.log_message writes to sys.stderr
    # by default which ends up in the fetcher's docker logs.
    def log_message(self, format, *args):  # noqa: A002 - parent's signature
        return

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # Accept POST *and* GET so users can simply click a link from
    # Grafana (browsers navigate with GET on anchor clicks).
    def do_POST(self) -> None:  # noqa: N802 - stdlib requires this name
        return self.do_GET()

    def do_GET(self) -> None:  # noqa: N802 - stdlib requires this name
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != "/regenerate-summary":
            self._send(404, b"not found", "text/plain")
            return

        # Token is optional — only enforced if SUMMARY_WEBHOOK_TOKEN is
        # set. For default LAN/VPN deployments it's empty and we skip
        # the check; if you want an extra layer for an internet-exposed
        # port, set it and the dashboard links need ?token=<value>.
        if _TOKEN:
            supplied = parse_qs(parsed.query).get("token", [""])[0]
            if supplied != _TOKEN:
                self._send(401, b"unauthorized", "text/plain")
                return

        # Rate limit across all callers on this instance. Anthropic
        # generation takes ~30s anyway, so back-to-back clicks are
        # pointless; this just keeps someone on the LAN from spamming
        # API spend by holding down F5.
        global _last_call_at
        with _rate_lock:
            now = time.monotonic()
            elapsed = now - _last_call_at
            if elapsed < _MIN_SECONDS_BETWEEN_CALLS:
                wait = int(_MIN_SECONDS_BETWEEN_CALLS - elapsed)
                self._send(
                    429,
                    _RATE_LIMIT_HTML % wait,
                )
                return
            _last_call_at = now

        try:
            # Lazy-import to keep module load cheap + avoid circular deps
            # between the orchestrator (which imports this module to kick
            # off the HTTP thread) and health_summary (which pulls in
            # garmin_fetch's InfluxDB client at import time).
            from . import garmin_fetch, health_summary  # noqa: PLC0415

            _log.info("Webhook: force-regenerating AI health summaries")
            n = health_summary.generate_summaries(garmin_fetch.influxdbclient, force=True)
            _log.info("Webhook: regenerated %d summaries", n)
            self._send(200, _SUCCESS_HTML % n)
        except Exception as err:  # noqa: BLE001 - surface everything to the user
            _log.exception("Webhook regenerate failed")
            self._send(500, _FAIL_HTML % str(err).encode("utf-8"))


def start() -> None:
    """Boot the webhook in a daemon thread at fetcher startup.

    Always runs unless ``SUMMARY_WEBHOOK_DISABLED`` is truthy. Rate-
    limited and optionally token-gated; see module docstring for the
    security model.
    """
    if os.getenv("SUMMARY_WEBHOOK_DISABLED", "").lower() in ("1", "true", "yes"):
        _log.info("SUMMARY_WEBHOOK_DISABLED set — skipping webhook startup")
        return
    server = HTTPServer(("0.0.0.0", _PORT), _Handler)
    t = threading.Thread(
        target=server.serve_forever, name="summary-webhook", daemon=True
    )
    t.start()
    auth = "with token" if _TOKEN else "unauthenticated (LAN/VPN only)"
    _log.info(
        "Summary webhook listening on :%d (/regenerate-summary), %s, "
        "rate limit %ds",
        _PORT, auth, _MIN_SECONDS_BETWEEN_CALLS,
    )
