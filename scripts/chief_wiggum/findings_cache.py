"""Per-file findings cache for the gate scanners (#327), stored as ONE index
per (repo, engine).

``check_traceability.py`` and ``check_single_writer.py`` both split scanning
into per-file **emission** (a pure function of one file's path + content —
``@cw-trace`` annotations, candidate write sites) and **claim** (a join
against the epic's declared IDs / invariants, computed live at report time —
the #160 split). Emission is the expensive half on a large repo, and it is a
pure function of inputs that rarely change between runs. Claim must NEVER be
memoized per file — a stale claim is the exact completeness violation this
repo's ratchet doctrine exists to prevent — so this cache covers emission
only; callers still run the claim join fresh, over every emitted fact
(cached or not), on every invocation.

**Full-coverage semantics, incremental cost — the doctrine this exists to
respect.** A completeness gate never goes faster by narrowing what it looks
at: every file in the manifest is still accounted for on every run. What
changes is whether a file's emission is RECOMPUTED or SERVED from a prior
run — legitimate only when the answer PROVABLY cannot have changed, which
takes both halves of the key:

- ``blob_sha`` — the file's content hash (from
  ``chief_wiggum.manifest.build_manifest``, git-blob-compatible). Content
  identical -> the same bytes reach the parser.
- ``scanner_hash`` — the scanner's own hash-derived version
  (``chief_wiggum.hashing.scanner_version``, already computed by each
  checker's own ``_scanner_version()`` over its source plus every dependency
  that affects emission). Scanner logic identical -> the same bytes still
  parse the same way.

Both are load-bearing. ``blob_sha`` alone is the exact stale-artifact bug
this repo has hit repeatedly; a key that fails to prove freshness — no
manifest entry (non-git ``--source``, a gitignored-but-present file, a
submodule), a missing entry, a corrupt index, or the escape hatch below —
re-scans; it never assumes a hit.

The key also covers ``rel`` (the file's repo-relative path), not just its
content: emission is a function of ``(path, content)``, not content alone —
the same bytes classify differently under a ``_test.go`` path (``is_test``)
than a plain ``.go`` one, and different extensions dispatch to entirely
different emitter modules.

**Layout.** One JSON index per (repo, engine) at
``<root>/<repo_id>/<engine>.json``::

    {"scanner_hash": "<hash>", "entries": {"<rel>": {"blob_sha": "...", "findings": [...]}}}

The first layout kept one file per ``(rel, blob_sha, scanner_hash)`` — a
scan of a 3,000-file repo was 3,000 opens plus 3,000 ``mkdir`` calls before
a single byte of source was read, and every commit added new blobs while
nothing ever removed the old ones (31k files after three benchmark runs).
The index is read once per process and written once at exit; a rel keeps
only its LATEST blob, so the index is bounded by the file count; and a
scanner change drops every entry, so a stale scanner's findings can never
be served and the cache garbage-collects itself. Two processes flushing the
same index merge (the file's entries under ours) and replace atomically —
the loser of a race loses at most a re-scan, never correctness.

Only genuine emission SUCCESSES are cached. A file that could not be read at
all (``read_text_safe`` failure -> ``unscanned``) is never stored — callers
skip this module entirely for such a file, so a crash or unreadable file
always re-attempts on the next run rather than caching an absence (#289: a
broken scanner must re-run and still report ``error``, never a cached
false-clean).

Escape hatch: ``CW_FINDINGS_NO_CACHE=1`` disables both the read and the write
(each checker's own ``--no-cache`` CLI flag sets it for its own process) — the
dual-run (cached vs ``--no-cache``) zero-diff is the validation gate for this
cache, exactly as PR #337's ``quality/cache.py``.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path

NO_CACHE_ENV = "CW_FINDINGS_NO_CACHE"
CACHE_DIR_ENV = "CW_FINDINGS_CACHE_DIR"

# In-process indexes, keyed by (repo_id, engine). Loaded lazily on first
# access, mutated by ``store``, flushed once at exit.
_INDEXES: dict[tuple[str, str], dict] = {}
_FLUSH_REGISTERED = False


def disabled() -> bool:
    """True when the escape hatch is set — any non-empty, non-"0" value."""
    return os.environ.get(NO_CACHE_ENV, "") not in ("", "0")


def _root() -> Path:
    root = Path(
        os.environ.get(CACHE_DIR_ENV)
        or (Path.home() / ".chief-wiggum" / "cache" / "findings")
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _repo_id(repo: str) -> str:
    return hashlib.sha256(os.path.abspath(repo).encode()).hexdigest()[:16]


def _index_path(repo: str, engine: str) -> Path:
    d = _root() / _repo_id(repo)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{engine}.json"


def _entry_path(repo: str, engine: str, rel: str, blob_sha: str, scanner_hash: str) -> Path:
    """The on-disk file that holds this key. Kept for callers/tests that
    corrupt or inspect the store directly; every key of one (repo, engine)
    now lives in the same index file."""
    return _index_path(repo, engine)


def _read_index(path: Path) -> dict | None:
    """The index on disk, or ``None`` when absent/unreadable/malformed — any
    of which is a miss for every key, never a crash."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return None
    if not isinstance(data.get("scanner_hash"), str):
        return None
    return data


def _index(repo: str, engine: str) -> dict:
    """The live in-process index for (repo, engine): ``{"path", "scanner_hash",
    "entries", "dirty"}``. Read from disk on first access; an index the
    current ``CACHE_DIR`` no longer points at (tests redirect it per test) is
    re-read rather than served from a stale process-level copy."""
    key = (_repo_id(repo), engine)
    path = _index_path(repo, engine)
    idx = _INDEXES.get(key)
    if idx is not None and idx["path"] == path:
        return idx
    data = _read_index(path)
    idx = {
        "path": path,
        "scanner_hash": data["scanner_hash"] if data else None,
        "entries": dict(data["entries"]) if data else {},
        "dirty": False,
    }
    _INDEXES[key] = idx
    return idx


def load(repo: str, engine: str, rel: str, blob_sha: str, scanner_hash: str) -> list[dict] | None:
    """Cached findings — a list of plain dicts, one per emitted fact — for
    ``(rel, blob_sha, scanner_hash)``, or ``None`` on a miss: disabled,
    absent, unreadable/corrupt, a different scanner, or a different blob
    (never raises — a broken index degrades to a fresh scan, never a crash)."""
    if disabled():
        return None
    idx = _index(repo, engine)
    if idx["scanner_hash"] != scanner_hash:
        return None
    entry = idx["entries"].get(rel)
    if not isinstance(entry, dict) or entry.get("blob_sha") != blob_sha:
        return None
    findings = entry.get("findings")
    return findings if isinstance(findings, list) else None


def store(
    repo: str, engine: str, rel: str, blob_sha: str, scanner_hash: str, findings: list[dict]
) -> None:
    """Best-effort record of a GENUINE emission success. Callers must never
    call this for a file that could not be read, or whose emission raised —
    only for output the scanner actually produced. A scanner change empties
    the index first (nothing from the old scanner may survive beside new
    entries); the write itself happens once, at exit."""
    global _FLUSH_REGISTERED
    if disabled():
        return
    idx = _index(repo, engine)
    if idx["scanner_hash"] != scanner_hash:
        idx["entries"] = {}
        idx["scanner_hash"] = scanner_hash
    idx["entries"][rel] = {"blob_sha": blob_sha, "findings": findings}
    idx["dirty"] = True
    if not _FLUSH_REGISTERED:
        atexit.register(flush)
        _FLUSH_REGISTERED = True


def flush() -> None:
    """Write every dirty index once, merging with whatever another process
    wrote to the same file meanwhile (theirs under ours, same scanner only)
    and replacing atomically. A cache that can't be written (read-only FS,
    disk full) must never fail the scan it memoizes."""
    for idx in list(_INDEXES.values()):
        if not idx["dirty"] or idx["scanner_hash"] is None:
            continue
        entries = dict(idx["entries"])
        on_disk = _read_index(idx["path"])
        if on_disk is not None and on_disk["scanner_hash"] == idx["scanner_hash"]:
            entries = {**on_disk["entries"], **entries}
        tmp = idx["path"].with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps({"scanner_hash": idx["scanner_hash"], "entries": entries}))
            os.replace(tmp, idx["path"])
            idx["entries"] = entries
            idx["dirty"] = False
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
