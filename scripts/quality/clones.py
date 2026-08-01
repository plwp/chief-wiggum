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
foot outside the domain is not this team's debt item).

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

from . import duplication

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


def cluster(repo: str, duplicates: list[dict], path_filter=None) -> list[dict]:
    """Cluster jscpd duplicate PAIRS into clone classes keyed by normalized
    span content hash. Deterministic: classes sorted (size desc, hash asc);
    members sorted (file, start_line)."""
    classes: dict[str, dict] = {}
    for dup in duplicates:
        # jscpd may emit fragment as an EMPTY string (observed on real repos)
        # — treat empty/missing alike and re-read the span from the file.
        fragment = dup.get("fragment") or None
        members = []
        for side in ("firstFile", "secondFile"):
            f = dup.get(side) or {}
            rel = _rel(repo, str(f.get("name", "")))
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
        })
        for rel, start, end in members:
            if path_filter is not None and not path_filter(rel):
                continue
            cls["members"].add((rel, start, end))

    out = []
    for cls in classes.values():
        members = sorted(cls["members"])
        if len(members) < 2:
            continue  # a class needs >= 2 in-scope spans to be a clone at all
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
    return out


def analyze(repo: str, workdir: str, path_filter=None, name: str | None = None) -> dict:
    """Clone classes for ``repo`` via the shared jscpd runner."""
    name = name or repo.rstrip("/").split("/")[-1]
    data, skip = duplication.run_jscpd(repo, workdir)
    if skip is not None:
        return {"repo": name, "engine": "clones", **skip}
    duplicates = data.get("duplicates") or []
    classes = cluster(repo, duplicates, path_filter=path_filter)
    return {
        "repo": name,
        "engine": "clones",
        "clone_pairs_reported": len(duplicates),
        "clone_classes": classes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="clone classes from jscpd locations")
    parser.add_argument("repo", help="path to the git repository")
    parser.add_argument("--workdir", required=True, help="scratch dir for jscpd output")
    args = parser.parse_args()
    print(json.dumps(analyze(args.repo, args.workdir), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
