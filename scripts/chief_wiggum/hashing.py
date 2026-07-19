"""Shared stable-hash helpers (#160).

``stable_hash`` originated in ``ratchet.py`` (contract-definition hashing) and is
now the single home for it, imported back into ``ratchet.py`` so there is one
implementation, not a copy. ``scanner_version`` builds on it for a
hash-derived scanner version: the version of a gate script IS the hash of its
own source plus its ``chief_wiggum`` dependencies, so a forgotten manual bump
(the previous failure mode — a hand-edited constant nobody remembers to touch)
is structurally impossible. Any edit to the scanner or a dependency changes the
version automatically.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def stable_hash(*parts: str) -> str:
    """Deterministic hash of one or more string parts (order-sensitive, NUL-joined)."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def scanner_version(*module_paths: str | Path) -> str:
    """Hash-derived version for a scanner: ``stable_hash`` over the raw source text
    of ``module_paths`` (the scanner module itself plus its ``chief_wiggum`` deps),
    in the order given. Order matters — callers pass a fixed, deterministic list
    (their own file first, then each imported dependency module) so the same
    inputs always produce the same version, and any source change (in the
    scanner OR a dependency) changes it."""
    texts = [Path(p).read_text() for p in module_paths]
    return stable_hash(*texts)
