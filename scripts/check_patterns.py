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
  6. Every pattern declares `success_metrics.metrics` (docs/patterns-registry.md:
     "every pattern must declare them") — missing/empty is an ERROR for a
     specified pattern, a WARN for a candidate.

Run report-only:   python3 scripts/check_patterns.py
Errors exit 1 (wired into `make lint`); warnings are reported but do not fail.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.trace_ids import kind_id_re  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PATTERNS_DIR = ROOT / "patterns"
REGISTRY = PATTERNS_DIR / "registry.json"

# Generic invariant id: the same three-segment KIND-SLUG-NNN grammar every
# stable-ID consumer shares (chief_wiggum.trace_ids), restricted to INV.
# Built from kind_id_re rather than hand-rolled so a pattern-manifest id can
# never again go registry-valid-yet-scanner-invisible — the old
# ``^INV-[A-Z]+-[A-Z]?[0-9]+$`` tolerated a letter-suffixed SEQ (e.g. the
# sibling branch id INV-FOWR-M1) that chief_wiggum.trace_ids.ID_RE cannot
# parse, so a pattern id could pass this linter yet be invisible to the
# traceability scanner once copied verbatim into an epic (chief-wiggum#294).
ID_RE = kind_id_re("INV")

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


@dataclass
class PatternsReport:
    """Findings PLUS what was actually measured (#289) — the same discipline
    as ``check_traceability.py``/``check_unresolved.py``: a registry that
    validates because it declares nothing must not look identical to one
    that validates because it genuinely holds.

    ``registry_readable`` is ``False`` only when ``registry.json`` itself
    could not be read/parsed, OR parsed to something other than a JSON
    object (the exact "top-level JSON array -> AttributeError" crash this
    ticket closes) — a BROKEN INSTRUMENT, never a pass.
    """

    findings: list[Finding]
    registry_readable: bool = True
    specified_count: int = 0
    candidate_count: int = 0
    pattern_dirs_scanned: int = 0

    @property
    def applicability(self) -> str:
        """The standard three-state gate vocabulary (#289): `applicable` /
        `inapplicable` / `error`. `inapplicable` is honest absence — an empty
        registry with zero manifest-bearing directories under `patterns/`,
        i.e. nothing at all to validate. `error` is the registry itself being
        unreadable/malformed — the instrument never got to look at anything."""
        if not self.registry_readable:
            return "error"
        if self.specified_count == 0 and self.candidate_count == 0 and self.pattern_dirs_scanned == 0:
            return "inapplicable"
        return "applicable"

    @property
    def outcome(self) -> str:
        """The standard four-state gate outcome (#289): pass | findings |
        inapplicable | error. Derived, never stored."""
        if self.applicability in ("error", "inapplicable"):
            return self.applicability
        return "findings" if self.findings else "pass"

    @property
    def measured(self) -> dict:
        return {
            "specified_patterns": self.specified_count,
            "candidate_patterns": self.candidate_count,
            "pattern_dirs_scanned": self.pattern_dirs_scanned,
        }


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
    """Backward-compatible entry point: just the findings list. Callers that
    need to know whether the run actually MEASURED anything (#289) use
    ``validate_report``."""
    return validate_report(registry_path).findings


def validate_report(registry_path: Path = REGISTRY) -> PatternsReport:
    findings: list[Finding] = []
    reg = _load_json(registry_path, "registry.json", findings)
    if reg is None:
        # _load_json already appended the ERROR finding (missing file /
        # invalid JSON) — the registry was never readable.
        return PatternsReport(findings=findings, registry_readable=False)
    if not isinstance(reg, dict):
        # #289: registry.json parsed fine but isn't a JSON OBJECT (e.g. a
        # top-level array) — `reg.get(...)` below would raise a bare
        # AttributeError. That crash is a worse failure mode than silence:
        # convert it into a loud, structured `error` finding instead.
        findings.append(Finding(
            ERROR, "registry.json",
            f"must be a JSON object with `patterns`/`candidates` keys, got {type(reg).__name__}",
        ))
        return PatternsReport(findings=findings, registry_readable=False)
    # Resolve spec/manifest paths relative to the registry's repo root
    # (patterns/registry.json -> repo root), so the linter is testable on a
    # fixture registry, not just the real one.
    base = registry_path.resolve().parent.parent
    patterns_dir = registry_path.resolve().parent

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

        sm = manifest.get("success_metrics")
        if not (sm.get("metrics") if isinstance(sm, dict) else None):
            findings.append(Finding(
                ERROR, where,
                "specified pattern must declare non-empty `success_metrics.metrics` "
                "(every pattern declares its success metrics)"))

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
        sm = entry.get("success_metrics")
        if not (sm.get("metrics") if isinstance(sm, dict) else None):
            findings.append(Finding(
                WARN, f"candidates/{cid}",
                "candidate has no `success_metrics.metrics` yet (required at promotion to specified)"))

    # #289: registry <-> patterns/ bijection. Scoped to DIRECT children of
    # `patterns/` that carry their OWN `manifest.json` — this naturally
    # excludes non-pattern directories that legitimately live alongside the
    # registry (a reference-data directory with no manifest.json, or a
    # nested stack-profile registry whose own manifests sit two levels down
    # and are governed by a DIFFERENT registry.json) without special-casing
    # either by name. Before this, an empty-but-"valid" registry ({}) walked
    # NONE of `patterns/` and printed "registry OK" even with real,
    # unregistered manifest directories sitting right next to it.
    pattern_dirs_scanned = 0
    if patterns_dir.is_dir():
        for child in sorted(patterns_dir.iterdir()):
            if not child.is_dir() or not (child / "manifest.json").is_file():
                continue
            pattern_dirs_scanned += 1
            if child.name not in known_ids:
                findings.append(Finding(
                    ERROR, f"patterns/{child.name}",
                    "directory has a manifest.json but is not listed in registry.json "
                    "(neither `patterns[]` nor `candidates[]`) — "
                    "registry<->patterns/ bijection violated",
                ))

    return PatternsReport(
        findings=findings,
        registry_readable=True,
        specified_count=len(specified),
        candidate_count=len(candidates),
        pattern_dirs_scanned=pattern_dirs_scanned,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the pattern registry / invariant-cluster model.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--registry", type=Path, default=REGISTRY, help="registry.json path (for testing)")
    args = parser.parse_args()

    report = validate_report(args.registry)
    findings = report.findings
    errors = [f for f in findings if f.severity == ERROR]
    warns = [f for f in findings if f.severity == WARN]

    # Factory telemetry (opt-in; no-op unless CW_TELEMETRY/CW_FACTORY_LOG set).
    try:
        from factory_log import emit_gate
        emit_gate("check_patterns", "fail" if errors else "pass", caught=len(errors), repo="chief-wiggum")
    except Exception:  # telemetry must never break the gate
        pass

    if args.format == "json":
        print(json.dumps({
            "findings": [f.__dict__ for f in findings],
            # #289: the outcome and its denominator travel with the
            # findings, so a zero count is never ambiguous about WHY it is
            # zero (a genuinely clean registry vs. one that measured nothing).
            "applicability": report.applicability,
            "outcome": report.outcome,
            "measured": report.measured,
        }, indent=2))
    else:
        c = report.measured
        print(
            f"Measured: {c['specified_patterns']} specified, {c['candidate_patterns']} "
            f"candidate pattern(s), {c['pattern_dirs_scanned']} manifest dir(s) scanned "
            f"under patterns/"
        )
        if report.applicability == "error":
            print("check_patterns: ERROR — registry.json could not be read as a valid "
                  "JSON object; nothing was checked (not a real pass)\n")
            for f in findings:
                print(f)
        elif report.applicability == "inapplicable":
            print("check_patterns: INAPPLICABLE — no registry entries and no pattern "
                  "manifest directories found; nothing to check (not a real pass)")
        elif not findings:
            print("check_patterns: registry OK — invariant-cluster model holds.")
        else:
            print(f"check_patterns: {len(errors)} error(s), {len(warns)} warning(s)")
            for f in findings:
                print(f)

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
