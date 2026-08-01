#!/usr/bin/env python3
"""markers.py — source-level TODO/FIXME/HACK/XXX inventory (#214).

``check_unresolved.py`` polices TBD/UNRESOLVED/PLACEHOLDER markers in epic
artifacts (docs/models only); SOURCE code has never been scanned. This engine
inventories the classic deferred-work markers in product source: uppercase,
whole-word ``TODO`` / ``FIXME`` / ``HACK`` / ``XXX``.

Population: the shared #214 scan population (``population.tracked_source`` —
tracked, non-generated, non-vendored, inside the #213 scope filter). Vendored
and generated dirs (node_modules, vendor, dist, ``*_pb2.py``, …) are excluded
by that shared population, reusing the existing quality-engine exclusion
conventions rather than a private copy.

Per finding: ``file``, ``line``, marker ``kind``, and the trailing text after
the marker (first 80 chars) — enough for a human or an agent to triage without
opening the file.

Pure Python + git; nothing to skip. Never raises on unreadable files (they are
counted in ``unreadable``, never silently dropped).

As a module:
    from quality.markers import analyze
    result = analyze("/path/to/repo")

As a CLI:
    python3 -m quality.markers <repo>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import population

# `(?<!context\.)`: Go's stdlib `context.TODO()` is an API call, not a
# deferred-work marker — caught live on a real Go validation corpus (2 of 7
# "TODO"s were context.TODO() call sites). Only the qualified form is
# excluded: bare `TODO(author):` attribution markers still count.
MARKER_RE = re.compile(r"\b(?<!context\.)(TODO|FIXME|HACK|XXX)\b")
TRAILING_CHARS = 80


def _trailing_text(line: str, match: re.Match) -> str:
    """The human text after the marker token: strip a leading separator
    (``:``, ``-``, ``(author)``) and cap at TRAILING_CHARS."""
    rest = line[match.end():]
    rest = re.sub(r"^\s*(\([^)]*\))?\s*[:\-–]?\s*", "", rest).rstrip()
    return rest[:TRAILING_CHARS]


def scan_file(rel: str, text: str) -> list[dict]:
    findings: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = MARKER_RE.search(line)
        if m:
            findings.append({
                "file": rel,
                "line": lineno,
                "kind": m.group(1),
                "text": _trailing_text(line, m),
            })
    return findings


def analyze(repo: str, path_filter=None) -> dict:
    """Inventory TODO/FIXME/HACK/XXX markers in the repo's scan population."""
    files = population.tracked_source(repo, path_filter=path_filter)
    findings: list[dict] = []
    unreadable: list[str] = []
    for rel in files:
        try:
            text = (Path(repo) / rel).read_text(errors="replace")
        except OSError:
            unreadable.append(rel)
            continue
        findings.extend(scan_file(rel, text))
    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    return {
        "engine": "markers",
        "files_scanned": len(files) - len(unreadable),
        "unreadable": unreadable,
        "counts_by_kind": by_kind,
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="source-level TODO/FIXME/HACK/XXX inventory")
    parser.add_argument("repo", help="path to the git repository")
    args = parser.parse_args()
    print(json.dumps(analyze(args.repo), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
