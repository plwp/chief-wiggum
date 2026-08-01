#!/usr/bin/env python3
"""hotspots.py — Tornhill-style hotspot discovery: churn x complexity + coupling (#187).

Composes (never re-derives) the three engines already living in
``scripts/quality/`` — this module adds no new git-history parser and no new
change-coupling definition (INV-fh-001, CTR-fh-030):

  - ``churn.analyze``            -> per-file churn (added+deleted) and commit
                                     counts, plus the analyzed window
                                     (``scale.span_days`` — churn.analyze has
                                     no window parameter of its own, so THIS
                                     module derives+records ``window_days``
                                     from that field; never ``datetime.now()``).
  - ``complexity.lizard_ccn``    -> per-function cyclomatic complexity (CCN),
                                     grouped here into a per-file complexity
                                     score (sum of function CCN in the file).
  - ``process.compute_coupling`` -> change-coupling pairs (support + confidence
                                     thresholds), the ONE coupling engine.

``hotspot_score(file) = norm_churn(file) * norm_complexity(file)``, each
normalized 0..1 by the max across analyzed files (``"normalization": "max"``).
Deciles are assigned over the FULL ranked population before any output
truncation (``--top``), so "top decile" means top 10% of every file this run
could score — not just the emitted slice. Ranking and decile assignment happen
on RAW scores; rounding is a serialization step afterwards, so a rounding-
induced tie can never let filename order shift a decile. Ties on the raw score
break (score desc, file asc) for determinism (CTR-fh-032).

The record also captures the full input/tool state ``--check`` needs to
detect staleness beyond ``git_sha`` + ``window_days`` (PR #194 review):
``dirty`` (was the worktree clean at generation? — complexity reads working-
tree file contents, so a dirty tree makes the record unreproducible) and
``complexity_source`` (``lizard-<version>`` or ``absent`` — a record built
without lizard is NOT the record the same SHA would produce with it).
History walks are always HEAD-based (``git log`` from HEAD, never ``--all``),
so unrelated local refs cannot change the output for a given ``git_sha``.

Authority boundary (never a gate — printed verbatim into every generated
record and by the CLI): a hotspot rank is a RISK PRIOR from git history at one
SHA, not a defect finding, and the ABSENCE of a rank is not evidence of health
(a young file simply has no history yet).

As a module:
    from quality.hotspots import discover
    result = discover("/path/to/repo")

As a CLI, see ``scripts/hotspot_discovery.py``.
"""

from __future__ import annotations

import os
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import churn as churn_mod
from . import complexity as complexity_mod
from . import process as process_mod

SCHEMA = "hotspots/1"

# Where the CLI conventionally writes the record inside a target repo. The
# artifact itself is excluded from the dirty-worktree computation: it is never
# an input to any engine (complexity's EXCLUDE_RE skips docs/; churn/coupling
# read committed history only), and counting it would make --check fail the
# moment generate writes it — a permanently-noisy check nobody trusts.
DEFAULT_ARTIFACT_REL = "docs/quality/hotspots.json"

AUTHORITY = (
    "ranks files by historical change-frequency x current complexity from git "
    "history at {sha}; high rank is a risk prior, not a defect finding; absence "
    "of rank is not evidence of health (young files have no history)."
)

# Reuse process.py's own coupling-support default so the two consumers of
# change coupling (its own report and this composer) never silently diverge
# on what "coupled" means.
DEFAULT_MIN_CO = process_mod.DEFAULT_MIN_CO
DEFAULT_TOP_N = 200
DEFAULT_COUPLED_TOP_N = 5
# A recent-vs-expected churn-share gap beyond this magnitude is reported as a
# directional trend; below it, "stable" — a deliberately coarse signal, not
# over-claiming precision a two-point split doesn't have.
TREND_THRESHOLD = 0.15
# Effectively "no cap" for churn.analyze's own top_n truncation — hotspots
# needs every file's churn, not a top-N slice of it.
_ALL_FILES = 10**9


def head_sha(repo: str) -> str | None:
    r = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True,
    )
    sha = r.stdout.strip()
    return sha or None


def worktree_dirty(repo: str, ignore: tuple[str, ...] = (DEFAULT_ARTIFACT_REL,)) -> bool:
    """True when ``git status --porcelain`` reports anything other than paths
    in ``ignore`` (by default only the hotspots artifact itself — see
    ``DEFAULT_ARTIFACT_REL``). Complexity reads working-tree file CONTENTS, so
    any other modification/deletion/untracked file makes the record
    unreproducible at its recorded git_sha — recorded at generate time and
    re-checked by ``--check`` (fail-closed: a git failure reads as dirty)."""
    # -uall lists untracked FILES individually (default collapses an untracked
    # directory to "?? dir/", which would defeat the exact-path ignore below).
    r = subprocess.run(
        ["git", "-C", repo, "status", "--porcelain", "-uall"], capture_output=True, text=True,
    )
    if r.returncode != 0:
        return True  # can't prove clean -> treat as dirty (fail closed)
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        # porcelain line: "XY path" (or "XY old -> new" for renames)
        path_part = line[3:].split(" -> ")[-1].strip().strip('"')
        if any(path_part == ig for ig in ignore):
            continue
        return True
    return False


def complexity_source(venv: str | None = None, gobin: str | None = None) -> str:
    """The complexity tool-state fingerprint recorded in the artifact and
    compared by ``--check``: ``lizard-<version>`` when lizard resolves,
    ``absent`` otherwise. A record generated without lizard (empty ranking)
    must not --check clean once lizard appears — the same SHA would now
    produce a different record."""
    return _complexity_source_from_bin(complexity_mod._tool("lizard", venv, gobin))


def _complexity_source_from_bin(lizard_bin: str | None) -> str:
    if not lizard_bin:
        return "absent"
    r = subprocess.run([lizard_bin, "--version"], capture_output=True, text=True)
    out = (r.stdout or "").strip() or (r.stderr or "").strip()
    ver = out.splitlines()[0].strip() if out else "unknown"
    return f"lizard-{ver}"


def window_days_at(repo: str) -> int | None:
    """Cheap window-days re-derivation (no lizard/complexity work) — used by
    ``--check`` to verify staleness without a full regenerate. Always the
    HEAD-based no-merges walk (the only walk ``discover`` uses)."""
    r = churn_mod.analyze(repo, top_n=1, no_merges=True)
    if r.get("error"):
        return None
    return r["scale"]["span_days"]


def _normalize(values: dict[str, float]) -> dict[str, float]:
    m = max(values.values(), default=0)
    if m <= 0:
        return dict.fromkeys(values, 0.0)
    return {k: v / m for k, v in values.items()}


def _file_complexity(repo: str, lizard_bin: str | None) -> tuple[dict[str, int], bool]:
    """Per-file complexity score (sum of per-function CCN in that file), via
    ``complexity.lizard_ccn`` — reused as-is (CTR-fh-030's named reuse
    target), just grouped by file instead of aggregated repo-wide. Returns
    ``(scores, lizard_available)``; an absent lizard degrades to ``({}, False)``
    rather than raising — the composer still emits a record, just with an
    honest note (issue #187: "graceful skip note when absent").

    Keys are git-style repo-relative paths (forward-slash, via
    ``Path.as_posix()``) — lizard reports OS-native paths, but churn's keys
    come from ``git log`` and code_query's exact-membership check compares
    against git-style paths verbatim, so normalization here is what keeps the
    intersection (and the emitted ``file`` fields) correct on every platform
    (PR #194 review)."""
    if not lizard_bin:
        return {}, False
    bucketed = complexity_mod.bucket(repo)
    all_src: list[str] = []
    for _lang, sets in bucketed.items():
        all_src += sets["src"]
    rows = complexity_mod.lizard_ccn(all_src, lizard_bin)
    totals: dict[str, int] = defaultdict(int)
    for row in rows:
        f = row.get("file")
        if not f:
            continue
        rel = Path(os.path.relpath(f, repo)).as_posix()
        totals[rel] += row["ccn"]
    return dict(totals), True


def _compute_trend(repo: str, churn_result: dict, window_days: int) -> dict[str, str]:
    """Directional trend: recent-half churn share vs. the share you'd expect
    if churn were spread evenly across the window. Reuses ``churn.analyze``
    a second time with ``since`` bounding it to the recent half — the SAME
    engine, date-bounded, not a new history parser."""
    try:
        first = datetime.strptime(churn_result["scale"]["first"], "%Y-%m-%d")
        last = datetime.strptime(churn_result["scale"]["last"], "%Y-%m-%d")
    except (KeyError, ValueError):
        return {}
    midpoint = first + (last - first) / 2
    recent_days = (last - midpoint).days
    if recent_days <= 0 or window_days <= 0:
        return {}
    recent = churn_mod.analyze(
        repo, top_n=_ALL_FILES, no_merges=True,
        since=midpoint.strftime("%Y-%m-%d"),
    )
    if recent.get("error"):
        return {}
    recent_by_file = {h["file"]: h["churn"] for h in recent["hotspots"]}
    expected_share = recent_days / window_days
    total_by_file = {h["file"]: h["churn"] for h in churn_result["hotspots"]}
    out: dict[str, str] = {}
    for f, total in total_by_file.items():
        if total <= 0:
            continue
        actual_share = recent_by_file.get(f, 0) / total
        diff = actual_share - expected_share
        if diff > TREND_THRESHOLD:
            out[f] = "rising"
        elif diff < -TREND_THRESHOLD:
            out[f] = "cooling"
        else:
            out[f] = "stable"
    return out


def _rank_decile_round(scored: list[dict]) -> list[dict]:
    """Sort on RAW scores (score desc, file asc), assign deciles over the raw
    ranking, THEN round for serialization. Rounding before ranking could
    create artificial ties between distinct raw scores, letting filename
    order shift which side of a decile boundary a file lands on (PR #194
    review). Rounding is a pure display step, so the emitted record is still
    byte-identical across runs at the same inputs (CTR-fh-032)."""
    scored.sort(key=lambda h: (-h["score"], h["file"]))
    n = len(scored)
    for i, h in enumerate(scored):
        pct_from_top = (i / n) if n else 0.0
        h["decile"] = max(1, min(10, 10 - int(pct_from_top * 10)))
    for h in scored:
        h["score"] = round(h["score"], 6)
        h["norm_churn"] = round(h["norm_churn"], 6)
        h["norm_complexity"] = round(h["norm_complexity"], 6)
    return scored


def discover(
    repo: str,
    *,
    min_co: int = DEFAULT_MIN_CO,
    top_n: int = DEFAULT_TOP_N,
    coupled_top_n: int = DEFAULT_COUPLED_TOP_N,
    venv: str | None = None,
    gobin: str | None = None,
    trend: bool = True,
    dirty_ignore: tuple[str, ...] = (DEFAULT_ARTIFACT_REL,),
) -> dict:
    """Compose churn x complexity x coupling into a ``hotspots/1`` record.
    Deterministic for a fixed ``(git_sha, window_days, normalization,
    complexity_source)`` on a clean worktree (CTR-fh-032); never raises — an
    empty/no-history repo degrades to an empty ``hotspots`` list with a note,
    exactly like the reused engines do. All git walks are HEAD-based (never
    ``--all``), so unrelated local refs cannot change the output.

    @cw-trace guards CTR-fh-030 CTR-fh-031 CTR-fh-032 CTR-fh-033 INV-fh-001 INV-fh-007
    """
    sha = head_sha(repo)
    generated_at = datetime.now(timezone.utc).isoformat()
    lizard_bin = complexity_mod._tool("lizard", venv, gobin)
    churn_result = churn_mod.analyze(repo, top_n=_ALL_FILES, no_merges=True)

    base = {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "git_sha": sha,
        "dirty": worktree_dirty(repo, ignore=dirty_ignore),
        "no_merges": True,
        "normalization": "max",
        "complexity_source": _complexity_source_from_bin(lizard_bin),
        "authority": AUTHORITY.format(sha=sha or "HEAD"),
        "inputs": {
            "churn": "scripts/quality/churn.py",
            "complexity": "scripts/quality/complexity.py",
            "coupling": "scripts/quality/process.py",
        },
    }

    if churn_result.get("error") or not sha:
        return {
            **base,
            "window_days": 0,
            "method": "no commit history to analyze",
            "params": {"coupling_min_co": min_co, "top_n": top_n, "decile_count": 10},
            "note": "no commits / not a repo — nothing to rank",
            "hotspots": [],
        }

    window_days = churn_result["scale"]["span_days"]
    churn_by_file = {h["file"]: h["churn"] for h in churn_result["hotspots"]}
    commits_by_file = {h["file"]: h["commits"] for h in churn_result["hotspots"]}

    complexity_by_file, lizard_ok = _file_complexity(repo, lizard_bin)

    # Only files with BOTH churn history and complexity data are rankable —
    # a file lizard can't parse (or excluded from complexity's product-code
    # bucket) simply has no complexity term to multiply.
    candidate_files = sorted(set(churn_by_file) & set(complexity_by_file))
    norm_churn = _normalize({f: churn_by_file[f] for f in candidate_files})
    norm_complexity = _normalize({f: complexity_by_file[f] for f in candidate_files})

    # coupling.confidence is single-write-path (INV-fh-001): process.py both
    # computes it AND shapes the per-file partner dicts (`partners_by_file`) —
    # this module only relays what it's handed, never re-declares the field.
    coupling_pairs = process_mod.compute_coupling(repo, min_co=min_co)
    coupled_with = process_mod.partners_by_file(coupling_pairs)

    trend_by_file: dict[str, str] = {}
    if trend and window_days >= 2:
        trend_by_file = _compute_trend(repo, churn_result, window_days)

    scored: list[dict] = []
    for f in candidate_files:
        scored.append({
            "file": f,
            "score": norm_churn[f] * norm_complexity[f],  # RAW — rounded after ranking
            "norm_churn": norm_churn[f],
            "norm_complexity": norm_complexity[f],
            "churn": churn_by_file[f],
            "commits": commits_by_file[f],
            "complexity": complexity_by_file[f],
            "coupled_with": coupled_with.get(f, [])[:coupled_top_n],
            "trend": trend_by_file.get(f),
        })
    # Rank + decile on raw scores, then round for serialization
    # (CTR-fh-032 / #187 IT-fh-08 / PR #194 review).
    scored = _rank_decile_round(scored)

    note = None
    if not lizard_ok:
        note = (
            "lizard not found — complexity scores unavailable; hotspot ranking "
            "requires both churn and complexity (pip install lizard)"
        )

    result = {
        **base,
        "window_days": window_days,
        "method": (
            f"score = norm_churn x norm_complexity per file; churn = added+deleted "
            f"lines over the window (scripts/quality/churn.py, no_merges=True); "
            f"complexity = sum of per-function cyclomatic complexity via lizard "
            f"(scripts/quality/complexity.py); each normalized 0..1 by the max "
            f"across analyzed files; change-coupling from scripts/quality/process.py "
            f"(co_changes>=min_co, confidence=co_changes/min(commits_a,commits_b))."
        ),
        "params": {
            "coupling_min_co": min_co,
            "coupled_with_top_n": coupled_top_n,
            "top_n": top_n,
            "decile_count": 10,
            "trend_threshold": TREND_THRESHOLD if trend else None,
        },
        "hotspots": scored[:top_n],
    }
    if note:
        result["note"] = note
    if len(scored) > top_n:
        result["truncated_from"] = len(scored)
    return result
