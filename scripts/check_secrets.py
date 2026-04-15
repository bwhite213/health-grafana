#!/usr/bin/env python3
"""
Scan tracked files for secrets that look like live credentials.

Designed to be called from two places:

1. A git pre-commit hook (`.pre-commit-config.yaml` below or
   `git config core.hooksPath .githooks`) so that the commit is blocked
   before the bad content ever leaves the developer's machine.

2. The CI workflow (`.github/workflows/ci.yml`) so that even if a
   contributor doesn't have the pre-commit hook installed, the PR is
   flagged automatically.

The patterns target the specific credential shapes this project uses:

- ``OURA_PERSONAL_ACCESS_TOKEN=<32+ uppercase alphanumerics>`` — the shape
  Oura Cloud issues for personal access tokens.
- ``GARMINCONNECT_BASE64_PASSWORD=<8+ base64 chars>`` — any non-empty
  base64-encoded password.
- ``GARMINCONNECT_EMAIL=<real-looking email>`` — any address that's not
  the placeholder in the template.

``override-default-vars.env.example`` is explicitly allowlisted because it
ships with intentionally-empty values. ``override-default-vars.env`` is
gitignored so it should never show up in the file list anyway; if it does,
that's itself a finding and we fail loudly.

Exit code:
    0 - clean (no findings)
    1 - one or more findings (blocks commit / fails CI)
    2 - usage error
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
from collections.abc import Iterable

# Files that are allowed to contain placeholder-shaped credentials.
ALLOWLIST = {
    "override-default-vars.env.example",
    "scripts/check_secrets.py",  # this file (our patterns would match themselves)
    ".github/workflows/ci.yml",  # CI invokes the scanner and may log patterns
    "docs/ubuntu-deployment.md",  # contains intentional placeholders for copy-paste
    "docs/oura-setup.md",  # contains intentional PAT placeholder
}

# If any of these paths show up in git's tracked-files output, that's itself
# a finding — these are supposed to be gitignored.
FORBIDDEN_PATHS = {
    "override-default-vars.env",
}

# Patterns that indicate a live secret. Each entry is (label, regex).
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "Oura Personal Access Token",
        re.compile(r"OURA_PERSONAL_ACCESS_TOKEN\s*=\s*([A-Z0-9]{32,})"),
    ),
    (
        "Garmin base64 password",
        re.compile(
            r"GARMINCONNECT_BASE64_PASSWORD\s*=\s*([A-Za-z0-9+/]{8,}={0,2})"
        ),
    ),
    (
        "Garmin email",
        # Any real-looking email address on a GARMINCONNECT_EMAIL line.
        # Placeholder `you@example.com` is caught by the allowlist file check,
        # not here — the scanner flags any value because we don't want even the
        # placeholder email leaking from personal clones.
        re.compile(
            r"GARMINCONNECT_EMAIL\s*=\s*([^\s#@]+@[^\s#]+\.[A-Za-z]{2,})"
        ),
    ),
]


def tracked_files() -> list[pathlib.Path]:
    """Return the list of files tracked by git (respects .gitignore)."""
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Fallback: walk the filesystem but respect obvious ignores.
        return [p for p in pathlib.Path(".").rglob("*") if p.is_file()]
    return [pathlib.Path(line) for line in out.splitlines() if line]


def scan_file(path: pathlib.Path) -> list[tuple[int, str, str]]:
    """Return a list of (lineno, label, snippet) findings for one file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return []
    findings: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pattern in PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            # Never show the captured value in the finding — that would just
            # re-leak it into CI logs. Report the label and surrounding key.
            key = line.split("=", 1)[0].strip()
            findings.append((lineno, label, key))
    return findings


def scan(files: Iterable[pathlib.Path]) -> int:
    total = 0
    for path in files:
        rel = str(path).replace("\\", "/")
        if rel in FORBIDDEN_PATHS:
            print(
                f"FAIL  {rel}: forbidden gitignored file is tracked — remove it"
            )
            total += 1
            continue
        if rel in ALLOWLIST:
            continue
        for lineno, label, key in scan_file(path):
            print(f"FAIL  {rel}:{lineno}: {label} on `{key}`")
            total += 1
    return total


def main(argv: list[str]) -> int:
    if len(argv) == 1:
        files = tracked_files()
    else:
        # Pre-commit passes the list of staged files as arguments.
        files = [pathlib.Path(p) for p in argv[1:]]
    total = scan(files)
    if total:
        print(
            f"\n{total} potential secret(s) detected. "
            "If this is a false positive, add the file to ALLOWLIST in "
            "scripts/check_secrets.py.",
            file=sys.stderr,
        )
        return 1
    print(f"scanned {sum(1 for _ in files) if isinstance(files, list) else '?'} files — no secrets detected")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
