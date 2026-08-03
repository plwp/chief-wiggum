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

Threat-model note (docs/sidecar.md "Trust boundary"): in sidecar mode a
goalpost edit cannot ride in the worker's REVIEWED DIFF — the contracts,
specs, and ratchet state have no path inside the target tree, so the
C2-style channel (a goalpost move hidden inside a reviewed code change) is
removed. That is the whole claim. Workers are NOT filesystem-sandboxed: a
process running as the same user can still write the sidecar directly, and
the ``CHIEF_WIGGUM_USER_DIR`` env var re-roots resolution entirely. The
boundary is the diff, not the disk.

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


SCOPE_ALLOWED_KEYS = ("include", "exclude", "$comment")


def load_scope_file(path: Path | str) -> dict | None:
    """Read a ``scope.json`` document. A MISSING file ⇒ ``None`` (= whole-repo
    scope, the documented default). A file that exists but is unparsable, not
    an object, or carries unknown keys raises ``ValueError`` naming the
    problem: a typo'd key (``"includes"``) must never silently mean whole-repo
    scope — that would be a silent authority widening. Exposed module-level so
    gates consuming an EXPLICIT ``--scope <path>`` share identical semantics."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot parse scope file {p}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(
            f"scope file {p} must be a JSON object, got {type(doc).__name__}"
        )
    unknown = sorted(k for k in doc if k not in SCOPE_ALLOWED_KEYS)
    if unknown:
        raise ValueError(
            f"scope file {p} has unknown key(s): {', '.join(unknown)} "
            f"(allowed: {', '.join(SCOPE_ALLOWED_KEYS)}) — a typo'd key must not "
            "silently mean whole-repo scope"
        )
    return doc


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


def _is_ancestor(target: Path, ancestor: str, descendant: str) -> bool | None:
    """True/False when git can determine ancestry; None when it CANNOT (a
    missing object — e.g. a shallow clone that never fetched ``ancestor`` — or
    any other git failure). ``git merge-base --is-ancestor`` exits 0 (true),
    1 (false), or >1 (error); only >1 is coerced to None (chief-wiggum#287) —
    an indeterminate comparison must warn, never silently pass OR silently
    fail closed as a false positive."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(target), "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def _changed_paths_between(target: Path, base: str, head: str) -> list[str] | None:
    """Repo-relative paths changed between ``base`` and ``head`` (exclusive..
    inclusive), or None if git could not compute the diff at all."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(target), "diff", "--name-only", f"{base}..{head}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.splitlines() if line]


def _safe_id_part(part: str) -> bool:
    """A remote-derived id segment that is safe to use as a path component
    under the meta root. Rejects anything that could traverse or escape:
    empty, ``.``/``..``, path separators, backslashes, NUL, absolute forms.
    The target id becomes ``meta/<id>`` on disk, so a hostile remote URL
    (``https://x.com/../..``) must NEVER survive into it — it falls back to
    the ``local/<hash>`` identity instead."""
    if not part or part in (".", ".."):
        return False
    if any(c in part for c in ("/", "\\", "\x00")):
        return False
    return True


def _parse_remote(url: str) -> str | None:
    """Target id from an https/ssh/scp-form remote URL, else None.

    - host ``github.com`` (the overwhelmingly common case): ``<owner>/<repo>``
      — unchanged, so every existing meta dir keeps resolving.
    - any other host: ``<host>/<owner>/<repo>`` — host-blind identity would
      collide ``gitlab.acme.com/team/app`` with ``github.com/team/app`` onto
      one meta dir (and one election).

    Every emitted segment is validated via ``_safe_id_part`` (dots in a host
    are fine; ``..`` is not); any violation returns None so the caller falls
    back to the safe, deterministic ``local/<hash>`` identity."""
    u = re.sub(r"\.git/?$", "", url.strip().rstrip("/"))
    m = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^/@]*@)?([^/:]+)(?::\d+)?/(.+)$", u, re.IGNORECASE)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        # scp form: [user@]host:owner/repo
        m = re.match(r"^(?:[^@/:]+@)?([^/:@]+):(.+)$", u)
        if not m:
            return None
        host, path = m.group(1), m.group(2)
    host = host.lower()
    parts = path.split("/")
    if len(parts) < 2:
        return None
    owner, repo = parts[-2], parts[-1]
    if not _safe_id_part(host) or host.startswith("."):
        return None
    if not all(_safe_id_part(p) for p in parts):
        return None
    if "@" in repo:
        return None
    if host == "github.com":
        return f"{owner}/{repo}"
    return f"{host}/{owner}/{repo}"


def derive_target_id(target: Path | str) -> str:
    """Target identity: ``<owner>/<repo>`` from the origin remote (prefixed
    with the host for non-github remotes); fallback
    ``local/<sha1-of-abspath-first-12>`` when the target has no remote, is not
    a git repo at all, or its remote does not parse into SAFE path segments
    (see ``_safe_id_part`` — a hostile remote must not traverse the meta
    root)."""
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
    if not isinstance(doc, dict):
        # [] / null / 12 / "sidecar" are all malformed, same failure path as
        # unparsable JSON — never an AttributeError leaking out of .get().
        raise ValueError(
            f"malformed election file at {path}: expected a JSON object, "
            f"got {type(doc).__name__}"
        )
    if doc.get("mode") not in MODES:
        raise ValueError(
            f"election file at {path} has unknown mode {doc.get('mode')!r} "
            f"(expected one of {MODES})"
        )
    if doc.get("backing", "local") not in BACKINGS:
        raise ValueError(
            f"election file at {path} has unknown backing {doc.get('backing')!r} "
            f"(expected one of {BACKINGS})"
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
        memoized). Missing ⇒ whole-repo scope; a malformed/unknown-key file
        raises ValueError (see ``load_scope_file``) so a typo never silently
        widens authority to the whole repo."""
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

    def _default_exclude_prefixes(self) -> list[str]:
        """Repo-relative path prefix(es) that changing alone does NOT make an
        artifact stale — this resolver's own state dir (``quality/``: ratchet's
        config/journal/highwater/scorecard, debt.json, ...). Committing that
        state (the normal `/implement` Step 13 flow) moves HEAD; that alone
        must not make the artifact it just recorded read as stale
        (chief-wiggum#287).

        Sidecar mode: the meta root has no path inside the target at all, so
        a target-tree diff can never contain it — nothing to exclude, and
        nothing needed."""
        if self.mode == "sidecar":
            return []
        try:
            rel = self.quality_dir().resolve().relative_to(self.target.resolve())
        except ValueError:
            return []
        return [rel.as_posix()]

    def check_stale(self, payload: dict, exclude_paths: list[str] | None = None) -> str | None:
        """None when the payload's ``target_sha`` is fresh w.r.t. the target's
        current HEAD; a human-readable warning string otherwise.

        "Fresh" is NOT exact-HEAD equality (chief-wiggum#287): the artifact's
        OWN state gets committed after it is produced (e.g. `ratchet score`
        stamps target_sha=HEAD, then `/implement` Step 13 commits the
        scorecard it just wrote — moving HEAD), which made every
        correctly-produced artifact read as stale the instant it landed.
        Instead: recorded is fresh when it is an ANCESTOR of current HEAD
        *and* every file that changed between them falls under
        ``exclude_paths`` (default: this resolver's own state dir) — i.e. only
        the artifact's own committed state moved, not the population it
        scanned. A missing sha, an unresolvable ancestry check (shallow
        clone/missing object), or an unreadable diff all count as
        UNVERIFIABLE — a warning, never a silent pass.
        """
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
        if recorded == current:
            return None
        is_ancestor = _is_ancestor(self.target, recorded, current)
        if is_ancestor is None:
            return (
                f"indeterminate: cannot determine whether recorded target_sha="
                f"{recorded} is an ancestor of current HEAD={current} (shallow "
                "clone or missing object?) — treating as UNVERIFIABLE, not "
                "fresh; regenerate to be safe"
            )
        if not is_ancestor:
            return (
                f"stale: recorded target_sha={recorded} is not an ancestor of "
                f"current HEAD={current} — the artifact was computed against a "
                "divergent target; regenerate"
            )
        changed = _changed_paths_between(self.target, recorded, current)
        if changed is None:
            return (
                f"indeterminate: cannot compute the diff between recorded "
                f"target_sha={recorded} and current HEAD={current} — treating "
                "as UNVERIFIABLE, not fresh; regenerate to be safe"
            )
        prefixes = self._default_exclude_prefixes() if exclude_paths is None else exclude_paths
        outside = [
            f for f in changed
            if not any(f == p or f.startswith(p.rstrip("/") + "/") for p in prefixes)
        ]
        if outside:
            shown = ", ".join(outside[:5])
            more = f" … and {len(outside) - 5} more" if len(outside) > 5 else ""
            return (
                f"stale: recorded target_sha={recorded} != current HEAD={current} "
                f"and {len(outside)} file(s) outside this artifact's own state "
                f"changed since it was recorded ({shown}{more}) — regenerate"
            )
        return None

    def to_dict(self) -> dict:
        return {
            "target": str(self.target),
            "target_id": self.target_id,
            "mode": self.mode,
            "backing": self.backing,
            "meta_root": str(self.meta_root),
            # Absolute, mode-resolved paths (#213 F8) so skills can consume
            # `show --format json | jq -r .quality_dir` instead of assuming
            # the embedded docs/ layout.
            "epics_dir": str(self.epics_dir()),
            "quality_dir": str(self.quality_dir()),
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
            resolver = Resolver.resolve(args.target)
        except ValueError as exc:
            print(f"artifacts: {exc}", file=sys.stderr)
            return 2
        print(f"artifacts: elected mode={record['mode']} backing={record['backing']} "
              f"for {record['target_id']} — meta root: {resolver.meta_root}")
        return 0

    if args.cmd == "show":
        # to_dict() reads scope.json (scope_summary) — a malformed scope file
        # must exit 2 with the ValueError's message, never silently degrade to
        # whole-repo scope (or traceback).
        try:
            resolver = Resolver.resolve(args.target)
            d = resolver.to_dict()
        except ValueError as exc:
            print(f"artifacts: {exc}", file=sys.stderr)
            return 2
        if args.format == "json":
            print(json.dumps(d, indent=2))
        else:
            for k in ("target_id", "mode", "backing", "meta_root",
                      "epics_dir", "quality_dir", "scope"):
                print(f"{k}: {d[k]}")
        return 0

    return 2  # pragma: no cover — argparse enforces the subcommand


if __name__ == "__main__":
    sys.exit(main())
