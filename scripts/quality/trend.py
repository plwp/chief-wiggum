#!/usr/bin/env python3
"""trend.py — complexity/scale trend across git history for one repo.

Samples N evenly-spaced commits on the first-parent (main) line, checks each
out into a throwaway ``git worktree`` (non-destructive), and recomputes a core
metric set so we can see whether complexity/test-ratio drift as the pipeline
adds code over time. Literature motivation: a HEAD snapshot suffers survivorship
bias — mine history to see whether complexity grows WITH size (the SLOC confound)
or stays flat (good).

Per sample point: date, commit, src_loc, test_loc, test_ratio, functions,
ccn_mean, pct_ccn_gt10, pct_len_gt60.

As a module:
    from quality.trend import analyze
    result = analyze("/path/to/repo", workdir="/tmp/wt", n=10)

As a CLI:
    python3 -m quality.trend <repo> --workdir <dir> [--n 10] [--venv <venv>] [--gobin <gobin>]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from .complexity import _tool, bucket, dist, lizard_ccn, loc_counts


def run(*a, **kw):
    return subprocess.run(a, capture_output=True, text=True, **kw)


def sample_commits(repo: str, n: int) -> list[list[str]]:
    out = run(
        "git", "-C", repo, "log", "--first-parent", "--reverse",
        "--format=%H\t%ad", "--date=short",
    ).stdout.splitlines()
    rows = [line.split("\t") for line in out if "\t" in line]
    if len(rows) <= n:
        return rows
    step = (len(rows) - 1) / (n - 1)
    picks = [rows[round(i * step)] for i in range(n)]
    if picks[-1] != rows[-1]:  # ensure last commit included
        picks[-1] = rows[-1]
    return picks


def measure_at(repo: str, commit: str, lizard_bin: str | None, workdir: str) -> dict:
    wt = os.path.join(workdir, "wt_" + commit[:10])
    run("git", "-C", repo, "worktree", "add", "--detach", "--force", wt, commit)
    try:
        b = bucket(wt)
        src_files: list[str] = []
        test_files: list[str] = []
        for _lang, sets in b.items():
            src_files += sets["src"]
            test_files += sets["test"]
        src_loc = loc_counts(src_files)
        test_loc = loc_counts(test_files)
        d = dist(lizard_ccn(src_files, lizard_bin)) or {}
        return {
            "src_loc": src_loc, "test_loc": test_loc,
            "test_ratio": round(test_loc / src_loc, 2) if src_loc else 0,
            "src_files": len(src_files),
            "functions": d.get("functions"),
            "ccn_mean": d.get("ccn_mean"),
            "pct_ccn_gt10": d.get("pct_ccn_gt10"),
            "pct_len_gt60": d.get("pct_len_gt60"),
        }
    finally:
        run("git", "-C", repo, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)


def analyze(
    repo: str, workdir: str, n: int = 10,
    venv: str | None = None, gobin: str | None = None,
) -> dict:
    """Sample N commits across history and recompute core metrics at each."""
    lizard_bin = _tool("lizard", venv, gobin)
    if not lizard_bin:
        return {
            "repo": repo.rstrip("/").split("/")[-1],
            "skipped": "lizard not found",
            "note": "trend sampling requires lizard (pip install lizard)",
        }
    os.makedirs(workdir, exist_ok=True)
    series: list[dict] = []
    for commit, date in sample_commits(repo, n):
        m = measure_at(repo, commit, lizard_bin, workdir)
        m["commit"] = commit[:10]
        m["date"] = date
        series.append(m)
    return {
        "repo": repo.rstrip("/").split("/")[-1],
        "points": len(series),
        "series": series,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="complexity/scale trend over history")
    parser.add_argument("repo", help="path to the git repository")
    parser.add_argument("--workdir", required=True, help="scratch dir for worktrees")
    parser.add_argument("--n", type=int, default=10, help="number of sample points")
    parser.add_argument("--venv", default=None, help="virtualenv with lizard")
    parser.add_argument("--gobin", default=None, help="dir containing go tools")
    args = parser.parse_args()
    result = analyze(args.repo, args.workdir, n=args.n, venv=args.venv, gobin=args.gobin)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
