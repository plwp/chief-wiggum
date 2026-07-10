#!/usr/bin/env python3
"""duplication.py — production copy/paste ratio via jscpd.

Literature signal (GitClear [VENDOR]): duplicated/copy-pasted code is one of the
"AI-slop" signals the field converged on — code "written to be added, not
refactored/reused." GitClear baselines: pre-AI 2020 ~8.3% duplicated blocks,
AI-assisted 2024 ~12.3%. We measure PRODUCTION code only (tests, node_modules,
docs, vendor, build output excluded) so the figure is comparable to those bands.

Runs the ``jscpd`` CLI (npm i -g jscpd, or ``npx jscpd``) and parses its
``jscpd-report.json`` ``statistics.total``. Requires node + jscpd; if either is
absent, ``analyze`` returns ``{"skipped": ...}`` rather than raising.

As a module:
    from quality.duplication import analyze
    result = analyze("/path/to/repo", workdir="/tmp/dup")

As a CLI:
    python3 -m quality.duplication <repo> --workdir <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

# Production-only: exclude tests, generated, vendored, docs, node output.
IGNORE = ",".join([
    "**/node_modules/**", "**/dist/**", "**/build/**", "**/out/**", "**/.next/**",
    "**/vendor/**", "**/.venv/**", "**/venv/**", "**/__pycache__/**",
    "**/coverage/**", "**/docs/**", "**/migrations/**",
    "**/*_test.go", "**/*.test.*", "**/*.spec.*",
    "**/test_*.py", "**/*_test.py", "**/tests/**", "**/__tests__/**", "**/e2e/**",
])
FORMATS = "python,go,typescript,tsx,javascript,jsx"


def _round(v: float | None, ndigits: int = 2) -> float | None:
    return round(v, ndigits) if isinstance(v, (int, float)) else v


def _jscpd_cmd() -> list[str] | None:
    """Resolve how to invoke jscpd: direct binary, else npx. None if node absent."""
    direct = shutil.which("jscpd")
    if direct:
        return [direct]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "jscpd"]
    return None


def analyze(repo: str, workdir: str, name: str | None = None) -> dict:
    """Run jscpd over production code and return the duplication statistics."""
    name = name or repo.rstrip("/").split("/")[-1]
    cmd = _jscpd_cmd()
    if not cmd:
        return {
            "repo": name,
            "skipped": "jscpd/node not found",
            "note": "duplication requires node + jscpd (npm i -g jscpd)",
        }
    os.makedirs(workdir, exist_ok=True)
    proc = subprocess.run(
        [
            *cmd, repo,
            "--reporters", "json",
            "--output", workdir,
            "--ignore", IGNORE,
            "--format", FORMATS,
            "--mode", "strict",
            "--silent",
        ],
        capture_output=True, text=True,
    )
    report = os.path.join(workdir, "jscpd-report.json")
    if not os.path.exists(report):
        return {
            "repo": name,
            "skipped": "jscpd produced no report",
            "note": (proc.stderr or proc.stdout or "").strip()[:400],
        }
    try:
        with open(report) as fh:
            stats = json.load(fh)["statistics"]["total"]
    except (json.JSONDecodeError, KeyError, OSError) as exc:
        return {"repo": name, "skipped": f"unreadable jscpd report: {exc}"}

    return {
        "repo": name,
        "lines": stats.get("lines"),
        "tokens": stats.get("tokens"),
        "sources": stats.get("sources"),
        "clones": stats.get("clones"),
        "duplicated_lines": stats.get("duplicatedLines"),
        "duplicated_tokens": stats.get("duplicatedTokens"),
        "duplication_pct_lines": _round(stats.get("percentage")),
        "duplication_pct_tokens": _round(stats.get("percentageTokens")),
        "baselines": {
            "gitclear_pre_ai_2020": 8.3,
            "gitclear_ai_2024": 12.3,
            "note": "GitClear [VENDOR] copy/paste baselines (% duplicated blocks); "
                    "direction is credible, exact multiples are framing-dependent.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="production code duplication (jscpd)")
    parser.add_argument("repo", help="path to the git repository")
    parser.add_argument("--workdir", required=True, help="scratch dir for jscpd output")
    parser.add_argument("--name", default=None, help="display name for the repo")
    args = parser.parse_args()
    print(json.dumps(analyze(args.repo, args.workdir, name=args.name), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
