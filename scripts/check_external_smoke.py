#!/usr/bin/env python3
"""Every external integration needs one real round-trip, and a skip must be loud.

chief-wiggum#353. A live smoke test existed for the engine but was OPTIONAL --
behind a build tag and ``LIVE_SMOKE=1`` -- and only asserted ``Connect``, not a
real turn. Four separate production bugs each needed exactly ONE real
end-to-end interaction to surface, and none was required to pass:

  * the native-audio turn-flow bug
  * the missing-system-prompt bug (the agent never called its tools)
  * the trailing-newline token bug -- the fakes used clean tokens; the real
    Secret-Manager-mounted value had a trailing ``\\n``, so ``Bearer <token>\\n``
    was an invalid HTTP header and every call failed with an opaque transport
    error
  * the guessed-route bug (chief-wiggum#350)

Unit tests were green throughout. They were green because they never touched
the real system.

Two halves, and the second is the point
---------------------------------------
**Static** -- every declared external system has an ``@cw-smoke <system>``
annotated test. This is the same shape as ``check_instrumentation.py``'s
``@cw-emits`` check and proves the same limited thing: a smoke EXISTS. It does
not prove it ran.

**Result-aware** (``--results <junit.xml>``) -- the smoke actually RAN and
PASSED. This is what #353 asks for, and it is why this checker cannot reuse
``ratchet.parse_junit_xml``: that function returns only passing case ids and
collapses ``skipped`` in with ``failure``/``error``. Collapsing those is
exactly the conflation this gate exists to prevent. A smoke that was skipped
because credentials were absent is **unverified** -- a visible gap in the epic
report -- and is never the same state as a smoke that ran and passed, nor the
same as one that ran and failed.

The five states a declared system can be in:

  verified    the smoke ran and passed -- the only state that is evidence
  failed      the smoke ran and failed
  unverified  the smoke was SKIPPED (creds absent, build tag off). LOUD.
  never_ran   no case in the results matched the annotation at all
  no_smoke    no ``@cw-smoke`` site anywhere in the source tree

Without ``--results`` the checker reports the static half only, and says so:
every system with an annotation is ``smoke_declared``, which is explicitly NOT
``verified``. A gate that let "an annotation exists" read as "the integration
works" would be the same vacuous pass in a new coat.

Annotation grammar (``chief_wiggum.annotations.SMOKE_TAG_RE``)
-------------------------------------------------------------
::

    @cw-smoke <system-name> [case=<substring>]

``<system-name>`` matches the ``external_system`` declared on a
``"external": true`` operation in ``contracts.json`` (chief-wiggum#350),
case-insensitively. Place it at the test that performs one real round-trip::

    // @cw-smoke SCP case=TestSCPLiveVenueInfo
    func TestSCPLiveVenueInfo(t *testing.T) { ... }

``case=`` names the test so results matching is exact. Without it the checker
falls back to matching the junit case's ``file``/``classname`` against the
annotated file -- which matches EVERY case in that file, so an unrelated
passing test would otherwise award the integration a pass it never earned.
An unpinned annotation therefore cannot reach ``verified``: its best outcome
is ``smoke_declared``, with a note to pin it. It CAN still reach ``unverified``
or ``failed``, because a skip or failure in the smoke's own file is worth
knowing even when the annotation is imprecise. Weak evidence may raise an
alarm; it may never grant one.

Gate status: REPORT-ONLY per ``docs/gate-rollout.md``. ``--gate`` exits 1 on
findings; no workflow passes it until a passing
``docs/quality/validation/check_external_smoke.json`` record exists.

CLI::

    python3 scripts/check_external_smoke.py <epic-dir> --source <repo-root>
        [--results <junit.xml>] [--format text|json] [--gate]

Exit codes: 0 ok / report-only / inapplicable, 1 findings under --gate,
2 usage error, 3 a required input was present and unreadable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.annotations import SMOKE_TAG_RE  # noqa: E402

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_ERROR = 3

SCANNED_SUFFIXES = {
    ".py", ".go", ".js", ".jsx", ".ts", ".tsx", ".java", ".rb", ".rs",
    ".cs", ".kt", ".swift", ".php", ".sh",
}
SKIP_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", "__pycache__",
    ".venv", "venv", "target", ".next", ".pytest_cache",
}

# The only state that is evidence the integration works.
VERIFIED = "verified"
FAILED = "failed"
UNVERIFIED = "unverified"
NEVER_RAN = "never_ran"
NO_SMOKE = "no_smoke"
SMOKE_DECLARED = "smoke_declared"  # static-only run; NOT verified

BLOCKING_STATES = {NO_SMOKE, UNVERIFIED, NEVER_RAN, FAILED}


@dataclass
class SmokeSite:
    system: str
    file: str
    line: int
    case: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SystemReport:
    system: str
    state: str
    detail: str
    sites: list[dict] = field(default_factory=list)
    matched_cases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    systems: list[SystemReport] = field(default_factory=list)
    unparsed: list[dict] = field(default_factory=list)
    contracts_scanned: int = 0
    external_operations: int = 0
    source_files_scanned: int = 0
    results_cases: int | None = None

    @property
    def findings(self) -> list[SystemReport]:
        return [s for s in self.systems if s.state in BLOCKING_STATES]

    @property
    def applicability(self) -> str:
        if self.unparsed:
            return "error"
        if not self.systems:
            return "inapplicable"
        return "applicable"

    @property
    def outcome(self) -> str:
        if self.unparsed:
            return "error"
        if not self.systems:
            return "inapplicable"
        return "findings" if self.findings else "pass"

    @property
    def measured(self) -> dict:
        counts: dict[str, int] = {}
        for s in self.systems:
            counts[s.state] = counts.get(s.state, 0) + 1
        return {
            "contracts_scanned": self.contracts_scanned,
            "external_operations": self.external_operations,
            "systems_declared": len(self.systems),
            "source_files_scanned": self.source_files_scanned,
            "results_cases": self.results_cases,
            "results_supplied": self.results_cases is not None,
            "by_state": counts,
            "files_unparsed": len(self.unparsed),
        }


# --- inventory: which external systems did the epic declare? -----------------

def collect_systems(epic_targets: list[Path], report: Report) -> dict[str, int]:
    """Distinct ``external_system`` names on ``"external": true`` operations.

    Reuses chief-wiggum#350's declaration rather than inventing a second
    inventory: an integration this gate does not know about is one #350's
    declaration gap already reports.
    """
    systems: dict[str, int] = {}
    for path in _contracts_files(epic_targets):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            report.unparsed.append({"file": str(path), "reason": f"{type(exc).__name__}: {exc}"})
            continue
        report.contracts_scanned += 1
        entities = data.get("entities") if isinstance(data, dict) else None
        if not isinstance(entities, list):
            continue
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            operations = entity.get("operations")
            if not isinstance(operations, list):
                continue
            for operation in operations:
                if not isinstance(operation, dict) or operation.get("external") is not True:
                    continue
                report.external_operations += 1
                name = operation.get("external_system")
                if isinstance(name, str) and name.strip():
                    key = name.strip()
                    systems[key] = systems.get(key, 0) + 1
    return systems


def _contracts_files(targets: list[Path]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        if target.is_dir():
            files.extend(sorted(p for p in target.rglob("contracts.json")))
        elif target.name == "contracts.json":
            files.append(target)
    return files


# --- static half: @cw-smoke sites --------------------------------------------

def scan_smoke_sites(source_root: Path, report: Report) -> list[SmokeSite]:
    sites: list[SmokeSite] = []
    if not source_root.is_dir():
        return sites
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        report.source_files_scanned += 1
        if "@cw-smoke" not in text:
            continue
        for lineno, line in enumerate(text.split("\n"), 1):
            for match in SMOKE_TAG_RE.finditer(line):
                sites.append(SmokeSite(
                    system=match.group("system"),
                    file=str(path.relative_to(source_root)),
                    line=lineno,
                    case=match.group("case"),
                ))
    return sites


# --- result-aware half: did it actually run? ---------------------------------

@dataclass
class Case:
    identifier: str
    file: str
    classname: str
    state: str  # passed | failed | skipped


def parse_junit(xml_text: str) -> list[Case]:
    """Three states kept DISTINCT.

    ``ratchet.parse_junit_xml`` returns passing case ids only and drops
    skipped in with failed. For this gate that collapse is the bug: a smoke
    skipped for absent credentials must be reportable as `unverified`, which
    is neither a pass nor a failure.
    """
    root = ET.fromstring(xml_text)
    cases: list[Case] = []
    for case in root.iter("testcase"):
        tags = {child.tag for child in case}
        if "skipped" in tags:
            state = "skipped"
        elif tags & {"failure", "error"}:
            state = "failed"
        else:
            state = "passed"
        classname = case.get("classname") or ""
        file_attr = case.get("file") or ""
        name = case.get("name", "")
        cases.append(Case(
            identifier=f"{classname}::{name}" if classname else name,
            file=file_attr,
            classname=classname,
            state=state,
        ))
    return cases


def _matches(site: SmokeSite, case: Case) -> bool:
    if site.case:
        return site.case in case.identifier or site.case in case.file
    # Weaker fallback: same file, or a classname that looks like the module.
    stem = Path(site.file).stem
    if case.file and (case.file.endswith(site.file) or site.file.endswith(case.file)):
        return True
    return bool(stem) and stem in case.classname


def _worst(states: set[str]) -> str:
    """A system is only as verified as its weakest smoke.

    Order matters: one passing smoke must never mask a sibling that was
    skipped. That masking is the same silent-green this gate exists to stop.
    """
    for state in ("failed", "skipped", "passed"):
        if state in states:
            return {"failed": FAILED, "skipped": UNVERIFIED, "passed": VERIFIED}[state]
    return NEVER_RAN


def evaluate(systems: dict[str, int], sites: list[SmokeSite],
             cases: list[Case] | None) -> list[SystemReport]:
    reports: list[SystemReport] = []
    for system in sorted(systems):
        owned = [s for s in sites if s.system.lower() == system.lower()]
        if not owned:
            reports.append(SystemReport(
                system=system,
                state=NO_SMOKE,
                detail=(f"declared on {systems[system]} external operation(s) and no "
                        f"`@cw-smoke {system}` site exists anywhere in the source tree — "
                        f"nothing performs one real round-trip against it"),
            ))
            continue

        site_dicts = [s.to_dict() for s in owned]
        if cases is None:
            reports.append(SystemReport(
                system=system,
                state=SMOKE_DECLARED,
                detail=("a smoke is annotated but no results were supplied, so it is "
                        "unknown whether it ran — pass --results to verify"),
                sites=site_dicts,
            ))
            continue

        pinned = all(s.case for s in owned)
        matched = [c for c in cases if any(_matches(s, c) for s in owned)]
        if not matched:
            reports.append(SystemReport(
                system=system,
                state=NEVER_RAN,
                detail=("annotated, but no case in the supplied results matched it"
                        + ("" if pinned else " — add `case=<test name>` to the annotation "
                                             "so matching is exact")),
                sites=site_dicts,
            ))
            continue

        state = _worst({c.state for c in matched})

        # A verdict of VERIFIED must rest on a PINNED match. Without `case=`,
        # matching falls back to the annotated FILE, which matches every case
        # in that file -- so an unrelated passing test in the same file would
        # award the integration a pass it never earned. Weak evidence may raise
        # an alarm (a skip or failure in the smoke's own file is worth knowing)
        # but it may never grant one. Fail-closed, in the direction #353 cares
        # about: never silently green.
        if state is VERIFIED and not pinned:
            reports.append(SystemReport(
                system=system,
                state=SMOKE_DECLARED,
                detail=("cases in the annotated file passed, but the annotation is not "
                        "pinned, so it cannot be shown that the SMOKE is what passed — "
                        "add `case=<test name>`. Not verified"),
                sites=site_dicts,
                matched_cases=[c.identifier for c in matched],
            ))
            continue

        detail = {
            VERIFIED: "one real round-trip ran and passed",
            FAILED: "the smoke ran and FAILED",
            UNVERIFIED: ("the smoke was SKIPPED — credentials absent or the build tag off. "
                         "This integration is UNVERIFIED, which is not a pass"),
        }[state]
        reports.append(SystemReport(
            system=system, state=state, detail=detail, sites=site_dicts,
            matched_cases=[c.identifier for c in matched],
        ))
    return reports


def check(epic_targets: list[Path], source_root: Path,
          results_path: Path | None = None) -> Report:
    report = Report()
    systems = collect_systems(epic_targets, report)
    sites = scan_smoke_sites(source_root, report)

    cases: list[Case] | None = None
    if results_path is not None:
        try:
            cases = parse_junit(results_path.read_text())
        except (OSError, UnicodeDecodeError, ET.ParseError) as exc:
            report.unparsed.append({
                "file": str(results_path),
                "reason": f"{type(exc).__name__}: {exc}",
            })
            cases = None
        else:
            report.results_cases = len(cases)

    report.systems = evaluate(systems, sites, cases)
    return report


# --- rendering ---------------------------------------------------------------

def render_text(report: Report, gating: bool) -> str:
    m = report.measured
    lines = [
        f"Measured: {m['contracts_scanned']} contracts file(s), "
        f"{m['external_operations']} external operation(s), "
        f"{m['systems_declared']} distinct system(s), "
        f"{m['source_files_scanned']} source file(s) scanned"
        + (f", {m['results_cases']} result case(s)" if m["results_supplied"]
           else ", NO results supplied")
    ]

    if report.unparsed:
        lines += ["", "ERROR: input(s) present that could not be read — this is not a "
                      "clean result (chief-wiggum#289):"]
        lines += [f"  {u['file']}: {u['reason']}" for u in report.unparsed]

    if report.outcome == "inapplicable":
        lines += ["", "INAPPLICABLE: no external system is declared "
                      "(`\"external\": true` with an `external_system` name) under the "
                      "given epic — nothing was checked, which is not a pass. If this "
                      "epic calls a third-party system, see chief-wiggum#350's "
                      "declaration gap."]
        return "\n".join(lines)

    if not m["results_supplied"]:
        lines += ["", "STATIC ONLY: no --results supplied. An annotation proves a smoke "
                      "EXISTS, never that it ran. `smoke_declared` is not `verified`."]

    for state, label in (
        (NO_SMOKE, "NO SMOKE — nothing performs a real round-trip"),
        (UNVERIFIED, "UNVERIFIED — the smoke was SKIPPED"),
        (NEVER_RAN, "NEVER RAN — annotated, no matching case in the results"),
        (FAILED, "FAILED — the smoke ran and failed"),
        (SMOKE_DECLARED, "SMOKE DECLARED — existence only, not verified"),
        (VERIFIED, "VERIFIED — one real round-trip ran and passed"),
    ):
        group = [s for s in report.systems if s.state == state]
        if not group:
            continue
        lines += ["", f"## {label} ({len(group)})"]
        for s in group:
            lines.append(f"  {s.system}: {s.detail}")
            for site in s.sites:
                pin = f" case={site['case']}" if site["case"] else ""
                lines.append(f"      {site['file']}:{site['line']}{pin}")

    if report.findings and not gating:
        lines += ["", "(report-only: exiting 0. Pass --gate to block — see "
                      "docs/gate-rollout.md)"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Every declared external system needs one real round-trip (chief-wiggum#353)"
    )
    parser.add_argument("targets", nargs="+", help="Epic directory or contracts.json file(s)")
    parser.add_argument("--source", required=True, help="Repo root to scan for @cw-smoke sites")
    parser.add_argument("--results", help="junit-xml results, to verify the smoke actually ran")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--gate", action="store_true",
                        help="Block (exit 1) on findings. Report-only without it.")
    args = parser.parse_args(argv)

    targets = [Path(t) for t in args.targets]
    missing = [t for t in targets if not t.exists()]
    source = Path(args.source)
    if not source.exists():
        missing.append(source)
    results = Path(args.results) if args.results else None
    if results is not None and not results.exists():
        missing.append(results)
    if missing:
        for t in missing:
            print(f"ERROR: {t} does not exist", file=sys.stderr)
        return EXIT_USAGE

    report = check(targets, source, results)

    if args.format == "json":
        print(json.dumps({
            "systems": [s.to_dict() for s in report.systems],
            "findings": [s.to_dict() for s in report.findings],
            "count": len(report.findings),
            "applicability": report.applicability,
            "outcome": report.outcome,
            "gating": args.gate,
            "measured": report.measured,
            "unparsed": report.unparsed,
        }, indent=2))
    else:
        print(render_text(report, args.gate))

    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from factory_log import emit_gate
        emit_gate("check_external_smoke", report.outcome, caught=len(report.findings))
    except Exception:
        pass

    if report.unparsed:
        return EXIT_ERROR
    if report.findings and args.gate:
        return EXIT_FINDINGS
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
