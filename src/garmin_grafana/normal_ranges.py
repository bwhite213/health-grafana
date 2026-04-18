"""
Profile-driven normal-range stamping for Grafana dashboards.

The user's demographic profile (age, sex, height, weight) comes from
environment variables at runtime - it is NOT stored in any tracked file.
This module:

1. Loads the profile from environment variables.
2. Resolves a catalog of clinical reference ranges (RHR by age x sex,
   sleep duration by age band, etc.) into concrete numeric ranges for
   that profile.
3. Walks the dashboard JSON files in ``Grafana_Dashboard/`` looking for
   panels marked with ``_normalRangeMetric``, and rewrites their
   ``thresholds.steps`` + ``custom.thresholdsStyle.mode`` in place so
   Grafana draws a red / yellow / green area-fill band matching the
   profile's normal range.

The stamper is idempotent: if the computed output is byte-identical to
the current file, it doesn't rewrite, so repeated restarts don't dirty
the git working tree.

Profile environment variables:

    USER_AGE          integer years
    USER_SEX          "male" | "female"
    USER_HEIGHT_CM    integer (currently unused; reserved for BMI etc.)
    USER_WEIGHT_KG    integer (currently unused; reserved for BMI etc.)

If any required variable is missing, stamping is skipped with a warning
and dashboards keep whatever placeholder thresholds they shipped with.

Citations for the catalog live inline as comments next to each entry so
future edits have the reference at hand.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    age: int
    sex: str  # "male" or "female"
    height_cm: int | None = None
    weight_kg: int | None = None


def load_profile_from_env() -> Profile | None:
    age_raw = os.getenv("USER_AGE", "").strip()
    sex_raw = os.getenv("USER_SEX", "").strip().lower()
    if not age_raw or not sex_raw:
        return None
    try:
        age = int(age_raw)
    except ValueError:
        _log.warning("USER_AGE=%r is not an integer; skipping normal-range stamping", age_raw)
        return None
    if sex_raw not in ("male", "female"):
        _log.warning("USER_SEX=%r must be 'male' or 'female'; skipping normal-range stamping", sex_raw)
        return None

    def _opt_int(name: str) -> int | None:
        raw = os.getenv(name, "").strip()
        if not raw:
            return None
        try:
            return int(float(raw))
        except ValueError:
            return None

    return Profile(
        age=age,
        sex=sex_raw,
        height_cm=_opt_int("USER_HEIGHT_CM"),
        weight_kg=_opt_int("USER_WEIGHT_KG"),
    )


# ---------------------------------------------------------------------------
# Catalog - clinical reference ranges keyed by demographic bracket.
# ---------------------------------------------------------------------------
#
# Each metric entry maps a profile bracket to a dict with:
#   normal_min / normal_max   - bounds of the green zone (None = unbounded)
#   borderline_min / borderline_max - bounds of the yellow zone beyond normal
# The red zone is everything outside the borderline zone.

# Resting heart rate by age x sex.
# Source: American Heart Association (adult 60-100 bpm) intersected with
# Cooper Institute age-graded fitness norms - the "normal" band here covers
# the "excellent" through "average" zones; the "borderline" band spans
# "below average" through "poor"; outside borderline is "very poor" / needs
# investigation. Picked slightly fit-leaning thresholds since wearables are
# more commonly used by active populations.
_RHR_BRACKETS: list[tuple[str, int, int, dict[str, int | None]]] = [
    ("male",   18, 29, {"normal_min": 50, "normal_max": 70, "borderline_min": 40, "borderline_max": 85}),
    ("male",   30, 39, {"normal_min": 50, "normal_max": 70, "borderline_min": 40, "borderline_max": 85}),
    ("male",   40, 49, {"normal_min": 52, "normal_max": 72, "borderline_min": 42, "borderline_max": 87}),
    ("male",   50, 59, {"normal_min": 54, "normal_max": 74, "borderline_min": 44, "borderline_max": 90}),
    ("male",   60, 69, {"normal_min": 56, "normal_max": 76, "borderline_min": 46, "borderline_max": 92}),
    ("male",   70, 120, {"normal_min": 58, "normal_max": 78, "borderline_min": 48, "borderline_max": 95}),
    ("female", 18, 29, {"normal_min": 55, "normal_max": 75, "borderline_min": 45, "borderline_max": 90}),
    ("female", 30, 39, {"normal_min": 55, "normal_max": 75, "borderline_min": 45, "borderline_max": 90}),
    ("female", 40, 49, {"normal_min": 57, "normal_max": 77, "borderline_min": 47, "borderline_max": 92}),
    ("female", 50, 59, {"normal_min": 58, "normal_max": 78, "borderline_min": 48, "borderline_max": 93}),
    ("female", 60, 69, {"normal_min": 60, "normal_max": 80, "borderline_min": 50, "borderline_max": 95}),
    ("female", 70, 120, {"normal_min": 62, "normal_max": 82, "borderline_min": 52, "borderline_max": 97}),
]

# Sleep duration by age band.
# Source: CDC + National Sleep Foundation adult recommendations.
#   18-60: 7-9h recommended, <6 or >10 flagged.
#   61-64: 7-9h recommended (same as adult).
#   65+:   7-8h recommended, <6 or >9 flagged.
_SLEEP_HOURS_BRACKETS: list[tuple[int, int, dict[str, float | None]]] = [
    (18, 64, {"normal_min": 7, "normal_max": 9, "borderline_min": 6, "borderline_max": 10}),
    (65, 120, {"normal_min": 7, "normal_max": 8, "borderline_min": 6, "borderline_max": 9}),
]

# Sleep efficiency - age-independent clinical convention.
# Source: AASM clinical practice parameters. >=85% good, 75-84% fair, <75% poor.
_SLEEP_EFFICIENCY_CONSTANT: dict[str, float | None] = {
    "normal_min": 85, "normal_max": None, "borderline_min": 75, "borderline_max": None,
}

# Daily steps - Tudor-Locke adult activity classification.
# Source: Tudor-Locke et al., "How many steps/day are enough?" 2011.
#   <5000 sedentary, 5000-7499 low-active, 7500-9999 somewhat-active, 10000+ active.
# For 65+ we drop the normal floor slightly since walking capacity declines.
_STEPS_BRACKETS: list[tuple[int, int, dict[str, int | None]]] = [
    (18, 64, {"normal_min": 7500, "normal_max": None, "borderline_min": 5000, "borderline_max": None}),
    (65, 120, {"normal_min": 6000, "normal_max": None, "borderline_min": 4000, "borderline_max": None}),
]

# VO2 max (ml/kg/min) by age x sex.
# Source: Cooper Institute aerobic fitness norms. The `normal` band covers
# "Good" through "Superior"; `borderline` extends down through "Average";
# below `borderline_min` is "Below Average / Poor" (red). Cooper thresholds
# drop gradually with age as peak VO2 declines ~10% per decade after 25.
#   Men 20-29: Superior >=58, Excellent 52-57, Good 47-51, Above Avg 42-46, Avg 37-41, Below 33-36, Poor <33
#   Men 30-39: Superior >=54, Excellent 48-53, Good 44-47, Above Avg 40-43, Avg 36-39, Below 32-35, Poor <32
#   Men 40-49: Superior >=50, Excellent 45-49, Good 41-44, Above Avg 37-40, Avg 33-36, Below 29-32, Poor <29
#   Men 50-59: Superior >=46, Excellent 42-45, Good 38-41, Above Avg 34-37, Avg 30-33, Below 26-29, Poor <26
#   Men 60+:   Superior >=43, Excellent 39-42, Good 35-38, Above Avg 31-34, Avg 26-30, Below 22-25, Poor <22
# Female norms run ~6-8 ml/kg/min lower across the same brackets.
_VO2_MAX_BRACKETS: list[tuple[str, int, int, dict[str, float | None]]] = [
    ("male",   18, 29, {"normal_min": 47, "normal_max": None, "borderline_min": 37, "borderline_max": None}),
    ("male",   30, 39, {"normal_min": 44, "normal_max": None, "borderline_min": 36, "borderline_max": None}),
    ("male",   40, 49, {"normal_min": 41, "normal_max": None, "borderline_min": 33, "borderline_max": None}),
    ("male",   50, 59, {"normal_min": 38, "normal_max": None, "borderline_min": 30, "borderline_max": None}),
    ("male",   60, 120, {"normal_min": 35, "normal_max": None, "borderline_min": 26, "borderline_max": None}),
    ("female", 18, 29, {"normal_min": 41, "normal_max": None, "borderline_min": 32, "borderline_max": None}),
    ("female", 30, 39, {"normal_min": 38, "normal_max": None, "borderline_min": 30, "borderline_max": None}),
    ("female", 40, 49, {"normal_min": 35, "normal_max": None, "borderline_min": 27, "borderline_max": None}),
    ("female", 50, 59, {"normal_min": 32, "normal_max": None, "borderline_min": 25, "borderline_max": None}),
    ("female", 60, 120, {"normal_min": 29, "normal_max": None, "borderline_min": 22, "borderline_max": None}),
]


# Blood pressure (AHA 2017 guidelines, age-independent).
# Source: American Heart Association / American College of Cardiology 2017:
#   Normal: <120/<80, Elevated: 120-129/<80,
#   Stage 1 HTN: 130-139/80-89, Stage 2 HTN: ≥140/≥90.
# These are "lower is better" metrics — green zone is at the bottom.
_BP_SYSTOLIC_CONSTANT: dict[str, float | None] = {
    "normal_max": 120, "borderline_max": 140,
    "lower_is_better": True,
}
_BP_DIASTOLIC_CONSTANT: dict[str, float | None] = {
    "normal_max": 80, "borderline_max": 90,
    "lower_is_better": True,
}

# Acute:Chronic Workload Ratio — sport science consensus.
# Source: Gabbett 2016, Hulin et al. 2014 (BJSM):
#   0.8-1.3 = "sweet spot" (progressive overload with low injury risk)
#   0.6-0.8 / 1.3-1.5 = borderline (under/overreaching)
#   <0.6 = detraining, >1.5 = high injury risk
_ACWR_CONSTANT: dict[str, float | None] = {
    "normal_min": 0.8, "normal_max": 1.3,
    "borderline_min": 0.6, "borderline_max": 1.5,
}


def _lookup_age_sex(profile: Profile, table: list) -> dict | None:
    for sex, lo, hi, ranges in table:
        if sex == profile.sex and lo <= profile.age <= hi:
            return ranges
    return None


def _lookup_age(profile: Profile, table: list) -> dict | None:
    for lo, hi, ranges in table:
        if lo <= profile.age <= hi:
            return ranges
    return None


def resolve(profile: Profile) -> dict[str, dict]:
    """
    Turn a profile into a flat ``{metric_key: ranges}`` mapping.

    Metric keys match the ``_normalRangeMetric`` marker strings used in the
    dashboard JSON files. Missing lookups are silently omitted - panels that
    don't resolve will keep whatever placeholder thresholds they ship with.
    """
    out: dict[str, dict] = {}

    rhr = _lookup_age_sex(profile, _RHR_BRACKETS)
    if rhr is not None:
        out["resting_heart_rate"] = rhr

    sleep_hours = _lookup_age(profile, _SLEEP_HOURS_BRACKETS)
    if sleep_hours is not None:
        out["sleep_duration_hours"] = sleep_hours

    out["sleep_efficiency_pct"] = _SLEEP_EFFICIENCY_CONSTANT

    steps = _lookup_age(profile, _STEPS_BRACKETS)
    if steps is not None:
        out["daily_steps"] = steps

    vo2 = _lookup_age_sex(profile, _VO2_MAX_BRACKETS)
    if vo2 is not None:
        out["vo2_max"] = vo2

    out["systolic_bp"] = _BP_SYSTOLIC_CONSTANT
    out["diastolic_bp"] = _BP_DIASTOLIC_CONSTANT
    out["acwr"] = _ACWR_CONSTANT

    return out


# ---------------------------------------------------------------------------
# Threshold-step generator
# ---------------------------------------------------------------------------


def build_threshold_steps(ranges: dict) -> list[dict]:
    """
    Turn a range dict into a Grafana `thresholds.steps` list with colors.

    For standard metrics (higher-in-range = good):
      red (below) -> yellow -> green -> yellow -> red (above)

    For ``lower_is_better`` metrics (like blood pressure):
      green (below) -> yellow -> red (above)

    Any of those transitions are skipped when the corresponding bound is
    ``None`` or equal to its neighbor.
    """
    if ranges.get("lower_is_better"):
        normal_max = ranges.get("normal_max")
        borderline_max = ranges.get("borderline_max")
        steps: list[dict] = [{"color": "green", "value": None}]
        if normal_max is not None:
            steps.append({"color": "yellow", "value": normal_max})
        if borderline_max is not None and borderline_max != normal_max:
            steps.append({"color": "red", "value": borderline_max})
        return steps

    normal_min = ranges.get("normal_min")
    normal_max = ranges.get("normal_max")
    borderline_min = ranges.get("borderline_min")
    borderline_max = ranges.get("borderline_max")

    steps = [{"color": "red", "value": None}]
    if borderline_min is not None and borderline_min != normal_min:
        steps.append({"color": "yellow", "value": borderline_min})
    if normal_min is not None:
        steps.append({"color": "green", "value": normal_min})
    if normal_max is not None:
        if borderline_max is not None and borderline_max != normal_max:
            steps.append({"color": "yellow", "value": normal_max})
            steps.append({"color": "red", "value": borderline_max})
        else:
            steps.append({"color": "red", "value": normal_max})
    return steps


# ---------------------------------------------------------------------------
# Dashboard stamper
# ---------------------------------------------------------------------------


MARKER_KEY = "_normalRangeMetric"


def _iter_panels(dashboard: dict[str, Any]):
    for panel in dashboard.get("panels", []) or []:
        yield panel
        yield from panel.get("panels", []) or []


def _apply_to_panel(panel: dict, resolved: dict[str, dict]) -> bool:
    metric = panel.get(MARKER_KEY)
    if not metric:
        return False
    ranges = resolved.get(metric)
    if not ranges:
        return False
    steps = build_threshold_steps(ranges)
    defaults = panel.setdefault("fieldConfig", {}).setdefault("defaults", {})
    new_thresholds = {"mode": "absolute", "steps": steps}
    if defaults.get("thresholds") == new_thresholds:
        needs_style_fix = (
            defaults.get("custom", {}).get("thresholdsStyle", {}).get("mode") != "area"
        )
        if not needs_style_fix:
            return False
    defaults["thresholds"] = new_thresholds
    defaults.setdefault("custom", {})["thresholdsStyle"] = {"mode": "area"}
    return True


def stamp_dashboard_file(path: Path, resolved: dict[str, dict]) -> bool:
    try:
        original_bytes = path.read_bytes()
        dashboard = json.loads(original_bytes)
    except (OSError, json.JSONDecodeError) as err:
        _log.warning("Could not read dashboard %s: %s", path, err)
        return False

    changed = False
    for panel in _iter_panels(dashboard):
        if _apply_to_panel(panel, resolved):
            changed = True

    if not changed:
        return False

    new_bytes = (json.dumps(dashboard, indent=2) + "\n").encode("utf-8")
    if new_bytes == original_bytes:
        return False
    try:
        path.write_bytes(new_bytes)
    except OSError as err:
        _log.warning("Could not write stamped dashboard %s: %s", path, err)
        return False
    return True


def stamp_dashboards(dashboard_dir: Path | str, profile: Profile) -> int:
    """
    Apply resolved normal ranges to every ``*.json`` in ``dashboard_dir``.
    Returns the number of files rewritten.
    """
    directory = Path(dashboard_dir)
    if not directory.is_dir():
        _log.warning("Dashboard directory %s does not exist; skipping normal-range stamping", directory)
        return 0

    resolved = resolve(profile)
    if not resolved:
        _log.warning(
            "No normal ranges resolved for profile age=%s sex=%s; skipping stamping",
            profile.age,
            profile.sex,
        )
        return 0

    rewritten = 0
    for path in sorted(directory.glob("*.json")):
        if stamp_dashboard_file(path, resolved):
            rewritten += 1
            _log.info("Stamped normal ranges into %s", path.name)
    _log.info(
        "Normal-range stamping complete: %d dashboard(s) rewritten (profile: %dyo %s)",
        rewritten,
        profile.age,
        profile.sex,
    )
    return rewritten


def stamp_dashboards_from_env(dashboard_dir: Path | str = "/app/Grafana_Dashboard") -> None:
    """Entry point used by the orchestrator at startup. Fails soft."""
    profile = load_profile_from_env()
    if profile is None:
        _log.info(
            "USER_AGE/USER_SEX not set - skipping dashboard normal-range stamping. "
            "Set them in override-default-vars.env to enable profile-driven bands."
        )
        return
    try:
        stamp_dashboards(dashboard_dir, profile)
    except Exception as err:  # noqa: BLE001
        _log.warning("Normal-range dashboard stamping failed: %s", err)
