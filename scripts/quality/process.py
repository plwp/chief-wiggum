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


# Minimum co-changes for a pair to count as "coupled" at all — below this,
# two files sharing a handful of commits is noise, not a Tornhill temporal
# coupling signal. Documented (not a magic number): #187 (hotspots.py) reuses
# this exact threshold via `compute_coupling`'s ``min_co`` default so the two
# consumers of change coupling (this module's own report and the hotspot
# composer) never silently diverge on what "coupled" means.
DEFAULT_MIN_CO = 4


def _parse_commits(repo: str) -> list[dict]:
    """Parse ``git log --numstat`` into ``[{author, subject, files:[(path, churn)]}]``,
    restricted to code files (``is_code``). The ONE git-log parse this module
    does — ``analyze()`` and ``compute_coupling()`` both build on this instead
    of each re-invoking/re-parsing ``git log`` (INV-fh-001: a second parser of
    the same history is how a second coupling definition would sneak in)."""
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
    return commits


def _coupling_from_commits(commits: list[dict], min_co: int = DEFAULT_MIN_CO) -> list[dict]:
    """Change-coupling pairs (Tornhill co-change) from already-parsed ``commits``.
    Full pair list, sorted (confidence, co_changes) desc — NOT truncated. This is
    the single computation both ``analyze()`` (which keeps its own top-8 report
    slice) and ``compute_coupling()`` (the full-set entry point #187's
    ``hotspots.py`` calls) share, so there is exactly one co-change definition
    (INV-fh-001)."""
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
        if co < min_co:
            continue
        conf = co / min(file_commits[a], file_commits[b])
        cross_dir = a.rsplit("/", 1)[0] != b.rsplit("/", 1)[0]
        coupling.append({
            "a": a, "b": b, "co_changes": co,
            "confidence": round(conf, 2), "cross_dir": cross_dir,
        })
    coupling.sort(key=lambda x: (x["confidence"], x["co_changes"]), reverse=True)
    return coupling


def compute_coupling(repo: str, min_co: int = DEFAULT_MIN_CO) -> list[dict]:
    """
    Public, standalone change-coupling entry point: the FULL pair set (no
    top-8 truncation), for consumers that need every file's coupled partners
    rather than just the repo-wide top few — #187's ``hotspots.py`` composes
    this into ``coupled_with`` for each hotspot file. Same computation
    ``analyze()`` uses internally (via ``_coupling_from_commits``); this is
    the ONE change-coupling engine in ``scripts/quality/`` (INV-fh-001) —
    callers reuse it rather than re-deriving co-change from git history.

    @cw-trace guards CTR-fh-030 INV-fh-001
    """
    return _coupling_from_commits(_parse_commits(repo), min_co=min_co)


def partners_by_file(pairs: list[dict]) -> dict[str, list[dict]]:
    """Bidirectional index over ``compute_coupling``'s pair list: for each
    file, its coupled partners shaped ``{file, confidence, co_changes}``,
    sorted (confidence desc, co_changes desc, file asc) for determinism.

    ``coupling.confidence`` is single-write-path (INV-fh-001,
    ``sanctioned_writers: scripts/quality/process.py``) — this is where that
    field is authored into a per-file partner SHAPE, so downstream composers
    (#187's ``hotspots.py``) relay these dicts rather than re-declaring the
    field themselves.

    @cw-trace guards INV-fh-001
    """
    out: dict[str, list[dict]] = defaultdict(list)
    for p in pairs:
        out[p["a"]].append({"file": p["b"], "confidence": p["confidence"], "co_changes": p["co_changes"]})
        out[p["b"]].append({"file": p["a"], "confidence": p["confidence"], "co_changes": p["co_changes"]})
    for f in out:
        out[f].sort(key=lambda c: (-c["confidence"], -c["co_changes"], c["file"]))
    return dict(out)


def analyze(repo: str) -> dict:
    """Compute process/history metrics for ``repo``. Never raises on empty repos."""
    commits = _parse_commits(repo)

    if not commits:
        return {"repo": repo.rstrip("/").split("/")[-1], "commits_analyzed": 0}

    # ---- change coupling ----
    file_commits: Counter = Counter()
    for c in commits:
        for f in {f for f, _ in c["files"]}:
            file_commits[f] += 1
    coupling = _coupling_from_commits(commits, min_co=DEFAULT_MIN_CO)

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
