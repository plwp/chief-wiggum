"""Per-file findings cache for the gate scanners (#327).

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
this repo has hit repeatedly (a ratchet fabricating a pass count from an old
junit report; a crashed rerun replaying the previous run's numbers instead of
its own): editing ``write_emission.py``'s regex family, or
``trace_emission.py``'s annotation grammar, would otherwise silently keep
serving findings computed by the PREVIOUS scanner version for every file in
the repo whose CONTENT never changed. A key that fails to prove freshness —
no manifest entry (non-git ``--source``, or a path the manifest legitimately
excludes — a gitignored-but-present file, a submodule), a missing cache
entry, a corrupt one, or the escape hatch below — re-scans; it never assumes
a hit.

The key also covers ``rel`` (the file's repo-relative path), not just its
content: emission is a function of ``(path, content)``, not content alone —
the same bytes classify differently under a ``_test.go`` path (``is_test``)
than a plain ``.go`` one, and different extensions dispatch to entirely
different emitter modules. A content-only key would let two byte-identical
files at different paths collide and serve each other's findings.

Only genuine emission SUCCESSES are cached. A file that could not be read at
all (``read_text_safe`` failure -> ``unscanned``) is never stored — callers
skip this module entirely for such a file, so a crash or unreadable file
always re-attempts on the next run rather than caching an absence (#289: a
broken scanner must re-run and still report ``error``, never a cached
false-clean).

Escape hatch: ``CW_FINDINGS_NO_CACHE=1`` disables both the read and the write
(each checker's own ``--no-cache`` CLI flag sets it for its own process) — the
dual-run (cached vs ``--no-cache``) zero-diff is the validation gate for this
cache, exactly as PR #337's ``quality/cache.py`` (same discipline, this
module's coarser-grained sibling — per-repo/per-corpus there, because those
engines shell out to one external tool per call; per-FILE here, because a
gate scanner's cost is dominated by per-file regex/parse work across
thousands of files, not one external invocation)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

NO_CACHE_ENV = "CW_FINDINGS_NO_CACHE"
CACHE_DIR_ENV = "CW_FINDINGS_CACHE_DIR"


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


def _entry_path(repo: str, engine: str, rel: str, blob_sha: str, scanner_hash: str) -> Path:
    repo_id = hashlib.sha256(os.path.abspath(repo).encode()).hexdigest()[:16]
    digest = hashlib.sha256(f"{rel}\x00{blob_sha}\x00{scanner_hash}".encode()).hexdigest()[:24]
    d = _root() / repo_id / engine
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{digest}.json"


def load(repo: str, engine: str, rel: str, blob_sha: str, scanner_hash: str) -> list[dict] | None:
    """Cached findings — a list of plain dicts, one per emitted fact — for
    ``(rel, blob_sha, scanner_hash)``, or ``None`` on a miss: disabled,
    absent, unreadable/corrupt, or a defensive key mismatch (never raises —
    a broken cache entry degrades to a fresh scan, never a crash)."""
    if disabled():
        return None
    path = _entry_path(repo, engine, rel, blob_sha, scanner_hash)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # The filename digest already encodes all three fields; this re-check
    # guards a truncated-hash near-collision or a hand-edited file — treat
    # either as a miss rather than risk serving a finding for the wrong key.
    if (
        data.get("rel") != rel
        or data.get("blob_sha") != blob_sha
        or data.get("scanner_hash") != scanner_hash
    ):
        return None
    findings = data.get("findings")
    return findings if isinstance(findings, list) else None


def store(
    repo: str, engine: str, rel: str, blob_sha: str, scanner_hash: str, findings: list[dict]
) -> None:
    """Best-effort write of a GENUINE emission success. Callers must never
    call this for a file that could not be read, or whose emission raised —
    only for output the scanner actually produced. A cache that can't be
    written (read-only FS, disk full) must never fail the scan it memoizes."""
    if disabled():
        return
    path = _entry_path(repo, engine, rel, blob_sha, scanner_hash)
    try:
        path.write_text(json.dumps({
            "rel": rel, "blob_sha": blob_sha, "scanner_hash": scanner_hash,
            "findings": findings,
        }))
    except OSError:
        pass
