#!/usr/bin/env python3
"""Validate the pattern registry and enforce the invariant-cluster model.

The registry (`patterns/registry.json` + each `patterns/<id>/manifest.json`) models
patterns as clusters of invariants (see docs/patterns-registry.md#patterns-as-clusters-of-invariants).
This linter makes the model's rules mechanical rather than trusted:

  1. registry.json + every referenced manifest parse, and the manifest `id`
     matches both the registry entry and its directory name.
  2. The bar for `status: specified`: the manifest declares a NON-EMPTY invariant
     cluster, and every entry has a well-formed `id` + non-empty `statement`.
     (`realized_as` provenance is OPTIONAL — an invariant may be design-derived —
     but when present it must name an `app` plus `code` or `id`.)
  3. Invariant ids are well-formed (`INV-<ABBR>-<SEQ>`) and unique within a pattern.
  4. Cross-references (`depends_on` / `feeds`) resolve to known ids, and no
     specified pattern depends on a mere candidate (a dangling floor).
  5. Every specified index entry carries an `invariants` summary string, keeping
     the registry index uniform with the manifests (so you can list clusters
     without opening each manifest).

Run report-only:   python3 scripts/check_patterns.py
Errors exit 1 (wired into `make lint`); warnings are reported but do not fail.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS_DIR = ROOT / "patterns"
REGISTRY = PATTERNS_DIR / "registry.json"

# Generic invariant id: INV-<ABBR>-<SEQ>, uppercase. SEQ allows a leading letter
# (e.g. the sibling branch id INV-FOWR-M1) and digits.
ID_RE = re.compile(r"^INV-[A-Z]+-[A-Z]?[0-9]+$")

ERROR = "error"
WARN = "warn"


@dataclass
class Finding:
    severity: str
    where: str
    message: str

    def __str__(self) -> str:
        tag = "ERROR" if self.severity == ERROR else "warn "
        return f"  [{tag}] {self.where}: {self.message}"


def _load_json(path: Path, where: str, findings: list[Finding]):
    """Return parsed JSON or None (appending an error finding on failure)."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        findings.append(Finding(ERROR, where, f"file not found: {path}"))
    except json.JSONDecodeError as exc:
        findings.append(Finding(ERROR, where, f"invalid JSON: {exc}"))
    return None


def cluster_entries(invariants) -> list:
    """Flatten a manifest/candidate `invariants` value into a list of entries.

    Accepts either a bare list (candidate inline form) or a dict with `cluster`
    plus an optional `sibling_*` branch that also carries a `cluster`.
    """
    if isinstance(invariants, list):
        return list(invariants)
    if isinstance(invariants, dict):
        entries = list(invariants.get("cluster", []))
        for key, val in invariants.items():
            if key.startswith("sibling") and isinstance(val, dict):
                entries.extend(val.get("cluster", []))
        return entries
    return []


def validate_cluster(entries: list, where: str, findings: list[Finding]) -> None:
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        loc = f"{where}[{i}]"
        if not isinstance(entry, dict):
            findings.append(Finding(ERROR, loc, "invariant entry must be an object"))
            continue
        cid = entry.get("id", "")
        if not cid:
            findings.append(Finding(ERROR, loc, "invariant missing `id`"))
        elif not ID_RE.match(cid):
            findings.append(Finding(ERROR, loc, f"malformed invariant id: {cid!r} (want INV-ABBR-SEQ)"))
        elif cid in seen:
            findings.append(Finding(ERROR, loc, f"duplicate invariant id within pattern: {cid}"))
        else:
            seen.add(cid)
        if not str(entry.get("statement", "")).strip():
            findings.append(Finding(ERROR, loc, f"invariant {cid or '?'} missing `statement`"))
        ra = entry.get("realized_as")
        if ra is not None:  # provenance is optional, but well-formed when present
            if not isinstance(ra, dict) or not ra.get("app") or not (ra.get("code") or ra.get("id")):
                findings.append(Finding(ERROR, loc, f"invariant {cid or '?'} `realized_as` needs `app` plus `code` or `id`"))


def _refs(value) -> list[str]:
    """Parse a comma-joined ref string (registry index form) into ids."""
    if isinstance(value, str):
        return [r.strip() for r in value.split(",") if r.strip()]
    if isinstance(value, dict):
        return list(value.keys())
    return []


def validate(registry_path: Path = REGISTRY) -> list[Finding]:
    findings: list[Finding] = []
    reg = _load_json(registry_path, "registry.json", findings)
    if reg is None:
        return findings
    # Resolve spec/manifest paths relative to the registry's repo root
    # (patterns/registry.json -> repo root), so the linter is testable on a
    # fixture registry, not just the real one.
    base = registry_path.resolve().parent.parent

    specified = reg.get("patterns", [])
    candidates = reg.get("candidates", [])
    known_ids = {e.get("id") for e in specified + candidates if e.get("id")}
    candidate_ids = {e.get("id") for e in candidates if e.get("id")}

    for entry in specified:
        pid = entry.get("id", "?")
        where = f"patterns/{pid}"
        if entry.get("status") != "specified":
            findings.append(Finding(WARN, where, f"listed under patterns[] but status={entry.get('status')!r}"))

        man_rel = entry.get("manifest")
        if not man_rel or not entry.get("spec"):
            findings.append(Finding(ERROR, where, "registry entry missing `spec` and/or `manifest` path"))
            continue
        spec_path = base / entry["spec"]
        if not spec_path.exists():
            findings.append(Finding(ERROR, where, f"spec file missing: {entry['spec']}"))
        man_path = base / man_rel
        manifest = _load_json(man_path, f"{where} (manifest)", findings)
        if manifest is None:
            continue

        if manifest.get("id") != pid:
            findings.append(Finding(ERROR, where, f"manifest id {manifest.get('id')!r} != registry id {pid!r}"))
        if man_path.parent.name != pid:
            findings.append(Finding(ERROR, where, f"manifest directory {man_path.parent.name!r} != id {pid!r}"))

        if not str(entry.get("invariants", "")).strip():
            findings.append(Finding(
                ERROR, where,
                "registry index entry missing `invariants` summary string "
                "(keep the index uniform with the manifest cluster)"))

        entries = cluster_entries(manifest.get("invariants"))
        if not entries:
            findings.append(Finding(
                ERROR, where,
                "specified pattern must declare a non-empty invariant cluster "
                "(the bar for status: specified)"))
        else:
            validate_cluster(entries, f"{where}.invariants.cluster", findings)

        for dep in _refs(entry.get("depends_on")) + _refs(manifest.get("depends_on")):
            if dep not in known_ids:
                findings.append(Finding(ERROR, where, f"depends_on unknown pattern id: {dep}"))
            elif dep in candidate_ids:
                findings.append(Finding(WARN, where, f"specified pattern depends_on a candidate (not-yet-specified floor): {dep}"))
        for fed in _refs(entry.get("feeds")):
            if fed not in known_ids:
                findings.append(Finding(ERROR, where, f"feeds unknown pattern id: {fed}"))

    for entry in candidates:
        cid = entry.get("id", "?")
        inv = entry.get("invariants")
        if inv is not None:
            validate_cluster(cluster_entries(inv), f"candidates/{cid}.invariants", findings)

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the pattern registry / invariant-cluster model.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--registry", type=Path, default=REGISTRY, help="registry.json path (for testing)")
    args = parser.parse_args()

    findings = validate(args.registry)
    errors = [f for f in findings if f.severity == ERROR]
    warns = [f for f in findings if f.severity == WARN]

    if args.format == "json":
        print(json.dumps([f.__dict__ for f in findings], indent=2))
    else:
        if not findings:
            print("check_patterns: registry OK — invariant-cluster model holds.")
        else:
            print(f"check_patterns: {len(errors)} error(s), {len(warns)} warning(s)")
            for f in findings:
                print(f)

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
