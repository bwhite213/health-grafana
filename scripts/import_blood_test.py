#!/usr/bin/env python3
"""
Import Rythm Health blood test results into InfluxDB.

Accepts either a Rythm PDF or a JSON file with the same structure.

Usage:
    # From a PDF (requires pdfplumber):
    uv run python scripts/import_blood_test.py path/to/rythm-results.pdf

    # From a JSON file:
    uv run python scripts/import_blood_test.py path/to/results.json

    # Dry-run (print points without writing):
    uv run python scripts/import_blood_test.py --dry-run path/to/results.pdf

JSON format (for manual entry or when pdfplumber isn't available):
    {
        "collected": "2026-04-06",
        "fasting": true,
        "lab": "Rythm Health",
        "results": [
            {"test": "Total Cholesterol", "value": 188, "unit": "mg/dL",
             "range_low": 100, "range_high": 240,
             "perf_low": null, "perf_high": null},
            ...
        ]
    }

InfluxDB measurement written: ``BloodTest``
    Tags: Lab, Fasting
    Fields: one float per biomarker, snake_case normalized
    Time: collection date at 06:00 UTC (morning draw)

Environment variables (same as the main fetcher):
    INFLUXDB_HOST, INFLUXDB_PORT, INFLUXDB_USERNAME, INFLUXDB_PASSWORD,
    INFLUXDB_DATABASE, INFLUXDB_VERSION
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pytz


# ---------------------------------------------------------------------------
# Biomarker name normalization
# ---------------------------------------------------------------------------

_NAME_MAP = {
    "thyroid stimulating hormone": "tsh",
    "albumin": "albumin",
    "triglycerides": "triglycerides",
    "hdl cholesterol": "hdl",
    "total cholesterol": "total_cholesterol",
    "apob": "apob",
    "creatinine": "creatinine",
    "hs-crp (high-sensitivity c-reactive protein)": "hscrp",
    "hs-crp": "hscrp",
    "hscrp": "hscrp",
    "total testosterone": "total_testosterone",
    "estrogen": "estrogen",
    "shbg": "shbg",
    "ferritin": "ferritin",
    "free t3": "free_t3",
    "vitamin d": "vitamin_d",
    "free testosterone": "free_testosterone",
    "ldl cholesterol": "ldl",
    "ldl/apob ratio": "ldl_apob_ratio",
    "total cholesterol/hdl ratio": "total_chol_hdl_ratio",
    "triglycerides/hdl ratio": "trig_hdl_ratio",
    "remnant cholesterol": "remnant_cholesterol",
    "hba1c": "hba1c",
    "hemoglobin a1c": "hba1c",
    "fasting glucose": "fasting_glucose",
    "insulin": "insulin",
    "lp(a)": "lpa",
    "lipoprotein(a)": "lpa",
    "homocysteine": "homocysteine",
    "uric acid": "uric_acid",
    "alt": "alt",
    "ast": "ast",
    "ggt": "ggt",
    "iron": "iron",
    "tibc": "tibc",
    "iron saturation": "iron_saturation",
    "tsh": "tsh",
    "free t4": "free_t4",
    "dhea-s": "dhea_s",
    "cortisol": "cortisol",
    "psa": "psa",
    "magnesium": "magnesium",
    "zinc": "zinc",
    "b12": "b12",
    "vitamin b12": "b12",
    "folate": "folate",
    "omega-3 index": "omega3_index",
    "white blood cells": "wbc",
    "red blood cells": "rbc",
    "hemoglobin": "hemoglobin",
    "hematocrit": "hematocrit",
    "platelets": "platelets",
}


def normalize_name(raw: str) -> str:
    """Turn a test name into a stable snake_case field name."""
    key = raw.strip().lower()
    if key in _NAME_MAP:
        return _NAME_MAP[key]
    # Fallback: strip parens, replace non-alnum with _, collapse
    cleaned = re.sub(r"\([^)]*\)", "", key)
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")
    return cleaned


# ---------------------------------------------------------------------------
# PDF parser (Rythm Health format)
# ---------------------------------------------------------------------------


def parse_rythm_pdf(path: str) -> dict:
    """Extract test results from a Rythm Health PDF."""
    try:
        import pdfplumber
    except ImportError:
        print(
            "ERROR: pdfplumber is required for PDF import.\n"
            "  Install: pip install pdfplumber\n"
            "  Or provide a JSON file instead.",
            file=sys.stderr,
        )
        sys.exit(1)

    results = []
    collected = None
    fasting = None

    with pdfplumber.open(path) as pdf:
        full_text = ""
        for page in pdf.pages:
            full_text += page.extract_text() or ""

            for table in page.extract_tables() or []:
                for row in table:
                    if not row or len(row) < 3:
                        continue
                    # Skip header rows
                    if row[0] and "Test" in str(row[0]) and "Value" in str(row[1] or ""):
                        continue
                    test_name = (row[0] or "").strip()
                    if not test_name:
                        continue
                    try:
                        value = float(str(row[1]).strip())
                    except (ValueError, TypeError):
                        continue
                    unit = (row[2] or "").strip() if len(row) > 2 else ""

                    range_low = range_high = perf_low = perf_high = None
                    if len(row) > 3 and row[3]:
                        parts = str(row[3]).split("-")
                        if len(parts) == 2:
                            try:
                                range_low = float(parts[0].strip())
                                range_high = float(parts[1].strip())
                            except ValueError:
                                pass
                    if len(row) > 4 and row[4]:
                        parts = str(row[4]).split("-")
                        if len(parts) == 2:
                            try:
                                perf_low = float(parts[0].strip())
                                perf_high = float(parts[1].strip())
                            except ValueError:
                                pass

                    results.append({
                        "test": test_name,
                        "value": value,
                        "unit": unit,
                        "range_low": range_low,
                        "range_high": range_high,
                        "perf_low": perf_low,
                        "perf_high": perf_high,
                    })

        # Extract collection date from text
        match = re.search(r"COLLECTED\s+(\d{1,2}/\d{1,2}/\d{4})", full_text)
        if match:
            collected = datetime.strptime(match.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")

        fasting_match = re.search(r"FASTING\s+(Yes|No)", full_text, re.IGNORECASE)
        if fasting_match:
            fasting = fasting_match.group(1).lower() == "yes"

    return {
        "collected": collected,
        "fasting": fasting,
        "lab": "Rythm Health",
        "results": results,
    }


def parse_json_file(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# InfluxDB writer
# ---------------------------------------------------------------------------


def build_points(data: dict) -> list[dict]:
    collected = data.get("collected")
    if not collected:
        print("ERROR: 'collected' date is required.", file=sys.stderr)
        sys.exit(1)

    ts = datetime.strptime(collected, "%Y-%m-%d").replace(
        hour=6, tzinfo=pytz.utc
    )

    fields = {}
    for r in data.get("results", []):
        name = normalize_name(r["test"])
        value = r.get("value")
        if value is not None:
            fields[name] = float(value)

    if not fields:
        print("ERROR: no valid test results found.", file=sys.stderr)
        sys.exit(1)

    tags = {
        "Lab": data.get("lab", "Unknown"),
        "Fasting": str(data.get("fasting", False)),
    }

    point = {
        "measurement": "BloodTest",
        "time": ts.isoformat(),
        "tags": tags,
        "fields": fields,
    }

    # Also write reference ranges as a separate measurement so the dashboard
    # can query them for dynamic bands.
    ref_fields = {}
    for r in data.get("results", []):
        name = normalize_name(r["test"])
        if r.get("perf_low") is not None:
            ref_fields[f"{name}_perf_low"] = float(r["perf_low"])
        if r.get("perf_high") is not None:
            ref_fields[f"{name}_perf_high"] = float(r["perf_high"])
        if r.get("range_low") is not None:
            ref_fields[f"{name}_range_low"] = float(r["range_low"])
        if r.get("range_high") is not None:
            ref_fields[f"{name}_range_high"] = float(r["range_high"])

    points = [point]
    if ref_fields:
        points.append({
            "measurement": "BloodTestRanges",
            "time": ts.isoformat(),
            "tags": tags,
            "fields": ref_fields,
        })

    return points


def write_to_influxdb(points: list[dict]) -> None:
    from influxdb import InfluxDBClient

    host = os.getenv("INFLUXDB_HOST", "localhost")
    port = int(os.getenv("INFLUXDB_PORT", "8086"))
    user = os.getenv("INFLUXDB_USERNAME", "influxdb_user")
    password = os.getenv("INFLUXDB_PASSWORD", "influxdb_secret_password")
    database = os.getenv("INFLUXDB_DATABASE", "GarminStats")

    client = InfluxDBClient(host=host, port=port, username=user, password=password, database=database)
    client.write_points(points)
    print(f"Wrote {len(points)} point(s) to InfluxDB ({host}:{port}/{database})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    if dry_run:
        args.remove("--dry-run")

    if not args:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    path = Path(args[0])
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    if path.suffix.lower() == ".pdf":
        data = parse_rythm_pdf(str(path))
    elif path.suffix.lower() == ".json":
        data = parse_json_file(str(path))
    else:
        print(f"ERROR: unsupported file type: {path.suffix}", file=sys.stderr)
        sys.exit(1)

    print(f"Parsed {len(data.get('results', []))} biomarkers from {path.name}")
    print(f"  Collection date: {data.get('collected')}")
    print(f"  Fasting: {data.get('fasting')}")
    print(f"  Lab: {data.get('lab')}")

    points = build_points(data)

    if dry_run:
        print("\n--- DRY RUN (not writing to InfluxDB) ---")
        for p in points:
            print(f"\nMeasurement: {p['measurement']}")
            print(f"  Time: {p['time']}")
            print(f"  Tags: {p['tags']}")
            for k, v in sorted(p["fields"].items()):
                print(f"  {k}: {v}")
    else:
        write_to_influxdb(points)


if __name__ == "__main__":
    main()
