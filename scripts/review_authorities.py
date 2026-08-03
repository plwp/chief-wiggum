#!/usr/bin/env python3
"""review_authorities.py — an adopted repo's OWN conventions, per phase (#264).

`/adopt` measures a target's *shape* (size, age, languages, test runner, per-gate
applicability) but has nowhere to record its *authorities*. A repo CW didn't
build usually already has house rules — naming conventions, test standards, a
tech lead's standing objections, framework-specific patterns — and on a mature
codebase those are often packaged as harness skills. CW's generic review
checklist knows nothing about them, and the multi-AI quorum cannot infer them
from a diff, so a brownfield ticket gets reviewed against CW's defaults while
the conventions that would actually block it in human review are never
consulted.

Binding: ``<meta root>/adoption/review-authorities.json``, schema
``review-authorities/1``::

    {
      "schema": "review-authorities/1",
      "target": "owner/repo",
      "authoring":  [{"skill": "plugin:lang-developer", "reason": "…"}],
      "review":     [{"skill": "plugin:lang-reviewer",  "reason": "…"}],
      "operations": [{"skill": "plugin:ci-tool",        "reason": "…"}]
    }

Three phases, keyed to when a skill has something to say — ``authoring`` while
code is being written (`/implement` Step 6), ``review`` while it is being
critiqued (`/implement` Step 7, `/close-epic` Step 1), ``operations`` when the
loop touches deploy, observability, or the tracker.

**Failure modes mirror ``scope.json`` deliberately.** A MISSING file means no
authorities — the greenfield default, where CW's own checklist is the only
authority. It is *not* a claim that the target has no conventions, only that
none were recorded. A file that EXISTS but is unreadable raises: an unknown
phase key names itself rather than silently dropping every skill under it,
because an unreadable binding that renders identically to an absent one is
exactly how a target quietly loses its house rules.

**Authority boundary**: this file is written by the operator, never inferred.
Which skills hold authority over a repo is a judgment about ownership; inferring
it would be the same mistake as inferring contracts, which `/adopt` defers.

**Why this is not in ``artifacts.py``** (non-obvious, and load-bearing):
``chief_wiggum.hashing.scanner_version`` hashes a gate scanner's source PLUS its
dependencies, and four gates (``check_traceability``, ``check_single_writer``,
``quality_slop_gate``, ``ratchet``) depend on the meta-location resolver. Adding
this there would stale all four gate-validation records and demand re-authored
seeded-defect trials — for a change that has nothing to do with scanning. A
separate module that merely *imports* the resolver has no such effect.
``tests/test_gate_validation_retroactive.py`` pins that no gate scanner can
reach this module, so a later refactor cannot quietly undo the separation.

As a module:
    from review_authorities import load, skills_for
    ids = skills_for(resolver.meta_root, "review")

As a CLI:
    python3 scripts/review_authorities.py show <target> [--phase P] [--format json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifacts  # noqa: E402 — resolver import, never the reverse

SCHEMA = "review-authorities/1"
# Mirrors adopt.py's layout without importing it: pulling in adopt just for a
# directory name would drag its unrelated adoption dependencies into what is
# otherwise a small, dependency-light resolver.
ADOPTION_DIRNAME = "adoption"
AUTHORITIES_NAME = "review-authorities.json"

PHASES = ("authoring", "review", "operations")
ALLOWED_KEYS = ("schema", "target", "$comment", *PHASES)


def authorities_path(meta_root: Path | str) -> Path:
    return Path(meta_root) / ADOPTION_DIRNAME / AUTHORITIES_NAME


def _fail(path: Path, problem: str) -> ValueError:
    return ValueError(f"{path}: {problem}")


def _parse_phase(path: Path, phase: str, raw) -> list[dict]:
    if not isinstance(raw, list):
        raise _fail(path, f"phase {phase!r} must be a list, got {type(raw).__name__}")
    out: list[dict] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        where = f"phase {phase!r} entry {i}"
        if not isinstance(entry, dict):
            raise _fail(path, f"{where} must be an object, got {type(entry).__name__}")
        unknown = sorted(set(entry) - {"skill", "reason"})
        if unknown:
            raise _fail(path, f"{where} has unknown key(s) {unknown}")
        skill = entry.get("skill")
        if not isinstance(skill, str) or not skill.strip():
            raise _fail(path, f"{where} needs a non-empty string 'skill'")
        reason = entry.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise _fail(path, f"{where} 'reason' must be a string when present")
        if skill in seen:
            # Silently de-duplicating would hide an operator's edit conflict.
            raise _fail(path, f"phase {phase!r} lists duplicate skill {skill!r}")
        seen.add(skill)
        item = {"skill": skill}
        if reason is not None:
            item["reason"] = reason
        out.append(item)
    return out


def load(meta_root: Path | str) -> dict:
    """Read the binding for a resolved meta root.

    Returns ``{"present": bool, "path": str, "target": str|None,
    "authoring": [...], "review": [...], "operations": [...]}``. A missing file
    yields ``present: False`` and empty phases. A file that exists but does not
    parse raises ``ValueError`` naming the problem — never an empty result."""
    path = authorities_path(meta_root)
    empty = {"present": False, "path": str(path), "target": None,
             **{p: [] for p in PHASES}}
    if not path.exists():
        return empty
    try:
        text = path.read_text()
    except OSError as exc:
        raise _fail(path, f"unreadable: {exc}") from exc
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _fail(path, f"invalid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise _fail(path, f"top level must be an object, got {type(doc).__name__}")

    unknown = sorted(set(doc) - set(ALLOWED_KEYS))
    if unknown:
        # A typo'd phase ("reviews") must name ITSELF. Ignoring it would drop
        # every skill under it and read exactly like an empty phase.
        raise _fail(path, f"unknown key(s) {unknown}; allowed: {sorted(ALLOWED_KEYS)}")
    schema = doc.get("schema")
    if schema != SCHEMA:
        raise _fail(path, f"schema must be {SCHEMA!r}, got {schema!r}")
    target = doc.get("target")
    if not isinstance(target, str) or not target.strip():
        # Informational (which repo the operator wrote this for), not matched
        # against the resolved target id: a path-derived id and a written
        # "owner/repo" legitimately differ, and blocking on that would be a
        # gate that is noisy on correct input.
        raise _fail(path, "'target' must be a non-empty string naming the repo")

    out = {"present": True, "path": str(path), "target": target}
    for phase in PHASES:
        out[phase] = _parse_phase(path, phase, doc.get(phase, []))
    return out


def skills_for(meta_root: Path | str, phase: str) -> list[str]:
    """Skill ids holding authority over ``phase``, in file order."""
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}; expected one of {list(PHASES)}")
    return [e["skill"] for e in load(meta_root)[phase]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="an adopted repo's own review authorities, per phase (#264)")
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="print the authorities for a target")
    show.add_argument("target", help="target repo path")
    show.add_argument("--phase", choices=PHASES, default=None,
                      help="only this phase (default: all)")
    show.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    resolver = artifacts.Resolver.resolve(args.target)
    try:
        doc = load(resolver.meta_root)
    except ValueError as exc:
        # NEVER an empty result set on stdout here: empty-and-exit-0 is exactly
        # what a consumer reads as "no authorities", which is the silent loss
        # of house rules this binding exists to prevent.
        if args.format == "json":
            print(json.dumps({"error": "malformed_review_authorities",
                              "message": str(exc),
                              "path": str(authorities_path(resolver.meta_root))}),
                  file=sys.stderr)
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        if args.phase:
            print(json.dumps({"schema": SCHEMA, "target": doc["target"],
                              "path": doc["path"], "present": doc["present"],
                              "phase": args.phase,
                              "authorities": doc[args.phase]}, indent=2))
        else:
            print(json.dumps(doc, indent=2))
        return 0

    phases = [args.phase] if args.phase else list(PHASES)
    for phase in phases:
        for entry in doc[phase]:
            print(entry["skill"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
