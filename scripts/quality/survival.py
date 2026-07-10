#!/usr/bin/env python3
"""survival.py — code-survival / 2-week churn from git-of-theseus output.

Literature signal (GitClear/DORA): AI-generated code shows elevated churn —
lines reverted/reworked soon after authoring. Code SURVIVAL is the inverse: of
the lines a commit added, what fraction are still alive after age dt? GitClear
baselines: pre-AI 2020 ~96.9% survive 2 weeks; AI-assisted 2024 ~94.3% (5.7%
churn). This engine reports survival at 14/30/60 days so a repo can be placed
against those bands.

git-of-theseus survival.json = {commit_hash: [[unix_ts, lines_alive], ...]}
(monotonic non-increasing). We anchor each commit to its author time, convert
each snapshot to an AGE in days, normalise by the commit's initial line count,
and aggregate a line-weighted survival curve across all commits.

Requires the ``git-of-theseus-analyze`` CLI (pip install git-of-theseus). If it
is absent, ``analyze`` returns ``{"skipped": ...}`` rather than raising.

As a module:
    from quality.survival import analyze
    result = analyze("/path/to/repo", workdir="/tmp/survival")

As a CLI:
    python3 -m quality.survival <repo> --workdir <dir>
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import shutil
import subprocess
import sys

AGES = [7, 14, 30, 60, 90]


def _run_git_of_theseus(repo: str, outdir: str) -> str | None:
    """Run git-of-theseus-analyze into outdir. Returns survival.json path or None."""
    # Prefer a tool co-located with the running interpreter (venv install),
    # then fall back to PATH — mirrors quality.complexity._tool discovery.
    sibling = os.path.join(os.path.dirname(sys.executable), "git-of-theseus-analyze")
    tool = sibling if os.path.exists(sibling) else shutil.which("git-of-theseus-analyze")
    if not tool:
        return None
    os.makedirs(outdir, exist_ok=True)
    subprocess.run(
        # weekly interval → enough resolution for 14/30-day survival bins
        [tool, repo, "--outdir", outdir, "--branch", "HEAD", "--interval", "604800"],
        capture_output=True, text=True,
    )
    survival = os.path.join(outdir, "survival.json")
    return survival if os.path.exists(survival) else None


def analyze_survival_json(survival_path: str, repo: str, name: str | None = None) -> dict:
    """Analyze an existing git-of-theseus survival.json against author times."""
    name = name or repo.rstrip("/").split("/")[-1]
    with open(survival_path) as fh:
        surv = json.load(fh)

    hashes = list(surv)
    authored: dict[str, int] = {}
    if hashes:
        out = subprocess.run(
            ["git", "-C", repo, "show", "-s", "--format=%H %at", *hashes],
            capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            if " " in line:
                h, at = line.split()
                authored[h] = int(at)

    agg = {a: {"num": 0.0, "den": 0.0} for a in AGES}
    curve: list[tuple[float, float, int]] = []
    total_lines = 0
    for h, series in surv.items():
        ct = authored.get(h)
        if ct is None or not series:
            continue
        L0 = series[0][1]
        if L0 <= 0:
            continue
        total_lines += L0
        pts = [((ts - ct) / 86400.0, min(1.0, lines / L0)) for ts, lines in series]
        pts = [(a, f) for a, f in pts if a >= 0]
        if not pts:
            continue
        ages = [a for a, _ in pts]
        fracs = [f for _, f in pts]
        max_age = ages[-1]
        for A in AGES:
            if max_age >= A:  # commit has lived at least A days -> can measure
                i = bisect.bisect_left(ages, A)
                if i == 0:
                    f = fracs[0]
                elif i >= len(ages):
                    f = fracs[-1]
                else:
                    a0, a1 = ages[i - 1], ages[i]
                    f0, f1 = fracs[i - 1], fracs[i]
                    f = f0 + (f1 - f0) * ((A - a0) / (a1 - a0)) if a1 > a0 else f1
                agg[A]["num"] += f * L0
                agg[A]["den"] += L0
        for a, f in pts:
            curve.append((a, f, L0))

    result: dict = {
        "repo": name, "total_lines_tracked": total_lines,
        "survival_by_age_days": {},
    }
    for A in AGES:
        d = agg[A]["den"]
        result["survival_by_age_days"][A] = {
            "survival_pct": round(100 * agg[A]["num"] / d, 1) if d else None,
            "lines_old_enough": int(d),
        }

    # half-life: line-weighted survival across weekly age bins; first bin < 50%
    bins: dict[int, list[float]] = {}
    for a, f, w in curve:
        b = int(a // 7) * 7
        bins.setdefault(b, [0.0, 0.0])
        bins[b][0] += f * w
        bins[b][1] += w
    curve_pts = sorted((b, num / den) for b, (num, den) in bins.items() if den)
    half_life = None
    for b, s in curve_pts:
        if s < 0.5:
            half_life = b
            break
    result["half_life_days"] = half_life if half_life is not None else ">observed"
    result["weekly_survival_curve"] = [
        {"age_days": b, "survival": round(s, 3)} for b, s in curve_pts
    ]
    result["baselines"] = {
        "gitclear_pre_ai_2020_14d": 96.9,
        "gitclear_ai_2024_14d": 94.3,
        "note": "GitClear [VENDOR] longitudinal churn baselines — direction, not gospel.",
    }
    return result


def analyze(repo: str, workdir: str, name: str | None = None) -> dict:
    """Run git-of-theseus then analyze survival. Degrades gracefully if absent."""
    name = name or repo.rstrip("/").split("/")[-1]
    survival_path = _run_git_of_theseus(repo, workdir)
    if not survival_path:
        return {
            "repo": name,
            "skipped": "git-of-theseus not found",
            "note": "code survival requires git-of-theseus (pip install git-of-theseus)",
        }
    return analyze_survival_json(survival_path, repo, name=name)


def main() -> int:
    parser = argparse.ArgumentParser(description="code survival / 2-week churn")
    parser.add_argument("repo", help="path to the git repository")
    parser.add_argument("--workdir", required=True, help="scratch dir for git-of-theseus output")
    parser.add_argument("--name", default=None, help="display name for the repo")
    args = parser.parse_args()
    print(json.dumps(analyze(args.repo, args.workdir, name=args.name), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
