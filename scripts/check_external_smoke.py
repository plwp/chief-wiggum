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
from chief_wiggum.hashing import scanner_version  # noqa: E402

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
    parser.add_argument("targets", nargs="*",
                        help="Epic directory or contracts.json file(s); "
                             "not required with --scanner-version")
    parser.add_argument("--source", help="Repo root to scan for @cw-smoke sites")
    parser.add_argument("--results", help="junit-xml results, to verify the smoke actually ran")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--gate", action="store_true",
                        help="Block (exit 1) on findings. Report-only without it.")
    parser.add_argument(
        "--scanner-version", action="store_true",
        help="Print the hash-derived scanner version and exit. The "
             "gate-validation protocol probes this to detect a record that has "
             "gone stale against the code it certifies (INV-fh-005).",
    )
    args = parser.parse_args(argv)

    if args.scanner_version:
        print(_scanner_version())
        return EXIT_OK

    if not args.targets or not args.source:
        parser.error("the following arguments are required: targets, --source")

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


# --- gate-validation replay (docs/gate-validation.md, chief-wiggum#410) -------
#
# The seeded trials this gate's validation record cites, as executable
# mutations of a pinned fixture corpus. `gate_validation_designer.py
# revalidate check_external_smoke` re-runs every one of them, so a record can
# never drift into asserting a trial nobody has executed since.

GV_CORPUS = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / \
    "gate_validation" / "external_smoke_clean"

_SKIPPED_CASE = ('<testcase classname="internal/scp" name="TestSCPLiveVenueInfo" '
                 'file="internal/scp/live_smoke_test.go"/>')


def _gv_paths(corpus: Path) -> tuple[Path, Path, Path]:
    return corpus / "epic", corpus / "src", corpus / "results" / "junit.xml"


def _seed_direct(corpus: Path) -> None:
    """THE defect: the smoke was SKIPPED — credentials absent, build tag off —
    and a suite that collapses skipped into "not failed" reads green."""
    results = corpus / "results" / "junit.xml"
    text = results.read_text()
    assert _SKIPPED_CASE in text
    results.write_text(text.replace(
        _SKIPPED_CASE,
        _SKIPPED_CASE.replace("/>", "><skipped/></testcase>")))


def _seed_omission(corpus: Path) -> None:
    """Evasion by omission at THIS gate's layer: the @cw-smoke annotation is
    deleted while the system stays declared external. Nothing then claims to
    exercise SCP at all."""
    src = corpus / "src" / "internal" / "scp" / "live_smoke_test.go"
    text = src.read_text()
    assert "@cw-smoke" in text
    src.write_text("\n".join(
        line for line in text.split("\n") if "@cw-smoke" not in line))


def _seed_config_indirection(corpus: Path) -> None:
    """Disabled by CONFIG, not by editing the assertion: the smoke stays
    annotated and fully intact in the tree, and a build tag keeps it out of the
    run entirely, so it never appears in the results."""
    results = corpus / "results" / "junit.xml"
    text = results.read_text()
    assert _SKIPPED_CASE in text
    results.write_text(text.replace(_SKIPPED_CASE, ""))


def _seed_sampling_gap(corpus: Path) -> None:
    """A passing sibling must not vouch for a skipped one. A second smoke is
    added for the SAME system and skipped; the original still passes, so a gate
    that samples any-one-passes reports clean."""
    src = corpus / "src" / "internal" / "scp" / "second_smoke_test.go"
    src.write_text(
        "package scp\n\nimport \"testing\"\n\n"
        "// @cw-smoke SCP case=TestSCPLiveBookings\n"
        "func TestSCPLiveBookings(t *testing.T) {}\n")
    results = corpus / "results" / "junit.xml"
    text = results.read_text()
    results.write_text(text.replace(
        "</testsuite>",
        '  <testcase classname="internal/scp" name="TestSCPLiveBookings" '
        'file="internal/scp/second_smoke_test.go"><skipped/></testcase>\n</testsuite>'))


def _seed_instrument_broken(corpus: Path) -> None:
    """The instrument itself breaks: the results file is present and
    unparseable. Its verdicts are UNKNOWN, which is never a pass — the #289
    broken-instrument class."""
    results = corpus / "results" / "junit.xml"
    results.write_text("<testsuite><testcase name=\"truncated\"")


def _seed_boundary_unrelated_skip(corpus: Path) -> None:
    """A NO-FIRE trial proving the stated boundary: this gate speaks only about
    DECLARED external systems. An unrelated skipped test, and a non-external
    operation with no smoke, must not produce a finding."""
    results = corpus / "results" / "junit.xml"
    text = results.read_text()
    results.write_text(text.replace(
        "</testsuite>",
        '  <testcase classname="internal/api" name="TestBookingsHandler" '
        'file="internal/api/bookings_test.go"><skipped/></testcase>\n</testsuite>'))


SEED_EXECUTORS = {
    "es-direct-01": _seed_direct,
    "es-omission-01": _seed_omission,
    "es-config-indirection-01": _seed_config_indirection,
    "es-sampling-gap-01": _seed_sampling_gap,
    "es-instrument-broken-01": _seed_instrument_broken,
    "es-sampling-gap-02": _seed_boundary_unrelated_skip,
}


def _gv_outcome(corpus: Path) -> str:
    """fired / not-fired for a prepared corpus.

    ``error`` counts as FIRED (#289): a harness that only accepted `findings`
    would score a broken instrument as a clean run, which is the precise
    conflation this gate exists to prevent.
    """
    epic, src, results = _gv_paths(corpus)
    report = check([epic], src, results if results.is_file() else None)
    return "fired" if (report.findings or report.unparsed) else "not-fired"


def replay_seeded_trial(seed: dict) -> str:
    """Re-run one seeded trial against a throwaway copy of the pinned corpus."""
    import shutil
    import tempfile

    seed_id = str(seed.get("seed_id", ""))
    if seed_id not in SEED_EXECUTORS:
        raise KeyError(f"no seed executor for {seed_id!r}")
    with tempfile.TemporaryDirectory() as tmp:
        corpus = Path(tmp) / "corpus"
        shutil.copytree(GV_CORPUS, corpus)
        SEED_EXECUTORS[seed_id](corpus)
        return _gv_outcome(corpus)


def replay_clean_corpus() -> dict:
    """Re-run the clean corpus, deriving coverage from the live run."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        corpus = Path(tmp) / "corpus"
        shutil.copytree(GV_CORPUS, corpus)
        epic, src, results = _gv_paths(corpus)
        report = check([epic], src, results)
    return {
        "repo": "tests/fixtures/gate_validation/external_smoke_clean",
        "findings": len(report.findings),
        "coverage": {
            "external_operations": report.measured["external_operations"],
            "systems_declared": report.measured["systems_declared"],
            "source_files_scanned": report.measured["source_files_scanned"],
            "results_cases": report.measured["results_cases"],
        },
        # `applicable` matters as much as the zero: a clean corpus that the gate
        # found INAPPLICABLE exercised nothing, and would certify the checker on
        # the strength of a run that never looked at anything.
        "passed": not report.findings and report.applicability == "applicable",
    }


def _scanner_version() -> str:
    """Hash-derived ``scanner_version``: this module plus its finding-affecting
    dependencies. ``annotations.py`` carries the @cw-smoke grammar, so a change
    there changes which sites this gate sees. No hand-bumped constant to forget
    (INV-fh-005)."""
    here = Path(__file__).resolve()
    cw_dir = here.parent / "chief_wiggum"
    return scanner_version(here, cw_dir / "annotations.py")


if __name__ == "__main__":
    sys.exit(main())
