#!/usr/bin/env python3
"""Every EXTERNAL interface an epic describes must cite a source somebody saw.

The failure this exists for (chief-wiggum#350): an epic shipped nine tool
schemas and their routes for a third-party system, carrying a prose caveat --
*"this ticket's own documented, best-effort approximation... not a verified
contract"* -- and no machine-checked marker. Every live call 404'd until it was
fixed after deploy. The ratchet recorded ``gate_failed: 0``: the artifacts had
45 ``TBD:`` markers, all of them on DATA unknowns (a venue GUID, a token). The
INTERFACE guesses -- routes, envelope, schemas -- carried none, so they gated
nothing. CW's "unknowns gate work" discipline fired for data and not for shape.

Why this is a presence check and not a prose lint
-------------------------------------------------
#350 also proposed flagging hedge words ("best-effort", "approximation",
"assumed", "guess") in contract artifacts. Measured against 314 shipped epic
artifacts across 9 repos, that lint is unshippable: ``best-effort`` alone fired
84 times and every hit was a genuine design property ("a best-effort emitter",
"single async best-effort recorder") -- a term of art, not a hedge. A gate that
noisy teaches the operator to ``--force`` past it, which costs more than it
catches (``docs/gate-rollout.md``). Intent in prose is not mechanically
decidable; the presence of a cited source is.

So the rule is structural. An operation carrying ``"external": true`` must
either

  * cite verification-grade provenance -- a ``derived_from`` entry of type
    ``observed_fact`` (somebody captured a real response) or ``api_doc``
    (somebody read the vendor's published contract); or
  * carry an unresolved marker, in which case ``check_unresolved.py`` already
    gates the dependent work and this gate stands down.

``ticket``, ``acceptance_criterion``, ``user_input`` and ``epic_invariant`` are
deliberately NOT verification-grade. They record who ASKED for the interface,
never that anyone looked at it -- and in the failure above the guessed schemas
were faithfully derived from the ticket.

Provenance is required on the OPERATION, not inherited from its entity. One
``api_doc`` cite at entity level would cover twenty guessed routes, which is
precisely the shape of the original defect.

The declaration gap
-------------------
``external`` is optional in the schema, on purpose, so an author cannot be
blocked into lying. That leaves an omission evasion: guess a vendor route and
simply never declare it external. This gate cannot close that hole -- nothing
mechanical can tell a vendor path from an own-API path -- so it refuses to
hide it. When an epic declares operations and NONE are external, the report
says so as a named ``declaration_gap`` rather than printing a clean zero. An
epic that genuinely integrates nothing external is a normal, valid state; an
epic that integrates Stripe and declares nothing is a lie the report at least
makes visible to a human.

Outcome vocabulary is #289's five states -- ``pass | findings | inapplicable |
error`` plus ``skipped`` upstream -- and every count travels with its
denominator, so a zero is never ambiguous about why it is zero.

Gate status: REPORT-ONLY by default, per ``docs/gate-rollout.md``. It exits 0
with findings unless ``--gate`` is passed, and it must not be wired as a
blocker until it carries a passing ``validation/interface_provenance.json``
record (``docs/gate-validation.md``).

CLI:
    python3 scripts/check_interface_provenance.py <epic-dir-or-file> [...]
        [--format text|json] [--gate]

Exit codes:
    0  pass, or findings while report-only, or inapplicable
    1  findings, under --gate
    2  usage error (no such target)
    3  an artifact was present and could not be read -- a broken instrument,
       which is never a pass, and is non-zero even report-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_unresolved import MARKER_RE  # noqa: E402

# Provenance types that record OBSERVATION of the real interface. Keep this set
# closed: adding "ticket" here would restore the exact defect #350 was filed for.
VERIFIED_SOURCE_TYPES = frozenset({"observed_fact", "api_doc"})

CONTRACT_FILENAMES = ("contracts.json",)

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_ERROR = 3


@dataclass
class Finding:
    file: str
    location: str
    operation: str
    external_system: str | None
    cited_types: list[str]
    detail: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    unparsed: list[dict] = field(default_factory=list)
    files_scanned: int = 0
    operations_total: int = 0
    external_declared: int = 0
    marked_unresolved: int = 0
    sourced: int = 0

    @property
    def declaration_gap(self) -> bool:
        """Operations exist and not one is declared external.

        Reported, never a finding: an epic with no third-party integration is a
        normal state, and this gate cannot tell the two apart. Printing it is
        what stops a zero from reading as proof.
        """
        return self.operations_total > 0 and self.external_declared == 0

    @property
    def applicability(self) -> str:
        """`applicable` requires that this gate actually had something of its
        OWN kind to check -- a declared external operation.

        An epic with 47 operations and none declared external is
        `inapplicable`, never `pass`: zero of zero verified is not evidence of
        anything, and reporting it as a pass is the fail-open shape
        (chief-wiggum#289) this repo keeps paying for. Caught on the #350
        dry-run, where 15 of 22 real epics -- including a Stripe billing
        epic -- reported `pass` while checking nothing.
        """
        if self.unparsed:
            return "error"
        # Both of the next two are "nothing of this gate's own kind was
        # checked". The first is strictly subsumed by the second (no file means
        # no declared external operation) and is kept because it states the
        # primary case plainly; mutating it away changes no behaviour.
        if not self.files_scanned:
            return "inapplicable"
        # Zero declared external operations subsumes zero operations. The
        # renderer still tells those two apart, because "no operations at all"
        # and "operations, none of them external" mean different things to the
        # human reading the report.
        if not self.external_declared:
            return "inapplicable"
        return "applicable"

    @property
    def outcome(self) -> str:
        if self.unparsed:
            return "error"
        if self.applicability == "inapplicable":
            return "inapplicable"
        return "findings" if self.findings else "pass"

    @property
    def measured(self) -> dict:
        return {
            "files_scanned": self.files_scanned,
            "files_unparsed": len(self.unparsed),
            "operations_total": self.operations_total,
            "external_declared": self.external_declared,
            "external_sourced": self.sourced,
            "external_marked_unresolved": self.marked_unresolved,
            "external_unsourced": len(self.findings),
            "declaration_gap": self.declaration_gap,
        }


def _strings(node) -> list[str]:
    """Every string anywhere under a node, so a marker is found wherever the
    author put it -- the description, a precondition, an error case."""
    out: list[str] = []
    if isinstance(node, dict):
        for value in node.values():
            out.extend(_strings(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(_strings(item))
    elif isinstance(node, str):
        out.append(node)
    return out


def cited_source_types(operation: dict) -> list[str]:
    types: list[str] = []
    for prov in operation.get("derived_from") or []:
        if isinstance(prov, dict):
            kind = prov.get("type")
            if isinstance(kind, str):
                types.append(kind)
    return types


def is_external(operation: dict) -> bool:
    return operation.get("external") is True


def scan_contracts(path: Path, report: Report) -> None:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        report.unparsed.append({"file": str(path), "reason": f"{type(exc).__name__}: {exc}"})
        return
    report.files_scanned += 1

    entities = data.get("entities") if isinstance(data, dict) else None
    if not isinstance(entities, list):
        entities = []

    for e_idx, entity in enumerate(entities):
        if not isinstance(entity, dict):
            continue
        entity_name = entity.get("name") or f"entities[{e_idx}]"
        operations = entity.get("operations")
        if not isinstance(operations, list):
            continue
        for o_idx, operation in enumerate(operations):
            if not isinstance(operation, dict):
                continue
            report.operations_total += 1
            if not is_external(operation):
                continue
            report.external_declared += 1

            location = f"entities[{e_idx}].operations[{o_idx}]"
            name = operation.get("name") or f"{operation.get('method', '?')} {operation.get('path', '?')}"
            label = f"{entity_name}.{name}"

            types = cited_source_types(operation)
            if VERIFIED_SOURCE_TYPES.intersection(types):
                report.sourced += 1
                continue

            if any(MARKER_RE.search(text) for text in _strings(operation)):
                # Already gating through check_unresolved.py. Counting it here
                # too would report one unknown twice and make the denominators
                # disagree with each other.
                report.marked_unresolved += 1
                continue

            cited = ", ".join(sorted(set(types))) if types else "nothing"
            report.findings.append(Finding(
                file=str(path),
                location=location,
                operation=label,
                external_system=operation.get("external_system"),
                cited_types=types,
                detail=(
                    f"declared external and cites {cited}; needs a derived_from entry of "
                    f"type {' or '.join(sorted(VERIFIED_SOURCE_TYPES))} (a captured real "
                    f"response, or the vendor's published contract), or a TBD:/UNRESOLVED: "
                    f"marker so dependent work is gated"
                ),
            ))


def collect_files(targets: list[Path]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        if target.is_dir():
            files.extend(sorted(
                p for p in target.rglob("*") if p.name in CONTRACT_FILENAMES
            ))
        elif target.name in CONTRACT_FILENAMES:
            files.append(target)
    return files


def scan(targets: list[Path]) -> Report:
    report = Report()
    for path in collect_files(targets):
        scan_contracts(path, report)
    return report


def _render_text(report: Report, gating: bool) -> None:
    m = report.measured
    print(
        f"Measured: {m['files_scanned']} contracts file(s), "
        f"{m['operations_total']} operation(s), "
        f"{m['external_declared']} declared external"
        + (f", {m['files_unparsed']} unreadable" if m["files_unparsed"] else "")
    )

    if report.unparsed:
        print("\nERROR: contracts artifact(s) present that could NOT be read — their "
              "external interfaces are unknown, so this is not a clean result "
              "(chief-wiggum#289):\n")
        for u in report.unparsed:
            print(f"  {u['file']}: {u['reason']}")

    if report.declaration_gap:
        print(f"\nINAPPLICABLE (declaration gap): {m['operations_total']} operation(s) "
              f"declared, none marked \"external\": true — so this gate checked nothing.\n"
              f"  That is correct for an epic that integrates no third-party system, and "
              f"a blind spot for one that does.\n"
              f"  `external` is optional by design; a human confirms which it is. Zero of "
              f"zero verified is not a pass.")
        return

    if report.outcome == "inapplicable":
        if not m["files_scanned"]:
            print("INAPPLICABLE: no contracts.json under the given target(s); nothing "
                  "was checked (not a pass)")
        else:
            print("INAPPLICABLE: contracts.json declares no operations; nothing was "
                  "checked (not a pass)")
        return

    if report.findings:
        print(f"\nUNSOURCED EXTERNAL INTERFACES: {len(report.findings)} of "
              f"{m['external_declared']} declared\n")
        for f in report.findings:
            system = f" [{f.external_system}]" if f.external_system else ""
            print(f"  {f.file} ({f.location}){system}")
            print(f"    {f.operation}: {f.detail}")
    elif m["external_declared"]:
        print(f"\nOK: all {m['external_declared']} declared external interface(s) cite a "
              f"verified source or carry an unresolved marker "
              f"({m['external_sourced']} sourced, {m['external_marked_unresolved']} marked)")

    if report.findings and not gating:
        print("\n(report-only: exiting 0. Pass --gate to block on these — see "
              "docs/gate-rollout.md)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that declared external interfaces cite a verified source (chief-wiggum#350)"
    )
    parser.add_argument("targets", nargs="+", help="Epic directory or contracts.json file(s)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument(
        "--gate", action="store_true",
        help="Block (exit 1) on unsourced external interfaces. Report-only without it.",
    )
    args = parser.parse_args(argv)

    targets = [Path(t) for t in args.targets]
    missing = [t for t in targets if not t.exists()]
    if missing:
        for t in missing:
            print(f"ERROR: {t} does not exist", file=sys.stderr)
        return EXIT_USAGE

    report = scan(targets)

    if args.format == "json":
        print(json.dumps({
            "findings": [asdict(f) for f in report.findings],
            "count": len(report.findings),
            "applicability": report.applicability,
            "outcome": report.outcome,
            "gating": args.gate,
            "measured": report.measured,
            "unparsed": report.unparsed,
        }, indent=2))
    else:
        _render_text(report, args.gate)

    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from factory_log import emit_gate
        repo = os.path.basename(os.path.abspath(str(targets[0]))) if targets else None
        emit_gate("check_interface_provenance", report.outcome,
                  caught=len(report.findings), repo=repo)
    except Exception:
        pass

    if report.unparsed:
        # A broken instrument is non-zero even report-only: report-only means
        # "findings do not block", never "the scanner may fail silently".
        return EXIT_ERROR
    if report.findings and args.gate:
        return EXIT_FINDINGS
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
