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

  6. Stamps the pattern's `scaffold/` code (the seams that realize the cluster)
     into the target repo with parameters bound, when the pattern ships one.
     Scaffold files are code, so stamping is idempotent: an existing target is
     skipped (never clobbered) unless `--force` is passed. Templating replaces
     `{{param}}` in both the target path and the file body with the bound value.
     Scaffold stamping needs every REQUIRED param bound; if any is unresolved the
     contract pack still installs and the scaffold is skipped with a note.

    python3 scripts/apply_pattern.py fetch-on-webhook-reconcile \\
        --target-dir /path/to/repo --param resource=subscription --param projected_field=plan
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from check_patterns import cluster_entries  # noqa: E402
from ratchet import DEFAULT_PROTECTED  # noqa: E402

ROOT = SCRIPTS.parent
REGISTRY = ROOT / "patterns" / "registry.json"

ADOPTED_REL = "docs/patterns/adopted.json"
RATCHET_REL = "docs/quality/ratchet.json"
PATTERN_GLOB = "docs/patterns/**"
SCAFFOLD_DIR = "scaffold"
SCAFFOLD_MANIFEST = "scaffold.json"
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


class ApplyError(Exception):
    """Usage / resolution problem. Maps to exit 2."""


@dataclass
class Plan:
    pattern_id: str
    version: int
    files: dict[str, str] = field(default_factory=dict)   # repo-rel path -> content (contract pack; regenerated, overwrite ok)
    scaffold_files: dict[str, str] = field(default_factory=dict)  # repo-rel path -> rendered code (idempotent, never clobbered without --force)
    scaffold_skipped: str = ""                            # note when scaffold stamping was skipped
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


def _render(text: str, bound: dict[str, str]) -> str:
    """Substitute {{param}} with its bound value, leaving an unbound placeholder
    verbatim. Callers (_render_scaffold) then fail closed on any survivor, so a
    template referencing a param that isn't bound is a hard error, not silent
    {{param}} leaking into stamped code."""
    return _PLACEHOLDER.sub(lambda m: bound.get(m.group(1), m.group(0)), text)


def load_scaffold(pattern_id: str, base: Path = ROOT) -> dict | None:
    """Read + structurally validate a pattern's scaffold/scaffold.json, or None if
    it ships no scaffold. A malformed manifest is a clear ApplyError, never an
    uncaught AttributeError (which would abort the contract-pack install too)."""
    path = base / "patterns" / pattern_id / SCAFFOLD_DIR / SCAFFOLD_MANIFEST
    if not path.is_file():
        return None
    try:
        scaffold = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ApplyError(f"malformed scaffold manifest {path}: {exc}") from exc
    if not isinstance(scaffold, dict):
        raise ApplyError(f"scaffold manifest {path} must be a JSON object")
    files = scaffold.get("files")
    if not isinstance(files, list) or not files:
        raise ApplyError(f"scaffold manifest {path} needs a non-empty 'files' array")
    for entry in files:
        if not isinstance(entry, dict) or not entry.get("template") or not entry.get("target"):
            raise ApplyError(f"scaffold file entry needs string 'template'+'target': {entry!r}")
    return scaffold


def _render_scaffold(pattern_id: str, scaffold: dict, bound: dict[str, str],
                     base: Path = ROOT) -> dict[str, str]:
    """Render each scaffold template into {target-relpath -> content}, params bound
    in both the target path and the body. Fails closed if any {{param}} survives
    the render (a template referencing a param that isn't bound)."""
    out: dict[str, str] = {}
    scaffold_dir = base / "patterns" / pattern_id / SCAFFOLD_DIR
    for entry in scaffold["files"]:  # structure validated in load_scaffold
        tmpl = entry["template"]
        target = entry["target"]
        tpath = scaffold_dir / tmpl
        if not tpath.is_file():
            raise ApplyError(f"scaffold template missing: {tpath}")
        rel = _render(target, bound)
        # Fail closed on a target that would escape the target repo. Scaffold
        # manifests (and any param bound into a path) are stamped into a real
        # checkout — an absolute path or a `..` segment must never write outside it.
        pp = PurePosixPath(rel)
        if _PLACEHOLDER.search(rel):
            raise ApplyError(f"scaffold target path has an unbound placeholder: {rel}")
        if pp.is_absolute() or ".." in pp.parts or rel != pp.as_posix():
            raise ApplyError(f"scaffold target must be a repo-relative path without '..': {rel!r}")
        body = _render(tpath.read_text(), bound)
        leftover = _PLACEHOLDER.search(body)
        if leftover:
            raise ApplyError(
                f"scaffold template {tmpl} references unbound param "
                f"{{{{{leftover.group(1)}}}}} — bind it or give it a manifest default")
        out[rel] = body
    return out


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

    # Scaffold (the pattern's code seams). Needs every required param bound, since
    # the templates reference them; if any is unresolved the contract pack still
    # installs and the scaffold is skipped with a note.
    scaffold = load_scaffold(pattern_id, base)
    if scaffold is not None:
        if unresolved:
            plan.scaffold_skipped = (
                "scaffold not stamped — unbound required param(s): "
                + ", ".join(sorted(unresolved))
            )
        else:
            plan.scaffold_files = _render_scaffold(pattern_id, scaffold, bound, base)
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


def apply_plan(plan: Plan, target: Path, write: bool = True, force: bool = False) -> list[str]:
    actions: list[str] = []
    for rel, content in plan.files.items():
        actions.append(f"write {rel}")
        if write:
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    # Scaffold code: idempotent. An existing target is skipped (never clobbered)
    # unless --force, so hand-edits to stamped seams survive re-application.
    target_root = target.resolve()
    for rel, content in plan.scaffold_files.items():
        path = target / rel
        # Defense in depth beyond the pure-path guard in _render_scaffold: resolve
        # the destination (following any symlink) and confirm it stays inside the
        # target repo, so a symlinked path component can't redirect the write out.
        resolved_parent = path.parent.resolve()
        if resolved_parent != target_root and target_root not in resolved_parent.parents:
            raise ApplyError(f"scaffold target escapes the repo via symlink: {rel!r}")
        if path.exists() and not force:
            actions.append(f"skip scaffold {rel} (exists — use --force to re-stamp)")
            continue
        verb = "re-stamp" if path.exists() else "stamp"
        actions.append(f"{verb} scaffold {rel}")
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    if plan.scaffold_skipped:
        actions.append(plan.scaffold_skipped)

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


def list_adopted(target_dir: Path, registry_path: Path = REGISTRY, base: Path = ROOT) -> list[dict]:
    """Read a target repo's adopted.json and return each pattern's fresh cluster.

    Statements are re-read from the registry manifest (source of truth), so
    `/architect` folds current invariant text — not whatever was stamped earlier.
    """
    path = Path(target_dir) / ADOPTED_REL
    if not path.is_file():
        return []
    doc = json.loads(path.read_text())
    registry = load_registry(registry_path)
    out: list[dict] = []
    for pid, rec in doc.get("patterns", {}).items():
        try:
            manifest = load_manifest(find_specified(registry, pid), base)
            cluster = [e for e in cluster_entries(manifest.get("invariants")) if isinstance(e, dict)]
        except ApplyError:
            cluster = []
        out.append({
            "id": pid,
            "version": rec.get("version"),
            "contract_pack": f"docs/patterns/{pid}/invariants.md",
            "invariants": [{"id": e.get("id"), "statement": e.get("statement", "")}
                           for e in cluster if e.get("id")],
            "unresolved": rec.get("unresolved", []),
        })
    return out


def catalog(registry_path: Path = REGISTRY, base: Path = ROOT) -> list[dict]:
    """The selectable menu for `/seed`: every pattern with its `applies_when`.

    Specified patterns carry their manifest `applies_when` (the selection criteria
    a human/model reasons over); candidates are listed with `status: candidate` so
    `/seed` can flag them as available-but-not-yet-installable.
    """
    reg = load_registry(registry_path)
    out: list[dict] = []
    for entry in reg.get("patterns", []):
        try:
            manifest = load_manifest(entry, base)
            applies = manifest.get("applies_when", [])
        except ApplyError:
            applies = []
        out.append({
            "id": entry.get("id"),
            "title": entry.get("title"),
            "category": entry.get("category"),
            "status": "specified",
            "applies_when": applies,
            "invariants": entry.get("invariants", ""),
            "depends_on": entry.get("depends_on"),
            "summary": entry.get("summary", ""),
        })
    for c in reg.get("candidates", []):
        out.append({
            "id": c.get("id"),
            "title": c.get("title"),
            "category": c.get("category"),
            "status": "candidate",
            "applies_when": [],
            "summary": c.get("summary", ""),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Install a registry pattern's contract pack into a target repo.")
    parser.add_argument("pattern_id", nargs="?", help="Specified pattern id from patterns/registry.json")
    parser.add_argument("--target-dir", type=Path, help="Local path to the target repo")
    parser.add_argument("--param", action="append", default=[], metavar="k=v", help="Bind a pattern parameter")
    parser.add_argument("--catalog", action="store_true",
                        help="Print the selectable pattern menu (for /seed) and exit")
    parser.add_argument("--list-adopted", action="store_true",
                        help="List the target's adopted patterns + clusters (for /architect) and exit")
    parser.add_argument("--now", help="ISO timestamp for the adoption record (testing)")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without writing")
    parser.add_argument("--force", action="store_true",
                        help="Re-stamp scaffold files that already exist (default: skip, never clobber)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    if args.catalog:
        items = catalog()
        if args.format == "json":
            print(json.dumps(items, indent=2))
        else:
            for c in items:
                mark = "" if c["status"] == "specified" else "  [candidate]"
                print(f"{c['id']} ({c['category']}){mark}")
                for w in c.get("applies_when", []):
                    print(f"    · {w}")
        return 0

    if not args.target_dir:
        print("apply_pattern: --target-dir is required (except with --catalog)", file=sys.stderr)
        return 2

    if args.list_adopted:
        adopted = list_adopted(args.target_dir)
        if args.format == "json":
            print(json.dumps(adopted, indent=2))
        elif not adopted:
            print(f"apply_pattern: no adopted patterns in {args.target_dir}")
        else:
            for a in adopted:
                tbd = f"  (unbound: {', '.join(a['unresolved'])})" if a["unresolved"] else ""
                print(f"{a['id']} v{a['version']} — {len(a['invariants'])} invariants{tbd}")
                for inv in a["invariants"]:
                    print(f"  {inv['id']}: {inv['statement']}")
        return 0

    if not args.pattern_id:
        print("apply_pattern: a pattern id is required (or use --list-adopted)", file=sys.stderr)
        return 2

    provided: dict[str, str] = {}
    for kv in args.param:
        if "=" not in kv:
            print(f"bad --param {kv!r} (want k=v)", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        provided[k.strip()] = v.strip()

    try:
        plan = build_plan(args.pattern_id, provided, now=args.now)
        actions = apply_plan(plan, args.target_dir, write=not args.dry_run, force=args.force)
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
