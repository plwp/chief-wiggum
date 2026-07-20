#!/usr/bin/env python3
"""quality_slop_gate.py — report-only AI-"slop" signals for /close-epic.

Two signals the literature converged on for AI-generated code degradation:

  - **code survival / 2-week churn** (GitClear [VENDOR]; DORA 2024 stability drop):
    AI-assisted code is reverted/reworked soon after authoring. We report the
    fraction of added lines still alive at 14/30 days via git-of-theseus, placed
    against GitClear's bands (pre-AI 2020 ~96.9% survive 2 weeks; AI-assisted
    2024 ~94.3%).
  - **production duplication** (GitClear [VENDOR]): copy/paste written to be added
    not reused. We report production-only clone % (tests excluded) via jscpd,
    against GitClear's bands (pre-AI 8.3%; AI 12.3%).

**Report-only** per docs/gate-rollout.md: compute, print against the reference
bands, exit 0 — never block. A future blocking mode is behind ``--gate`` (off by
default). The bands are directional (corroborated by DORA 2024; the exact
multiples are vendor framing, not independently replicated) — they are printed
as context, and even in ``--gate`` mode only regressions *past* the AI band
count as findings.

Degrades gracefully: git-of-theseus / jscpd / node absent -> "skipped (tool not
found)", exit 0, never crash. Survival also self-skips when the repo has < 14
days of history (too young to measure 2-week survival) — surfaced honestly.

Target resolution mirrors the other skills:
  - ``owner/repo``  -> resolved & cloned via scripts/repo.py
  - ``--repo PATH`` -> a direct local path
  - neither         -> the current git repo (git rev-parse --show-toplevel)

Usage:
    python3 scripts/quality_slop_gate.py [owner/repo] [--repo PATH] \\
        [--report | --gate] [--workdir DIR]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.hashing import scanner_version  # noqa: E402
from quality import duplication, survival  # noqa: E402

# GitClear [VENDOR] reference bands — direction is credible, exact multiples are
# framing-dependent (corroborated directionally by DORA 2024, not independently
# replicated). Kept here so the gate's messaging can't drift from the engines.
SURVIVAL_PRE_AI_14D = 96.9
SURVIVAL_AI_14D = 94.3
DUP_PRE_AI = 8.3
DUP_AI = 12.3

CAVEAT = (
    "[VENDOR] GitClear bands; directional (corroborated by DORA 2024), "
    "exact multiples not independently replicated."
)


def _current_repo_root() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        return out.stdout.strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def resolve_target(owner_repo: str | None, repo_path: str | None) -> str:
    """Resolve the target repo to a local absolute path."""
    if repo_path:
        p = Path(repo_path).expanduser().resolve()
        if not (p / ".git").exists():
            print(f"Error: {p} is not a git repository", file=sys.stderr)
            sys.exit(1)
        return str(p)
    if owner_repo:
        from repo import resolve_repo  # local import: only needed for owner/repo
        return str(resolve_repo(owner_repo))
    root = _current_repo_root()
    if not root:
        print("Error: not inside a git repo; pass owner/repo or --repo PATH", file=sys.stderr)
        sys.exit(1)
    return root


def _band(value: float | None, pre_ai: float, ai: float, higher_is_better: bool) -> str:
    """Classify a measured value against the pre-AI / AI reference bands.

    Returns one of: better-than-pre-ai, pre-ai..ai, past-ai (the finding band).
    """
    if value is None:
        return "unknown"
    if higher_is_better:  # survival: higher survival is healthier
        if value >= pre_ai:
            return "better-than-pre-ai"
        if value >= ai:
            return "pre-ai..ai"
        return "past-ai"
    # duplication: lower is healthier
    if value <= pre_ai:
        return "better-than-pre-ai"
    if value <= ai:
        return "pre-ai..ai"
    return "past-ai"


def evaluate_survival(result: dict) -> dict:
    """Reduce a survival engine result to a report-friendly verdict.

    verdict.status: 'skipped' | 'too_young' | 'measured'
    On 'measured', carries survival_14d/30d and the 14-day band classification.
    """
    if "skipped" in result:
        return {"status": "skipped", "detail": result["skipped"]}
    by_age = result.get("survival_by_age_days", {})
    # git-of-theseus ran but no commit has lived >= 14 days -> repo too young.
    band14 = by_age.get(14) or by_age.get("14") or {}
    if not band14 or (band14.get("lines_old_enough") or 0) == 0:
        return {
            "status": "too_young",
            "detail": "no commits have lived >= 14 days — too young to measure 2-week survival",
        }
    band30 = by_age.get(30) or by_age.get("30") or {}
    s14 = band14.get("survival_pct")
    s30 = band30.get("survival_pct")
    return {
        "status": "measured",
        "survival_14d": s14,
        "survival_30d": s30,
        "lines_14d": band14.get("lines_old_enough"),
        "band_14d": _band(s14, SURVIVAL_PRE_AI_14D, SURVIVAL_AI_14D, higher_is_better=True),
    }


def evaluate_duplication(result: dict) -> dict:
    """Reduce a duplication engine result to a report-friendly verdict."""
    if "skipped" in result:
        return {"status": "skipped", "detail": result["skipped"]}
    pct = result.get("duplication_pct_lines")
    return {
        "status": "measured",
        "duplication_pct": pct,
        "band": _band(pct, DUP_PRE_AI, DUP_AI, higher_is_better=False),
    }


def format_report(sv: dict, dup: dict) -> str:
    """Render both verdicts as a human-readable block for the epic report."""
    lines: list[str] = []
    lines.append("## AI-slop signals (report-only)")
    lines.append(f"_{CAVEAT}_")
    lines.append("")

    # --- survival ---
    lines.append("### Code survival (2-week churn)")
    lines.append(
        f"Reference bands: pre-AI ~{SURVIVAL_PRE_AI_14D}% / AI-assisted ~{SURVIVAL_AI_14D}% "
        "of added lines survive 14 days."
    )
    if sv["status"] == "skipped":
        lines.append(f"- skipped: {sv['detail']}")
    elif sv["status"] == "too_young":
        lines.append(f"- skipped: {sv['detail']}")
    else:
        s14 = sv["survival_14d"]
        s30 = sv["survival_30d"]
        lines.append(
            f"- 14-day survival: {s14}% ({_band_label(sv['band_14d'])}); "
            f"30-day survival: {s30}%"
        )
    lines.append("")

    # --- duplication ---
    lines.append("### Production duplication (copy/paste)")
    lines.append(
        f"Reference bands: pre-AI {DUP_PRE_AI}% / AI-assisted {DUP_AI}% duplicated blocks "
        "(production code only, tests excluded)."
    )
    if dup["status"] == "skipped":
        lines.append(f"- skipped: {dup['detail']}")
    else:
        pct = dup["duplication_pct"]
        lines.append(f"- production duplication: {pct}% ({_band_label(dup['band'])})")
    lines.append("")
    return "\n".join(lines)


def _band_label(band: str) -> str:
    return {
        "better-than-pre-ai": "beats the pre-AI human baseline",
        "pre-ai..ai": "between the pre-AI and AI-assisted bands",
        "past-ai": "past the AI-assisted band — worth a look",
        "unknown": "unclassified",
    }.get(band, band)


def has_findings(sv: dict, dup: dict) -> list[str]:
    """Return the list of findings (only 'past-ai' regressions count)."""
    findings: list[str] = []
    if sv.get("status") == "measured" and sv.get("band_14d") == "past-ai":
        findings.append(
            f"code survival {sv['survival_14d']}% at 14 days is below the AI-assisted "
            f"band (~{SURVIVAL_AI_14D}%) — elevated 2-week churn"
        )
    if dup.get("status") == "measured" and dup.get("band") == "past-ai":
        findings.append(
            f"production duplication {dup['duplication_pct']}% exceeds the AI-assisted "
            f"band ({DUP_AI}%)"
        )
    return findings


def run(args: argparse.Namespace) -> int:
    target = resolve_target(args.owner_repo, args.repo)
    if args.workdir:
        workdir = args.workdir
    else:
        import env  # session temp dir under ~/.chief-wiggum/tmp — never the target repo
        workdir = os.path.join(str(env.create_tmp()), "slop-gate", Path(target).name)
    os.makedirs(workdir, exist_ok=True)

    print(f"[slop-gate] target: {target}", file=sys.stderr)
    print("[slop-gate] survival...", file=sys.stderr)
    sv_raw = survival.analyze(target, workdir=os.path.join(workdir, "survival"))
    print("[slop-gate] duplication...", file=sys.stderr)
    dup_raw = duplication.analyze(target, workdir=os.path.join(workdir, "dup"))

    sv = evaluate_survival(sv_raw)
    dup = evaluate_duplication(dup_raw)

    print(format_report(sv, dup))

    findings = has_findings(sv, dup)
    if findings:
        print("Findings (informational):", file=sys.stderr)
        for f in findings:
            print(f"  - {f}", file=sys.stderr)

    # Report-only is the default (exit 0). --gate opts into blocking, and even
    # then only a regression PAST the AI band blocks — the bands are directional.
    if args.gate and findings:
        return 1
    return 0


def _scanner_version() -> str:
    """Hash-derived ``--scanner-version``: the source of this module plus its
    finding-affecting local dependencies. No hand-bumped constant to forget
    (INV-fh-005). The ``quality`` engine modules shape the verdicts ``run()``
    reports (survival/duplication result dicts feed the banding), so they are
    hash inputs — omitting them was the exact CTR-fh-041 silent-staleness class
    this gate's own dep-completeness test polices.
    @cw-trace guards CTR-fh-040 CTR-fh-041 CTR-fh-042"""
    here = Path(__file__).resolve()
    cw_dir = here.parent / "chief_wiggum"
    q_dir = here.parent / "quality"
    return scanner_version(
        here,
        cw_dir / "hashing.py",
        q_dir / "survival.py",
        q_dir / "duplication.py",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="report-only AI-slop signals (code survival + duplication)",
    )
    parser.add_argument("owner_repo", nargs="?", default=None,
                        help="owner/repo to resolve+clone (optional)")
    parser.add_argument("--repo", default=None, help="direct local repo path")
    parser.add_argument("--workdir", default=None, help="scratch dir for tool output")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--report", action="store_true", default=True,
                      help="report-only mode (default): print findings, exit 0")
    mode.add_argument("--gate", action="store_true",
                      help="blocking mode (opt-in): exit 1 if a signal is past the AI band")
    parser.add_argument(
        "--scanner-version",
        action="store_true",
        help="Print the hash-derived scanner version (source hash of this module + its "
        "chief_wiggum deps) and exit",
    )
    args = parser.parse_args()

    if args.scanner_version:
        print(_scanner_version())
        return 0

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
