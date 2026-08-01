#!/usr/bin/env python3
"""artifacts.py — per-target CW meta-location resolver (chief-wiggum#213).

One shared answer to "where does this target's chief-wiggum meta live, and
which paths are in play" for every skill, gate, and query surface. Two
footprint modes, elected once per target and recorded OUTSIDE the target
(under ``~/.chief-wiggum/meta/<owner>/<repo>/``, never in the target repo):

- ``embedded`` (the default — no election file means embedded, so every
  existing repo keeps working unchanged): meta lives in ``<target>/docs/``
  (epics/, quality/, patterns/, design/).
- ``sidecar``: meta lives in ``~/.chief-wiggum/meta/<owner>/<repo>/docs/``
  with the IDENTICAL layout beneath. Code (scaffolds, CI config, tests) stays
  in the target regardless of mode; knowledge (contracts, invariants, ratchet
  journal/high-water/scorecard, trace links, gate-validation records) moves to
  the sidecar.

Threat-model note (docs/ratchet.md): in sidecar mode, workers operating in
the target worktree PHYSICALLY CANNOT write the goalposts — the contracts,
specs, and ratchet state simply are not in the tree they can touch. The
ratchet fails closed by construction, not by diff inspection.

Version binding: in sidecar mode every artifact carries the target HEAD sha it
was computed against — ``Resolver.stamp`` / ``Resolver.check_stale``
generalize the ``git_sha``/``--check`` staleness pattern in
``scripts/hotspot_discovery.py``.

Domain scope: ``scope.json`` at the meta root (a sibling of ``quality/``),
``{"include": [globs], "exclude": [globs]}`` with fnmatch semantics. A missing
file means whole-repo scope; an empty include list means everything; exclude
wins over include.

The ``~/.chief-wiggum`` location is overridable via the
``CHIEF_WIGGUM_USER_DIR`` env var or the ``cw_home`` parameter (tests must
never touch the real home dir), consistent with how ``scripts/env.py`` roots
its user-space paths at ``~/.chief-wiggum``.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MODES = ("embedded", "sidecar")
BACKINGS = ("local", "git")
ELECTION_NAME = "election.json"
SCOPE_NAME = "scope.json"

# The subdirectories every meta root carries — identical beneath
# <target>/docs (embedded) and ~/.chief-wiggum/meta/<owner>/<repo>/docs
# (sidecar).
META_SUBDIRS = ("epics", "quality", "patterns", "design")


def load_scope_file(path: Path | str) -> dict | None:
    """Read a ``scope.json`` document. Missing or unreadable ⇒ ``None``
    (= whole-repo scope) — the same degradation ``Resolver._load_scope``
    always had, exposed module-level so gates can consume an EXPLICIT
    ``--scope <path>`` with identical semantics."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def path_in_scope(scope: dict | None, relpath: str | Path) -> bool:
    """fnmatch a repo-relative path against a scope document. ``None`` scope =
    everything in scope; empty include = everything; exclude wins. The single
    implementation behind ``Resolver.in_scope`` AND every gate's ``--scope``
    classification (chief-wiggum#213 Phase D) — one matching rule, not a copy
    per gate."""
    if scope is None:
        return True
    rel = PurePosixPath(Path(relpath)).as_posix()
    exclude = scope.get("exclude") or []
    if any(fnmatch.fnmatch(rel, g) for g in exclude):
        return False
    include = scope.get("include") or []
    if not include:
        return True
    return any(fnmatch.fnmatch(rel, g) for g in include)


def describe_scope(scope: dict | None, missing_note: str = "whole repo") -> str:
    """One-line human summary of a scope document (shared by
    ``Resolver.scope_summary`` and /status)."""
    if scope is None:
        return missing_note
    include = scope.get("include") or []
    exclude = scope.get("exclude") or []
    parts = ["include: " + (", ".join(include) if include else "(everything)")]
    if exclude:
        parts.append("exclude: " + ", ".join(exclude))
    return "; ".join(parts)


def user_dir(cw_home: Path | str | None = None) -> Path:
    """The chief-wiggum user-space dir (default ``~/.chief-wiggum``).

    Precedence: explicit ``cw_home`` param > ``CHIEF_WIGGUM_USER_DIR`` env var
    > ``~/.chief-wiggum`` — so tests can isolate themselves without ever
    touching the real home dir.
    """
    if cw_home is not None:
        return Path(cw_home).expanduser()
    env = os.environ.get("CHIEF_WIGGUM_USER_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".chief-wiggum"


def _git(target: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(target), *args],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def head_sha(target: Path | str) -> str | None:
    """Current HEAD of the target repo, or None outside a git repo."""
    return _git(Path(target), "rev-parse", "HEAD")


def _parse_remote(url: str) -> str | None:
    """``<owner>/<repo>`` from an https/ssh/scp-form remote URL, else None."""
    u = re.sub(r"\.git/?$", "", url.strip().rstrip("/"))
    m = re.search(r"[:/]([^/:]+)/([^/:]+)$", u)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if not owner or not repo or "@" in repo:
        return None
    return f"{owner}/{repo}"


def derive_target_id(target: Path | str) -> str:
    """Target identity: ``<owner>/<repo>`` from the origin remote; fallback
    ``local/<sha1-of-abspath-first-12>`` when the target has no remote (or is
    not a git repo at all)."""
    url = _git(Path(target), "remote", "get-url", "origin")
    if url:
        parsed = _parse_remote(url)
        if parsed:
            return parsed
    # realpath, not abspath: path variants through symlinks (macOS /tmp vs
    # /private/tmp, a worktree reached via a link) must yield the SAME id, or
    # an election recorded via one spelling is invisible via the other.
    digest = hashlib.sha1(os.path.realpath(str(target)).encode()).hexdigest()[:12]
    return f"local/{digest}"


def meta_dir(target_id: str, cw_home: Path | str | None = None) -> Path:
    """``~/.chief-wiggum/meta/<owner>/<repo>`` for a target id."""
    return user_dir(cw_home) / "meta" / Path(target_id)


def election_path(target_id: str, cw_home: Path | str | None = None) -> Path:
    return meta_dir(target_id, cw_home) / ELECTION_NAME


def load_election(target_id: str, cw_home: Path | str | None = None) -> dict | None:
    """The recorded election, or None (= embedded, the status quo)."""
    path = election_path(target_id, cw_home)
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed election file at {path}: {exc}") from exc
    if doc.get("mode") not in MODES:
        raise ValueError(
            f"election file at {path} has unknown mode {doc.get('mode')!r} "
            f"(expected one of {MODES})"
        )
    return doc


def elect(
    target: Path | str,
    mode: str,
    backing: str = "git",
    cw_home: Path | str | None = None,
) -> dict:
    """Record a footprint-mode election for a target (creates dirs).

    The election lives in the user dir, never in the target — an embedded->
    sidecar (or back) decision must not itself be a write into the tree whose
    footprint is being decided.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r} (expected one of {MODES})")
    if backing not in BACKINGS:
        raise ValueError(f"unknown backing {backing!r} (expected one of {BACKINGS})")
    target_id = derive_target_id(target)
    record = {
        "mode": mode,
        "backing": backing,
        "elected_at": datetime.now(timezone.utc).isoformat(),
        "target_id": target_id,
    }
    root = meta_dir(target_id, cw_home)
    root.mkdir(parents=True, exist_ok=True)
    if mode == "sidecar":
        (root / "docs").mkdir(exist_ok=True)
    (root / ELECTION_NAME).write_text(json.dumps(record, indent=2) + "\n")
    return record


@dataclass(frozen=True)
class Resolver:
    """Resolved meta locations for one target repo. Construct via
    ``Resolver.resolve(target_repo)``."""

    target: Path
    target_id: str
    mode: str
    backing: str
    meta_root: Path

    @classmethod
    def resolve(cls, target_repo: Path | str, cw_home: Path | str | None = None) -> Resolver:
        target = Path(target_repo)
        target_id = derive_target_id(target)
        election = load_election(target_id, cw_home)
        if election is None:
            mode, backing = "embedded", "local"
        else:
            mode = election["mode"]
            backing = election.get("backing", "local")
        if mode == "sidecar":
            meta_root = meta_dir(target_id, cw_home) / "docs"
        else:
            meta_root = target / "docs"
        return cls(target=target, target_id=target_id, mode=mode,
                   backing=backing, meta_root=meta_root)

    # -- path helpers (identical layout beneath meta_root in both modes) -------

    def epics_dir(self) -> Path:
        return self.meta_root / "epics"

    def epic_dir(self, slug: str) -> Path:
        return self.epics_dir() / slug

    def quality_dir(self) -> Path:
        return self.meta_root / "quality"

    def patterns_dir(self) -> Path:
        return self.meta_root / "patterns"

    def design_dir(self) -> Path:
        return self.meta_root / "design"

    # -- domain scope ----------------------------------------------------------

    def scope_path(self) -> Path:
        """Where this target's scope.json lives (may not exist)."""
        return self.meta_root / SCOPE_NAME

    def _load_scope(self) -> dict | None:
        """Read scope.json fresh each call (live-scan discipline — never
        memoized). Missing or unreadable ⇒ whole-repo scope."""
        return load_scope_file(self.scope_path())

    def in_scope(self, relpath: str | Path) -> bool:
        """fnmatch the repo-relative path against scope.json. Missing file =
        everything in scope; empty include = everything; exclude wins."""
        return path_in_scope(self._load_scope(), relpath)

    def scope_summary(self) -> str:
        return describe_scope(
            self._load_scope(),
            missing_note=f"whole repo (no {SCOPE_NAME} at {self.meta_root})",
        )

    # -- version binding -------------------------------------------------------

    def stamp(self, payload: dict) -> dict:
        """Copy of ``payload`` with ``target_sha`` = the target's current HEAD
        — mandatory on sidecar artifacts so staleness is detectable."""
        out = dict(payload)
        out["target_sha"] = head_sha(self.target)
        return out

    def check_stale(self, payload: dict) -> str | None:
        """None when the payload's target_sha matches the target's current
        HEAD; a human-readable warning string otherwise (missing sha counts as
        unverifiable, i.e. a warning — never a silent pass)."""
        recorded = payload.get("target_sha")
        if not recorded:
            return (
                "payload carries no target_sha — cannot verify it against the "
                "target's HEAD; regenerate it via stamp()"
            )
        current = head_sha(self.target)
        if current is None:
            return (
                f"target {self.target} has no HEAD (not a git repo?) — cannot "
                f"verify recorded target_sha {recorded}"
            )
        if recorded != current:
            return (
                f"stale: recorded target_sha={recorded} != current HEAD={current} "
                "— the artifact was computed against an older target; regenerate"
            )
        return None

    def to_dict(self) -> dict:
        return {
            "target": str(self.target),
            "target_id": self.target_id,
            "mode": self.mode,
            "backing": self.backing,
            "meta_root": str(self.meta_root),
            "scope": self.scope_summary(),
        }


# ---- CLI ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Per-target CW meta-location resolver (chief-wiggum#213)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("elect", help="record a footprint-mode election for a target")
    p.add_argument("target", help="local path to the target repo")
    p.add_argument("--mode", required=True, choices=MODES)
    p.add_argument("--backing", default="git", choices=BACKINGS,
                   help="sidecar backing store (default: git meta-repo)")

    p = sub.add_parser("show", help="print the resolved meta locations for a target")
    p.add_argument("target", help="local path to the target repo")
    p.add_argument("--format", choices=["text", "json"], default="text")

    args = parser.parse_args(argv)

    if args.cmd == "elect":
        try:
            record = elect(args.target, args.mode, backing=args.backing)
        except ValueError as exc:
            print(f"artifacts: {exc}", file=sys.stderr)
            return 2
        resolver = Resolver.resolve(args.target)
        print(f"artifacts: elected mode={record['mode']} backing={record['backing']} "
              f"for {record['target_id']} — meta root: {resolver.meta_root}")
        return 0

    if args.cmd == "show":
        try:
            resolver = Resolver.resolve(args.target)
        except ValueError as exc:
            print(f"artifacts: {exc}", file=sys.stderr)
            return 2
        if args.format == "json":
            print(json.dumps(resolver.to_dict(), indent=2))
        else:
            d = resolver.to_dict()
            for k in ("target_id", "mode", "backing", "meta_root", "scope"):
                print(f"{k}: {d[k]}")
        return 0

    return 2  # pragma: no cover — argparse enforces the subcommand


if __name__ == "__main__":
    sys.exit(main())
