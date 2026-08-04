#!/usr/bin/env python3
"""cache.py — SHA-keyed on-disk result cache for immutable quality-engine
inputs (#328).

Several engines in the code-metrics battery recompute a PURE function of an
immutable input on every run:

  - ``trend.measure_at`` — a historical commit's metrics can never change.
  - ``duplication.run_jscpd`` — the SAME corpus (repo-relative file set) at
    the same content hashes produces the same clone report, whether the two
    callers (``duplication.analyze``'s aggregate percentage and
    ``clones.py``'s clone-class clustering) run in the same process or two
    separate ``/close-epic`` CLI invocations.
  - ``survival.analyze`` — git-of-theseus walks committed history only (it
    never reads the working tree), so its output is a pure function of HEAD.

This module is the ONE cache both same-process and cross-process callers
share: a small JSON file per (repo, engine, key) under
``~/.chief-wiggum/cache/quality/`` (overridable via ``CW_QUALITY_CACHE_DIR``
for test isolation — see ``tests/conftest.py``'s ``isolate_quality_cache``).

**Not a completeness workaround** (#327's doctrine, restated in #328): the
cache key for a working-tree-dependent engine (jscpd) is built by
``manifest_key``, which content-hashes EVERY file in the corpus via
``chief_wiggum.manifest.build_manifest`` — the same batched git primitive
#325 uses for provenance. An uncommitted edit changes that file's hash and
busts the cache; the key is never derived by skipping or sampling files, only
by hashing all of them more cheaply than re-running the tool. For a
history-only engine (git-of-theseus), ``head_sha`` is sufficient because the
tool provably never reads the working tree.

Escape hatch: ``CW_QUALITY_NO_CACHE=1`` (or any of ``trend.py`` /
``duplication.py`` / ``clones.py`` / ``survival.py``'s ``--no-cache`` CLI
flags, which set it for their own process) disables both the read and the
write, for the dual-run parity check #328's acceptance criteria calls for.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

NO_CACHE_ENV = "CW_QUALITY_NO_CACHE"
CACHE_DIR_ENV = "CW_QUALITY_CACHE_DIR"


def disabled() -> bool:
    """True when the escape hatch is set — any non-empty, non-"0" value."""
    return os.environ.get(NO_CACHE_ENV, "") not in ("", "0")


def _root() -> Path:
    root = Path(
        os.environ.get(CACHE_DIR_ENV)
        or (Path.home() / ".chief-wiggum" / "cache" / "quality")
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _entry_path(repo: str, engine: str, key: str) -> Path:
    repo_id = hashlib.sha256(os.path.abspath(repo).encode()).hexdigest()[:16]
    d = _root() / repo_id / engine
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def load(repo: str, engine: str, key: str) -> dict | None:
    """Cached result for ``(repo, engine, key)``, or ``None`` on a miss —
    disabled, absent, or unreadable/corrupt (never raises: a broken cache
    entry degrades to a fresh measurement, never a crash)."""
    if disabled() or key is None:
        return None
    path = _entry_path(repo, engine, key)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def store(repo: str, engine: str, key: str, value: dict) -> None:
    """Best-effort write. A cache that can't be written (read-only FS, disk
    full) must never fail the measurement it is trying to memoize."""
    if disabled() or key is None:
        return
    path = _entry_path(repo, engine, key)
    try:
        path.write_text(json.dumps(value))
    except OSError:
        pass


def manifest_key(repo: str, files: list[str] | None) -> str | None:
    """Content-hash cache key for a jscpd-style corpus.

    ``files`` is the explicit repo-relative corpus (``clones.py``'s
    scope-narrowed list); ``None`` is the whole-repo default both
    ``duplication.run_jscpd`` callers use when no #213 scope applies — the
    common case where BOTH consumers hash the identical corpus and therefore
    share one cache entry.

    Returns ``None`` (uncacheable) if the manifest can't be built — not a git
    repo, or git itself is absent — so the caller falls back to always
    running the tool rather than crashing.
    """
    try:
        from chief_wiggum.manifest import ManifestError, build_manifest  # noqa: PLC0415
    except ImportError:
        return None
    predicate = (lambda p, _s=set(files): p in _s) if files is not None else None
    try:
        manifest = build_manifest(repo, predicate)
    except ManifestError:
        return None
    blob = "\n".join(f"{p}:{h}" for p, h in sorted(manifest.items()))
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def head_sha(repo: str) -> str | None:
    """The repo's current HEAD commit, or ``None`` outside a git repo / with
    an unborn HEAD. Only valid as a cache key for an engine that is provably a
    pure function of COMMITTED history alone (git-of-theseus survival, which
    never reads the working tree) — a working-tree-dependent engine (jscpd)
    must use ``manifest_key`` instead, or a dirty edit would be invisible to
    the cache."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
    except OSError:
        return None
    if out.returncode != 0:
        return None
    sha = out.stdout.strip()
    return sha or None
