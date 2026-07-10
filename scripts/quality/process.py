#!/usr/bin/env python3
"""process.py — process/history metrics (the literature's strongest signals).

Rahman & Devanbu (2013): process metrics outperform static product metrics for
defect prediction, and are more stable across releases. Computed from ``git log``:
  - Change (temporal) coupling  — Tornhill: files that change together.
  - Change entropy (HCM)        — Hassan 2009: Shannon entropy of change spread.
  - Ownership / bus-factor      — Bird et al. 2011: top-owner share, minor authors.
  - Commit-size distribution    — large commits = risk / weaker review.
  - Fix ratio + fix-hotspots    — SZZ-lite defect proxy.

Caveat for agentic repos: bus-factor / ownership metrics assume human authorship;
when one operator drives an AI pipeline the "author" collapses to one identity, so
read bus-factor as a signal about the ATTRIBUTION model, not team resilience.

As a module:
    from quality.process import analyze
    result = analyze("/path/to/repo")

As a CLI:
    python3 -m quality.process <repo>
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from collections import Counter, defaultdict
from itertools import combinations

CODE = (".py", ".go", ".ts", ".tsx", ".js", ".jsx")
EXCLUDE = re.compile(
    r"(^|/)(node_modules|dist|build|out|\.next|vendor|\.venv|"
    r"venv|__pycache__|coverage|docs|\.git)/|"
    r"(package-lock\.json|go\.sum|yarn\.lock)$",
    re.I,
)
FIX = re.compile(r"^(fix|bugfix|hotfix)(\(|:|\b)", re.I)

SENT = "@@@"


def is_code(p: str) -> bool:
    return p.endswith(CODE) and not EXCLUDE.search(p)


def analyze(repo: str) -> dict:
    """Compute process/history metrics for ``repo``. Never raises on empty repos."""
    log = subprocess.run(
        ["git", "-C", repo, "log", "--no-merges", f"--format={SENT}%H\t%an\t%s", "--numstat"],
        capture_output=True, text=True,
    ).stdout

    commits: list[dict] = []
    cur: dict | None = None
    for line in log.splitlines():
        if line.startswith(SENT):
            h, an, s = line[len(SENT):].split("\t", 2)
            cur = {"author": an, "subject": s, "files": []}
            commits.append(cur)
        elif line.strip() and cur is not None:
            parts = line.split("\t")
            if len(parts) == 3 and is_code(parts[2]):
                add = 0 if parts[0] == "-" else int(parts[0])
                dele = 0 if parts[1] == "-" else int(parts[1])
                cur["files"].append((parts[2], add + dele))

    if not commits:
        return {"repo": repo.rstrip("/").split("/")[-1], "commits_analyzed": 0}

    # ---- change coupling ----
    pair_co: Counter = Counter()
    file_commits: Counter = Counter()
    for c in commits:
        fs = [f for f, _ in c["files"]]
        for f in set(fs):
            file_commits[f] += 1
        for a, b in combinations(sorted(set(fs)), 2):
            pair_co[(a, b)] += 1
    coupling: list[dict] = []
    for (a, b), co in pair_co.items():
        if co < 4:
            continue
        conf = co / min(file_commits[a], file_commits[b])
        cross_dir = a.rsplit("/", 1)[0] != b.rsplit("/", 1)[0]
        coupling.append({
            "a": a, "b": b, "co_changes": co,
            "confidence": round(conf, 2), "cross_dir": cross_dir,
        })
    coupling.sort(key=lambda x: (x["confidence"], x["co_changes"]), reverse=True)

    # ---- change entropy (Hassan HCM), normalized 0..1 ----
    total = sum(file_commits.values())
    n = len(file_commits)
    if total and n > 1:
        H = -sum((v / total) * math.log2(v / total) for v in file_commits.values())
        entropy = round(H / math.log2(n), 3)
    else:
        entropy = 0.0

    # ---- ownership / bus-factor (Bird et al.) ----
    author_lines: Counter = Counter()
    file_authors: dict = defaultdict(Counter)
    for c in commits:
        for f, ch in c["files"]:
            author_lines[c["author"]] += ch
            file_authors[f][c["author"]] += 1
    tot_lines = sum(author_lines.values()) or 1
    ranked = author_lines.most_common()
    acc, bus = 0, 0
    for _, v in ranked:
        acc += v
        bus += 1
        if acc >= 0.5 * tot_lines:
            break
    top_owner_shares: list[float] = []
    minor_heavy = 0  # files with >1 author AND >=1 minor contributor (<5% of commits)
    for _f, ac in file_authors.items():
        tc = sum(ac.values())
        top = ac.most_common(1)[0][1]
        top_owner_shares.append(top / tc)
        if len(ac) > 1 and any(v / tc < 0.05 for v in ac.values()):
            minor_heavy += 1
    avg_top_owner = round(sum(top_owner_shares) / len(top_owner_shares), 2) if top_owner_shares else 1.0

    # ---- commit size ----
    sizes = sorted(sum(ch for _, ch in c["files"]) for c in commits if c["files"])
    fcounts = sorted(len(c["files"]) for c in commits if c["files"])

    def pct(a: list[int], p: float) -> int:
        return a[min(len(a) - 1, int(p * len(a)))] if a else 0

    big = sum(1 for s in sizes if s > 400)

    # ---- fix ratio + fix hotspots ----
    fix_commits = [c for c in commits if FIX.match(c["subject"])]
    fix_touch: Counter = Counter()
    for c in fix_commits:
        for f, _ in c["files"]:
            fix_touch[f] += 1

    return {
        "repo": repo.rstrip("/").split("/")[-1],
        "commits_analyzed": len(commits),
        "change_entropy_normalized": entropy,
        "ownership": {
            "distinct_authors": len(author_lines),
            "bus_factor_50pct": bus,
            "top_author_share": round(ranked[0][1] / tot_lines, 2) if ranked else 0,
            "avg_file_top_owner_share": avg_top_owner,
            "files_with_minor_contributors": minor_heavy,
        },
        "commit_size": {
            "median_churn": pct(sizes, 0.5), "p90_churn": pct(sizes, 0.9),
            "median_files": pct(fcounts, 0.5), "p90_files": pct(fcounts, 0.9),
            "pct_large_commits_gt400": round(100 * big / len(sizes), 1) if sizes else 0,
        },
        "defect_proxy": {
            "fix_commit_pct": round(100 * len(fix_commits) / len(commits), 1) if commits else 0,
            "top_fix_hotspots": [
                {"file": f, "fix_touches": v} for f, v in fix_touch.most_common(5)
            ],
        },
        "change_coupling_top": coupling[:8],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="process/history metrics")
    parser.add_argument("repo", help="path to the git repository")
    args = parser.parse_args()
    print(json.dumps(analyze(args.repo), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
