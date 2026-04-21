"""
AI-powered health summary generation for Grafana dashboards.

Queries the latest data from InfluxDB, sends it to Claude with a
wellness-doctor system prompt, and writes the resulting markdown summary
back to an ``AIHealthSummary`` measurement in InfluxDB. Each dashboard
category gets its own summary row, keyed by the ``dashboard`` tag.

The summary is regenerated once per orchestrator cycle. A staleness
check compares the latest data timestamp against the last summary
timestamp — if nothing changed, the API call is skipped.

Environment variables:
    ANTHROPIC_API_KEY       Required. Skip summary generation if missing.
    HEALTH_SUMMARY_MODEL    Optional. Default: claude-haiku-4-5-20251001
    USER_AGE                Used in the prompt for age-specific interpretation.
    USER_SEX                Used in the prompt for sex-specific interpretation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime

import pytz

_log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
MODEL = os.getenv("HEALTH_SUMMARY_MODEL", "claude-haiku-4-5-20251001")
USER_AGE = os.getenv("USER_AGE", "").strip()
USER_SEX = os.getenv("USER_SEX", "").strip()

SYSTEM_PROMPT = """\
You are an expert wellness doctor reviewing a patient's health data from \
wearable devices and lab work. Provide a concise, actionable summary in \
HTML format. Use ONLY these HTML tags: <h3> for section headers, <ul> and \
<li> for bullet points, <strong> for emphasis, <p> for paragraphs, \
<span style="color:#73BF69"> for good values and \
<span style="color:#FF6B6B"> for concerning values. \
Do NOT include <!DOCTYPE>, <html>, <head>, or <body> tags. \
Do NOT use markdown syntax (no # or ** or -). \
Structure your response with these four sections:

<h3>Key Findings</h3> — 3-5 bullet points highlighting what stands out \
(good and bad), referencing specific values and their optimal ranges.

<h3>Trends</h3> — note any improving or worsening patterns if historical \
data is available.

<h3>Recommendations</h3> — 2-4 specific, actionable lifestyle changes \
ranked by expected impact. Be concrete (e.g., "increase zone 2 cardio \
to 150 min/week" not "exercise more").

<h3>Watch List</h3> — anything that needs attention on the next check.

Keep the summary under 400 words. Use plain language. Do not hedge \
excessively — the patient wants clear guidance, not disclaimers. \
Reference the optimal/performance ranges when interpreting values. \
{profile_line}
"""

# Dashboard categories and their data queries
DASHBOARD_CONFIGS: dict[str, dict] = {
    "blood_work": {
        "title": "Blood Work",
        "query": 'SELECT * FROM "BloodTest" ORDER BY time DESC LIMIT 2',
        "context": "Lab blood test results from Rythm Health. Performance ranges represent optimal health, not just 'normal'.",
    },
    "cardio_health": {
        "title": "Cardio Health",
        "queries": [
            ('TrainingStatus', 'SELECT mean("weeklyTrainingLoad") AS "weekly_load", mean("dailyAcuteChronicWorkloadRatio") AS "acwr" FROM "TrainingStatus" WHERE time > now() - 14d GROUP BY time(1d) fill(null)'),
            ('TrainingReadiness', 'SELECT mean("score") AS "readiness", mean("hrvFactorPercent") AS "hrv_factor" FROM "TrainingReadiness" WHERE time > now() - 14d GROUP BY time(1d) fill(null)'),
            ('RHR', 'SELECT mean("restingHeartRate") AS "rhr" FROM "DailyStats" WHERE time > now() - 30d GROUP BY time(1d) fill(null)'),
            ('BloodPressure', 'SELECT mean("Systolic") AS "systolic", mean("Diastolic") AS "diastolic" FROM "BloodPressure" WHERE time > now() - 30d GROUP BY time(1d) fill(null)'),
            ('VO2Max', 'SELECT last("VO2_max_value") AS "vo2_max" FROM "VO2_Max" WHERE time > now() - 90d'),
            ('FitnessAge', 'SELECT last("fitnessAge") AS "fitness_age", last("chronologicalAge") AS "chrono_age" FROM "FitnessAge" WHERE time > now() - 30d'),
        ],
        "context": "Cardio health metrics from Garmin. RHR declining = improving fitness. ACWR 0.8-1.3 = sweet spot. Systolic <120 and Diastolic <80 = normal BP.",
    },
    "oura_health": {
        "title": "Oura Ring Health",
        "queries": [
            ('Sleep', 'SELECT mean("sleep_score") AS "sleep_score", mean("efficiency") AS "efficiency", mean("total_sleep_duration")/3600 AS "sleep_hours" FROM "OuraSleep" WHERE time > now() - 14d GROUP BY time(1d) fill(null)'),
            ('HRV', 'SELECT mean("average_hrv") AS "hrv", mean("lowest_heart_rate") AS "lowest_hr" FROM "OuraSleep" WHERE time > now() - 14d GROUP BY time(1d) fill(null)'),
            ('Readiness', 'SELECT mean("score") AS "readiness" FROM "OuraReadiness" WHERE time > now() - 14d GROUP BY time(1d) fill(null)'),
            ('Activity', 'SELECT mean("steps") AS "steps", mean("active_calories") AS "active_cal" FROM "OuraDailyActivity" WHERE time > now() - 14d GROUP BY time(1d) fill(null)'),
            ('TempDev', 'SELECT mean("temperature_deviation") AS "temp_dev" FROM "OuraReadiness" WHERE time > now() - 14d GROUP BY time(1d) fill(null)'),
        ],
        "context": "Oura Ring data. Sleep score >85 = good. HRV trending up = improving recovery. Temp deviation sustained >0.5C = possible illness/overtraining. Efficiency >85% = good sleep quality.",
    },
    "multi_source": {
        "title": "Multi-Source Health",
        "queries": [
            ('Sleep', 'SELECT mean("duration_s")/3600 AS "sleep_hours", mean("hrv_avg") AS "hrv" FROM "UnifiedSleep" WHERE time > now() - 14d GROUP BY time(1d), "Source" fill(null)'),
            ('HR', 'SELECT mean("rhr") AS "rhr" FROM "UnifiedHeartRate" WHERE time > now() - 14d GROUP BY time(1d), "Source" fill(null)'),
            ('Activity', 'SELECT mean("steps") AS "steps" FROM "UnifiedActivity" WHERE time > now() - 14d GROUP BY time(1d), "Source" fill(null)'),
            ('Readiness', 'SELECT mean("score") AS "readiness" FROM "UnifiedReadiness" WHERE time > now() - 14d GROUP BY time(1d), "Source" fill(null)'),
        ],
        "context": "Cross-source health data (Garmin + Oura). Look for agreement or disagreement between sources. Averaged values represent best estimate.",
    },
}


def _query_influxdb(client, query: str) -> list[dict]:
    """Run an InfluxQL query and return rows as dicts."""
    try:
        result = client.query(query)
        rows = []
        for series in result.raw.get("series", []):
            columns = series.get("columns", [])
            tags = series.get("tags", {})
            for values in series.get("values", []):
                row = dict(zip(columns, values, strict=False))
                row.update(tags)
                rows.append(row)
        return rows
    except Exception as err:
        _log.debug("Query failed: %s — %s", query, err)
        return []


def _collect_data(client, config: dict) -> str:
    """Collect data for a dashboard category and format as text for the prompt."""
    lines = [f"Dashboard: {config['title']}", f"Context: {config['context']}", ""]

    if "query" in config:
        rows = _query_influxdb(client, config["query"])
        if rows:
            lines.append("Data:")
            lines.append(json.dumps(rows, indent=2, default=str))
        else:
            lines.append("No data available.")
    elif "queries" in config:
        for label, query in config["queries"]:
            rows = _query_influxdb(client, query)
            if rows:
                # Summarize: take last few non-null values
                summary = [r for r in rows if any(v is not None for k, v in r.items() if k != "time")]
                if summary:
                    lines.append(f"\n{label} (last {len(summary)} data points):")
                    lines.append(json.dumps(summary[-7:], indent=2, default=str))
    else:
        lines.append("No queries configured.")

    return "\n".join(lines)


def _compute_data_hash(data_text: str) -> str:
    """Hash the data to detect changes.

    Strips timestamps from the text before hashing so that queries
    returning the same values at different times produce the same hash.
    This prevents regenerating summaries every fetch cycle when the
    underlying health data hasn't actually changed.
    """
    import re
    # Strip ISO timestamps and epoch nanoseconds that change every query
    stripped = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s,\"]*", "T", data_text)
    stripped = re.sub(r"\b\d{19}\b", "T", stripped)  # epoch nanos
    data_text = stripped
    return hashlib.sha256(data_text.encode()).hexdigest()[:16]


def _get_last_summary_hash(client, dashboard: str) -> str | None:
    """Get the hash of the data used to generate the last summary."""
    try:
        result = client.query(
            f'SELECT last("data_hash") AS "hash" FROM "AIHealthSummary" WHERE "dashboard" = \'{dashboard}\''
        )
        rows = list(result.get_points())
        if rows:
            return rows[0].get("hash")
    except Exception:
        pass
    return None


def _generate_summary(data_text: str, profile_line: str) -> str | None:
    """Call Claude API to generate the health summary."""
    try:
        import anthropic
    except ImportError:
        _log.warning("anthropic SDK not installed — skipping health summary generation")
        return None

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT.format(profile_line=profile_line),
            messages=[{"role": "user", "content": data_text}],
        )
        return response.content[0].text
    except Exception as err:
        _log.warning("Claude API call failed: %s", err)
        return None


def _write_summary(influx_client, dashboard: str, summary: str, data_hash: str) -> None:
    """Write the summary to InfluxDB.

    InfluxDB escapes newlines in string fields as literal ``\\n``, which
    breaks rendering in the Dynamic Text Grafana plugin. Since we now
    request pure HTML from the LLM, we strip all newlines — HTML does
    not need whitespace between elements.
    """
    summary_flat = summary.replace("\n", " ").replace("  ", " ")
    point = {
        "measurement": "AIHealthSummary",
        "time": datetime.now(tz=pytz.utc).isoformat(),
        "tags": {"dashboard": dashboard},
        "fields": {
            "summary": summary_flat,
            "data_hash": data_hash,
            "model": MODEL,
        },
    }
    influx_client.write_points([point])
    _log.info("Wrote AI health summary for dashboard=%s (%d chars)", dashboard, len(summary))


def generate_summaries(influx_client) -> int:
    """
    Generate AI health summaries for all configured dashboards.

    Returns the number of summaries generated (0 if all were stale or
    the API key is missing).
    """
    if not ANTHROPIC_API_KEY:
        _log.info(
            "ANTHROPIC_API_KEY not set — skipping AI health summary generation. "
            "Set it in override-default-vars.env to enable."
        )
        return 0

    profile_line = ""
    if USER_AGE and USER_SEX:
        profile_line = f"The patient is a {USER_AGE}-year-old {USER_SEX}."

    generated = 0
    for dashboard_key, config in DASHBOARD_CONFIGS.items():
        try:
            data_text = _collect_data(influx_client, config)
            data_hash = _compute_data_hash(data_text)

            # Skip if data hasn't changed
            last_hash = _get_last_summary_hash(influx_client, dashboard_key)
            if last_hash == data_hash:
                _log.debug("Data unchanged for %s — skipping summary regeneration", dashboard_key)
                continue

            summary = _generate_summary(data_text, profile_line)
            if summary:
                _write_summary(influx_client, dashboard_key, summary, data_hash)
                generated += 1
        except Exception as err:
            _log.warning("Health summary generation failed for %s: %s", dashboard_key, err)

    _log.info("AI health summary generation complete: %d of %d dashboards updated", generated, len(DASHBOARD_CONFIGS))
    return generated
