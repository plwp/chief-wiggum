#!/usr/bin/env python3
"""Install a registry pattern's *contract pack* into a target repo.

A registry pattern is, first, a cluster of invariants plus the parameters and
protected paths that make it real (see docs/patterns-registry.md). This installer
does the mechanical, scaffold-independent half of `/apply-pattern`:

  1. Loads the pattern manifest (must be `status: specified`).
  2. Binds parameters from `--param k=v`; any unbound REQUIRED parameter becomes a
     `TBD:` marker so `check_unresolved.py` blocks dependent work on a guess.
  3. Stamps the invariant cluster into the target as a contract-pack doc
     (`docs/patterns/<id>/invariants.md`) with stable ids, ready for `/architect`
     to fold into an epic's `invariants.md`.
  4. Records the adoption in `docs/patterns/adopted.json` (id, version, provenance,
     bound params, cluster ids, protected-path intents).
  5. Registers `docs/patterns/**` into the target's `docs/quality/ratchet.json`
     protected paths, so the adopted contract pack becomes a goalpost workers
     can't move.

Scaffold stamping (the pattern's `scaffold/` files) is deliberately out of scope
until scaffolds exist — this installs the contract pack, not the code.

    python3 scripts/apply_pattern.py fetch-on-webhook-reconcile \\
        --target-dir /path/to/repo --param resource=subscription --param projected_field=plan
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from check_patterns import cluster_entries  # noqa: E402
from ratchet import DEFAULT_PROTECTED  # noqa: E402

ROOT = SCRIPTS.parent
REGISTRY = ROOT / "patterns" / "registry.json"

ADOPTED_REL = "docs/patterns/adopted.json"
RATCHET_REL = "docs/quality/ratchet.json"
PATTERN_GLOB = "docs/patterns/**"


class ApplyError(Exception):
    """Usage / resolution problem. Maps to exit 2."""


@dataclass
class Plan:
    pattern_id: str
    version: int
    files: dict[str, str] = field(default_factory=dict)   # repo-rel path -> content
    ratchet_add: list[str] = field(default_factory=list)  # globs to merge into protected_paths
    bound: dict[str, str] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)   # required params with no value
    _adoption: dict = field(default_factory=dict)         # adopted.json record, built in build_plan


def load_registry(path: Path = REGISTRY) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ApplyError(f"registry not found: {path}") from exc


def find_specified(registry: dict, pattern_id: str) -> dict:
    for entry in registry.get("patterns", []):
        if entry.get("id") == pattern_id:
            return entry
    for entry in registry.get("candidates", []):
        if entry.get("id") == pattern_id:
            raise ApplyError(
                f"'{pattern_id}' is a candidate, not a specified pattern — it has no "
                f"manifest to install. Specify it first.")
    raise ApplyError(f"unknown pattern id: {pattern_id!r}")


def load_manifest(entry: dict, base: Path = ROOT) -> dict:
    rel = entry.get("manifest")
    if not rel:
        raise ApplyError(f"registry entry for {entry.get('id')} has no manifest path")
    path = base / rel
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ApplyError(f"manifest not found: {path}") from exc


def resolve_params(manifest: dict, provided: dict[str, str]) -> tuple[dict, list[str]]:
    """Bind provided params over the manifest schema. Returns (bound, unresolved)."""
    schema = manifest.get("parameters", {}) or {}
    bound: dict[str, str] = {}
    unresolved: list[str] = []
    for name, spec in schema.items():
        if name in provided:
            bound[name] = provided[name]
        elif not spec.get("required", False) and "default" in spec:
            bound[name] = spec["default"]
        elif spec.get("required", False):
            unresolved.append(name)
    unknown = [k for k in provided if k not in schema]
    if unknown:
        raise ApplyError(f"unknown parameter(s) for {manifest.get('id')}: {', '.join(sorted(unknown))}")
    return bound, unresolved


def _protected_intents(manifest: dict) -> list[str]:
    return manifest.get("protected_paths") or manifest.get("protected_paths_added") or []


def _invariants_doc(manifest: dict, cluster: list[dict], bound: dict, unresolved: list[str]) -> str:
    pid = manifest.get("id")
    lines = [
        f"# Pattern contract pack: {manifest.get('title', pid)}",
        "",
        f"> Installed by `apply_pattern.py` from the `{pid}` registry pattern.",
        "> `/architect` folds this invariant cluster into the epic's `invariants.md`",
        "> with these stable ids; the traceability, single-writer, and ratchet gates",
        "> then hold the cluster. Do not edit the ids — they are the pattern's contract.",
        "",
        "## Invariant cluster",
        "",
    ]
    for e in cluster:
        cid = e.get("id", "?")
        stmt = e.get("statement", "").strip()
        lines.append(f"- **{cid}** — {stmt}")
        ra = e.get("realized_as")
        if isinstance(ra, dict):
            ref = ra.get("code") or ra.get("id") or ""
            app = ra.get("app", "")
            if ref:
                lines.append(f"  - _reference impl:_ {app} `{ref}`")
            elif app:
                lines.append(f"  - _reference impl:_ {app}")
    lines += ["", "## Bound parameters", ""]
    if bound:
        for k, v in bound.items():
            lines.append(f"- `{k}` = `{v}`")
    else:
        lines.append("_(none bound yet)_")
    if unresolved:
        lines += ["", "## Unbound required parameters", ""]
        schema = manifest.get("parameters", {})
        for name in unresolved:
            desc = schema.get(name, {}).get("description", "")
            lines.append(f"- TBD: bind `{name}` — {desc}".rstrip())
    intents = _protected_intents(manifest)
    if intents:
        lines += ["", "## Protected-path intents", "",
                  "_Map these to real code paths and add them to the ratchet's `protected_paths`:_", ""]
        lines += [f"- {p}" for p in intents]
    lines.append("")
    return "\n".join(lines)


def build_plan(pattern_id: str, provided: dict[str, str],
               registry_path: Path = REGISTRY, base: Path = ROOT,
               now: str | None = None) -> Plan:
    registry = load_registry(registry_path)
    entry = find_specified(registry, pattern_id)
    manifest = load_manifest(entry, base)
    cluster = [e for e in cluster_entries(manifest.get("invariants")) if isinstance(e, dict)]
    bound, unresolved = resolve_params(manifest, provided)

    plan = Plan(pattern_id=pattern_id, version=int(manifest.get("version", 1)),
                bound=bound, unresolved=unresolved,
                ratchet_add=[PATTERN_GLOB])
    plan.files[f"docs/patterns/{pattern_id}/invariants.md"] = _invariants_doc(
        manifest, cluster, bound, unresolved)
    plan._adoption = {  # stashed for the adopted.json merge in apply_plan
        "version": plan.version,
        "applied_at": now or datetime.now(timezone.utc).isoformat(),
        "provenance": {"registry": "chief-wiggum", "manifest": entry.get("manifest")},
        "parameters": bound,
        "unresolved": unresolved,
        "invariants": [e.get("id") for e in cluster if e.get("id")],
        "protected_path_intents": _protected_intents(manifest),
    }
    return plan


def _merge_adopted(target: Path, pattern_id: str, adoption: dict) -> str:
    path = target / ADOPTED_REL
    if path.is_file():
        doc = json.loads(path.read_text())
    else:
        doc = {"$comment": "CW registry patterns adopted by this product (apply_pattern.py).",
               "patterns": {}}
    doc.setdefault("patterns", {})[pattern_id] = adoption
    return json.dumps(doc, indent=2) + "\n"


def _merge_ratchet(target: Path, add: list[str]) -> tuple[str | None, list[str]]:
    """Return (new content or None if no change, list of globs actually added)."""
    path = target / RATCHET_REL
    if path.is_file():
        cfg = json.loads(path.read_text())
        existing = list(cfg.get("protected_paths", list(DEFAULT_PROTECTED)))
    else:
        cfg = {"$comment": "Ratchet config stub created by apply_pattern.py; run `ratchet.py init` to complete it.",
               "protected_paths": list(DEFAULT_PROTECTED)}
        existing = cfg["protected_paths"]
    added = [g for g in add if g not in existing]
    if not added and path.is_file():
        return None, []
    cfg["protected_paths"] = existing + added
    return json.dumps(cfg, indent=2) + "\n", added


def apply_plan(plan: Plan, target: Path, write: bool = True) -> list[str]:
    actions: list[str] = []
    for rel, content in plan.files.items():
        actions.append(f"write {rel}")
        if write:
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    adopted = _merge_adopted(target, plan.pattern_id, plan._adoption)
    actions.append(f"record adoption in {ADOPTED_REL}")
    if write:
        p = target / ADOPTED_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(adopted)

    ratchet_content, added = _merge_ratchet(target, plan.ratchet_add)
    if added:
        actions.append(f"register protected paths in {RATCHET_REL}: {', '.join(added)}")
        if write:
            p = target / RATCHET_REL
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(ratchet_content)
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description="Install a registry pattern's contract pack into a target repo.")
    parser.add_argument("pattern_id", help="Specified pattern id from patterns/registry.json")
    parser.add_argument("--target-dir", required=True, type=Path, help="Local path to the target repo")
    parser.add_argument("--param", action="append", default=[], metavar="k=v", help="Bind a pattern parameter")
    parser.add_argument("--now", help="ISO timestamp for the adoption record (testing)")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without writing")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    provided: dict[str, str] = {}
    for kv in args.param:
        if "=" not in kv:
            print(f"bad --param {kv!r} (want k=v)", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        provided[k.strip()] = v.strip()

    try:
        plan = build_plan(args.pattern_id, provided, now=args.now)
        actions = apply_plan(plan, args.target_dir, write=not args.dry_run)
    except ApplyError as exc:
        print(f"apply_pattern: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps({"pattern": plan.pattern_id, "actions": actions,
                          "unresolved": plan.unresolved, "dry_run": args.dry_run}, indent=2))
    else:
        verb = "PLAN (dry-run)" if args.dry_run else "applied"
        print(f"apply_pattern: {verb} {plan.pattern_id} -> {args.target_dir}")
        for a in actions:
            print(f"  - {a}")
        if plan.unresolved:
            print(f"  ! {len(plan.unresolved)} unbound required param(s) written as TBD: {', '.join(plan.unresolved)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
