#!/usr/bin/env python3
"""churn.py — language-agnostic git-history churn metrics for a repo.

Pure ``git log`` analysis (no checkout, non-destructive). Returns a dict:
  - scale: commits, date range, active days
  - churn: total added/deleted, net, churn-over-time (per month)
  - hotspots: top files by (added+deleted), with churn/commit and a churn score
  - attribution: conventional-commit %, type/scope histograms, PR-merge %,
    ticket-ref %, author histogram
  - cadence: commits per active day, mean/median inter-commit gap (days)

Literature: relative churn is the strongest replicated defect/AI-slop signal
(Nagappan & Ball 2005). Absolute churn is a poor predictor — always normalise.

As a module:
    from quality.churn import analyze
    result = analyze("/path/to/repo", top_n=25, no_merges=True)

As a CLI:
    python3 -m quality.churn /path/to/repo [--top 25] [--no-merges]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
from collections import Counter, defaultdict
from datetime import datetime

# Files that are generated / vendored / binary — excluded from churn hotspots
EXCLUDE_RE = re.compile(
    r"(^|/)(node_modules|dist|build|out|\.next|vendor|\.venv|venv|__pycache__|"
    r"coverage|\.git)/|"
    r"(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|go\.sum|poetry\.lock|"
    r"Cargo\.lock|\.min\.(js|css)$)|"
    r"\.(png|jpg|jpeg|gif|svg|ico|pdf|woff2?|ttf|mp4|webm|zip|gz)$",
    re.IGNORECASE,
)
CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|chore|ci|build|arch|harden|revert)"
    r"(\(([^)]+)\))?(!)?:",
    re.IGNORECASE,
)
TICKET_RE = re.compile(r"#\d+")
MERGE_RE = re.compile(r"Merge pull request|\(#\d+\)$")

SENT = "@@@COMMIT@@@"


def _git_log(repo: str, no_merges: bool, since: str | None = None) -> str:
    scope = "--no-merges" if no_merges else "--all"
    cmd = [
        "git", "-C", repo, "log", scope,
        f"--format={SENT}%H\t%an\t%ae\t%ad\t%s",
        "--numstat", "--date=short",
    ]
    if since:
        # Native `git log --since` — reuses the SAME log/parse path with one
        # extra native flag; not a second git-log parser. #187's hotspots.py
        # uses this (via `analyze(..., since=...)`) to derive a per-file
        # recent-activity trend without hand-rolling date-range parsing.
        cmd.insert(4, f"--since={since}")
    # No check=True: a repo with no commits exits 128; we treat that as empty.
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def analyze(repo: str, top_n: int = 25, no_merges: bool = True, since: str | None = None) -> dict:
    """Compute git-history churn metrics for ``repo``. Never raises on empty repos.

    ``since`` (optional, e.g. ``"2026-01-01"`` or ``"90 days ago"``, anything
    ``git log --since`` accepts) restricts analysis to commits after that
    point — the SAME engine, just date-bounded, for callers that need a
    recent-activity slice (e.g. a trend comparison) without re-deriving churn
    from scratch.
    """
    log = _git_log(repo, no_merges, since=since)

    commits: list[dict] = []
    cur: dict | None = None
    file_churn: dict[str, dict] = defaultdict(lambda: {"add": 0, "del": 0, "commits": 0})
    for line in log.splitlines():
        if line.startswith(SENT):
            h, an, ae, ad, s = line[len(SENT):].split("\t", 4)
            cur = {
                "hash": h, "author": an, "email": ae, "date": ad, "subject": s,
                "add": 0, "del": 0, "files": 0,
            }
            commits.append(cur)
        elif line.strip() and cur is not None:
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            a, d, path = parts
            add = 0 if a == "-" else int(a)
            dele = 0 if d == "-" else int(d)
            cur["add"] += add
            cur["del"] += dele
            cur["files"] += 1
            if not EXCLUDE_RE.search(path):
                fc = file_churn[path]
                fc["add"] += add
                fc["del"] += dele
                fc["commits"] += 1

    if not commits:
        return {"repo": repo.rstrip("/").split("/")[-1], "error": "no commits"}

    dates = sorted(c["date"] for c in commits)

    per_month: dict[str, dict] = defaultdict(lambda: {"commits": 0, "add": 0, "del": 0})
    for c in commits:
        m = c["date"][:7]
        per_month[m]["commits"] += 1
        per_month[m]["add"] += c["add"]
        per_month[m]["del"] += c["del"]

    ts: list[datetime] = []
    for c in commits:
        try:
            ts.append(datetime.strptime(c["date"], "%Y-%m-%d"))
        except ValueError:
            pass
    ts.sort()
    gaps_days = [(ts[i] - ts[i - 1]).days for i in range(1, len(ts))]

    conv = sum(1 for c in commits if CONVENTIONAL_RE.match(c["subject"]))
    ticket = sum(1 for c in commits if TICKET_RE.search(c["subject"]))
    merges = sum(1 for c in commits if MERGE_RE.search(c["subject"]))
    type_hist: Counter = Counter()
    scope_hist: Counter = Counter()
    for c in commits:
        m = CONVENTIONAL_RE.match(c["subject"])
        if m:
            type_hist[m.group(1).lower()] += 1
            if m.group(3):
                scope_hist[m.group(3).lower()] += 1
    authors = Counter(c["author"] for c in commits)

    hotspots = sorted(
        (
            {
                "file": f, "churn": v["add"] + v["del"], "add": v["add"],
                "del": v["del"], "commits": v["commits"],
            }
            for f, v in file_churn.items()
        ),
        key=lambda x: x["churn"], reverse=True,
    )[:top_n]

    total_add = sum(c["add"] for c in commits)
    total_del = sum(c["del"] for c in commits)

    return {
        "repo": repo.rstrip("/").split("/")[-1],
        "scale": {
            "commits": len(commits),
            "first": dates[0], "last": dates[-1],
            "active_days": len(set(dates)),
            "span_days": (ts[-1] - ts[0]).days if len(ts) > 1 else 0,
        },
        "churn": {
            "added": total_add, "deleted": total_del,
            "net": total_add - total_del,
            "churn_ratio_del_add": round(total_del / total_add, 3) if total_add else 0,
            "per_month": dict(sorted(per_month.items())),
        },
        "cadence": {
            "commits_per_active_day": round(len(commits) / len(set(dates)), 2),
            "median_gap_days": statistics.median(gaps_days) if gaps_days else 0,
            "mean_gap_days": round(statistics.mean(gaps_days), 2) if gaps_days else 0,
        },
        "attribution": {
            "n": len(commits),
            "conventional_pct": round(100 * conv / len(commits), 1),
            "ticket_ref_pct": round(100 * ticket / len(commits), 1),
            "pr_merge_pct": round(100 * merges / len(commits), 1),
            "type_histogram": dict(type_hist.most_common()),
            "scope_histogram": dict(scope_hist.most_common(20)),
            "author_histogram": dict(authors.most_common()),
        },
        "hotspots": hotspots,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="git-history churn metrics")
    parser.add_argument("repo", help="path to the git repository")
    parser.add_argument("--top", type=int, default=25, help="number of hotspots")
    parser.add_argument(
        "--no-merges", action="store_true",
        help="analyze non-merge commits only (default analyses all)",
    )
    args = parser.parse_args()
    print(json.dumps(analyze(args.repo, top_n=args.top, no_merges=args.no_merges), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
