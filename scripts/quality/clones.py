#!/usr/bin/env python3
"""clones.py — per-clone locations clustered into clone CLASSES (#214).

``duplication.py`` reports the aggregate copy/paste percentage; this engine
surfaces what that number is made of. jscpd already emits per-clone locations
(``duplicates`` in its JSON report) — we reuse the exact same runner
(``duplication.run_jscpd``, one invocation semantics for both consumers) and
cluster the duplicated spans into **clone classes**: "these N spans are the
same", keyed by a content hash of the normalized duplicated span (whitespace
stripped per line, blank lines dropped) so the same text in 2 places and in 5
places is one class with size 2 vs 5, not an undifferentiated pair soup.

The #213 scope filter applies to class MEMBERS: an out-of-scope span leaves
the class, and a class below 2 in-scope members disappears (a clone with one
foot outside the domain is not this team's debt item). Classes dropped THAT
WAY — >= 2 total spans but < 2 in-scope — are not silently discarded: they are
returned as ``boundary_classes`` (full member list, pre-filter), so the debt
inventory can record them in its ``boundary`` section for owning-team
referrals (chief-wiggum#216 C2) instead of losing them at the engine.

Skipped (``{"skipped": ...}``) when jscpd/node is absent — same graceful
degrade as ``duplication.py``.

As a module:
    from quality.clones import analyze
    result = analyze("/path/to/repo", workdir="/tmp/clones")

As a CLI:
    python3 -m quality.clones <repo> --workdir <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from . import duplication, population

CONTENT_HASH_LEN = 16


def _normalize_span(fragment: str) -> str:
    """Whitespace-insensitive span identity: strip each line, drop blanks."""
    lines = [ln.strip() for ln in fragment.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _span_fragment(repo: str, rel: str, start: int, end: int) -> str | None:
    """Read a 1-indexed inclusive line span from the working tree — the
    fallback when a jscpd report carries no ``fragment`` text."""
    try:
        lines = (Path(repo) / rel).read_text(errors="replace").splitlines()
    except OSError:
        return None
    if start < 1 or start > len(lines):
        return None
    return "\n".join(lines[start - 1:min(end, len(lines))])


def _rel(repo: str, name: str) -> str:
    """jscpd file names, normalized to git-style repo-relative posix paths."""
    p = Path(name)
    if p.is_absolute():
        try:
            return Path(os.path.relpath(name, repo)).as_posix()
        except ValueError:
            return p.as_posix()
    return p.as_posix()


def cluster(repo: str, duplicates: list[dict], path_filter=None,
            boundary_out: list[dict] | None = None, corpus_root: str | None = None) -> list[dict]:
    """Cluster jscpd duplicate PAIRS into clone classes keyed by normalized
    span content hash. Deterministic: classes sorted (size desc, hash asc);
    members sorted (file, start_line).

    When ``path_filter`` drops a class below 2 in-scope members despite >= 2
    TOTAL spans, the class (full pre-filter member list) is appended to
    ``boundary_out`` when provided — the caller's boundary-referral evidence
    (chief-wiggum#216 C2), never an in-scope finding.

    ``corpus_root`` is the root jscpd's reported paths are actually relative
    to/absolute under — ``repo`` for an ordinary scan, or the #279 scratch
    corpus tree when the argv-over-budget path built one. Member file paths
    are ALWAYS remapped to repo-relative (never left pointing at a scratch
    tmp directory); source is still re-read from ``repo`` when a fragment is
    missing, since the scratch tree may not exist by the time this runs."""
    root = corpus_root if corpus_root is not None else repo
    classes: dict[str, dict] = {}
    for dup in duplicates:
        # jscpd may emit fragment as an EMPTY string (observed on real repos)
        # — treat empty/missing alike and re-read the span from the file.
        fragment = dup.get("fragment") or None
        members = []
        for side in ("firstFile", "secondFile"):
            f = dup.get(side) or {}
            rel = _rel(root, str(f.get("name", "")))
            start = int(f.get("start", 0) or 0)
            end = int(f.get("end", 0) or 0)
            members.append((rel, start, end))
        if fragment is None:
            rel, start, end = members[0]
            fragment = _span_fragment(repo, rel, start, end)
        if fragment is None:
            continue
        normalized = _normalize_span(str(fragment))
        if not normalized:
            continue
        key = hashlib.sha256(normalized.encode()).hexdigest()[:CONTENT_HASH_LEN]
        cls = classes.setdefault(key, {
            "content_hash": key,
            "lines": int(dup.get("lines", 0) or 0),
            "tokens": int(dup.get("tokens", 0) or 0),
            "members": set(),
            "all_members": set(),
        })
        for rel, start, end in members:
            cls["all_members"].add((rel, start, end))
            if path_filter is not None and not path_filter(rel):
                continue
            cls["members"].add((rel, start, end))

    out = []
    for cls in classes.values():
        members = sorted(cls["members"])
        if len(members) < 2:
            # A class needs >= 2 in-scope spans to be a clone HERE; with >= 2
            # total spans it is still a real clone — someone else's (boundary).
            all_members = sorted(cls["all_members"])
            if boundary_out is not None and len(all_members) >= 2:
                boundary_out.append({
                    "content_hash": cls["content_hash"],
                    "size": len(all_members),
                    "lines": cls["lines"],
                    "tokens": cls["tokens"],
                    "members": [
                        {"file": f, "start_line": s, "end_line": e}
                        for f, s, e in all_members
                    ],
                })
            continue
        out.append({
            "content_hash": cls["content_hash"],
            "size": len(members),
            "lines": cls["lines"],
            "tokens": cls["tokens"],
            "members": [
                {"file": f, "start_line": s, "end_line": e} for f, s, e in members
            ],
        })
    out.sort(key=lambda c: (-c["size"], c["content_hash"]))
    if boundary_out is not None:
        boundary_out.sort(key=lambda c: (-c["size"], c["content_hash"]))
    return out


def corpus(repo: str, path_filter=None) -> list[str]:
    """The files jscpd is handed: the #213 scope-narrowed PRODUCTION population.

    Narrowed at the source (#265), matching ``markers``/``dead_code``/
    ``test_health``, so a narrow ``scope.json`` genuinely reduces the work.
    Previously jscpd walked the repo root and scope was applied only afterwards
    in :func:`cluster`, so a 61-file scope still handed the tool the entire
    tracked corpus — and exhausted a 4 GB V8 heap.

    Test files are dropped here because ``duplication.IGNORE`` dropped them when
    jscpd did its own walking; an explicit file list must preserve that
    production-only contract rather than silently widen the corpus."""
    return [f for f in population.tracked_source(repo, path_filter=path_filter)
            if not population.is_test_file(f)]


BOUNDARY_UNOBSERVABLE = (
    "unobservable — the clone corpus is scope-narrowed (#265), so clone "
    "partners outside the scope are never scanned; an empty boundary list is "
    "NOT evidence the out-of-scope code is clone-free"
)


def analyze(repo: str, workdir: str, path_filter=None, name: str | None = None) -> dict:
    """Clone classes for ``repo`` via the shared jscpd runner."""
    name = name or repo.rstrip("/").split("/")[-1]
    base: dict = {"repo": name, "engine": "clones"}

    if path_filter is None:
        # Nothing to narrow, so keep the historical whole-repo walk. Building an
        # explicit list here would buy no reduction (it IS the whole population)
        # while a big unscoped repo would cross the argv budget and warn about
        # scope-narrowing the operator never asked for.
        files = None
    else:
        files = corpus(repo, path_filter=path_filter)
        base["files_in_corpus"] = len(files)
        # Narrowing means an out-of-scope clone partner is never scanned, so
        # #216's boundary referrals cannot be observed. Say so — the other three
        # engines are narrowed at the source too, and the envelope's boundary
        # note already warns that absence there is not evidence of cleanliness.
        base["boundary_detection"] = BOUNDARY_UNOBSERVABLE
        if not files:
            # A scope selecting nothing was MEASURED and found nothing. Claiming
            # a skip (or a crash) is the same over-claim #259 warns about.
            return {**base, "status": "measured", "clone_pairs_reported": 0,
                    "clone_classes": [], "boundary_classes": []}

    data, problem = duplication.run_jscpd(repo, workdir, files=files)
    if problem is not None:
        return {**base, **{k: v for k, v in problem.items() if v is not None}}

    duplicates = data.get("duplicates") or []
    boundary: list[dict] = []
    classes = cluster(repo, duplicates, path_filter=path_filter,
                      boundary_out=boundary, corpus_root=data.get("_corpus_root"))
    out = {
        **base,
        "status": "measured",
        "clone_pairs_reported": len(duplicates),
        "clone_classes": classes,
        # Classes with >= 2 total spans that fell below 2 in-scope members —
        # boundary evidence for the owning team, never in-scope findings.
        "boundary_classes": boundary,
    }
    if data.get("_corpus_fallback"):
        out["corpus_fallback"] = data["_corpus_fallback"]
        # The scan was repo-wide, so it CAN see out-of-scope partners again...
        out.pop("boundary_detection", None)
        # ...and the selected count is NOT what jscpd scanned. Reporting it as
        # `files_in_corpus` would let an AC1-style assertion read a scoped
        # number off a run that was never scoped.
        out.pop("files_in_corpus", None)
        out["scope_candidate_files"] = len(files or [])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="clone classes from jscpd locations")
    parser.add_argument("repo", help="path to the git repository")
    parser.add_argument("--workdir", required=True, help="scratch dir for jscpd output")
    args = parser.parse_args()
    print(json.dumps(analyze(args.repo, args.workdir), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
