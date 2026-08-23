#!/usr/bin/env python3
"""A test double for an external system must be recorded, not invented.

chief-wiggum#351 -- mock-model collusion, and the root cause the other three
gates in this family are symptoms of.

``harness/scp_fixture.go`` routed requests by ``TrimPrefix(path,
"/api/gx-agent/")`` and looked up scripts by tool NAME -- **the exact same
wrong assumption** as ``internal/scp/client.go``'s endpoint builder. Client and
fixture agreed. Every test was green. The route bug stayed invisible until a
real call.

Same shape in the engine: ``FakeVoiceEngine`` emitted turn events in the
cascade order the state machine expected. The real native-audio API never emits
those, so the session sat in ``listening`` dropping model output -- found only
on a real audio turn.

When one worker authors the code and its double from a single assumption, the
test validates the assumption rather than reality. TDD-with-fakes makes the
fake the spec, and a green suite then measures nothing but the author's
self-consistency.

The rule
--------
A test double standing in for a declared external system (``"external": true``
in ``contracts.json``, chief-wiggum#350) must be DERIVED from at least one real
captured interaction, and must say where that capture lives::

    // @cw-fixture SCP capture=testdata/captures/scp-venue-info.json
    func newSCPFixture(t *testing.T) *httptest.Server { ... }

and the capture must itself carry provenance -- when it was taken and from
what::

    {
      "captured_at": "2026-08-23T04:11:00Z",
      "source": "https://scp.example.com/venue-info (staging, curl)",
      "response": { ... }
    }

Per-system states
-----------------
  recorded            a fixture citing a capture that exists and is attributed
  hand_authored       a fixture with NO capture= -- invented, so it can agree
                      with the code and both be wrong. THE finding
  missing_capture     a capture is cited and the file is not there
  unattributed        the capture exists and records neither when nor whence.
                      An empty file must not be able to satisfy this gate
  no_fixture          no double for this system at all -- reported, never a
                      finding: not every external system needs one, and
                      inventing a requirement here would be noise

What this cannot do
-------------------
It cannot tell whether the capture is FAITHFUL -- a recorded response can still
be hand-edited afterwards, and nothing here re-runs it. That is what
``check_external_smoke.py`` (#353) is for: one real round-trip, whose SKIP is
loud. This gate and that one are complementary, not redundant -- this asks
"where did the double come from", that asks "does the real thing still answer
that way".

Gate status: REPORT-ONLY per ``docs/gate-rollout.md``.

CLI::

    python3 scripts/check_fixture_provenance.py <epic-dir> --source <repo-root>
        [--format text|json] [--gate]

Exit codes: 0 ok / report-only / inapplicable, 1 findings under --gate,
2 usage error, 3 an input was present and unreadable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.annotations import FIXTURE_TAG_RE  # noqa: E402

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_ERROR = 3

SCANNED_SUFFIXES = {
    ".py", ".go", ".js", ".jsx", ".ts", ".tsx", ".java", ".rb", ".rs",
    ".cs", ".kt", ".swift", ".php",
}
SKIP_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", "__pycache__",
    ".venv", "venv", "target", ".next", ".pytest_cache",
}

RECORDED = "recorded"
HAND_AUTHORED = "hand_authored"
MISSING_CAPTURE = "missing_capture"
UNATTRIBUTED = "unattributed"
NO_FIXTURE = "no_fixture"

BLOCKING_STATES = {HAND_AUTHORED, MISSING_CAPTURE, UNATTRIBUTED}

# A capture with neither of these records nothing about where it came from, so
# an empty file could otherwise satisfy the gate.
PROVENANCE_KEYS = ("captured_at", "source")


@dataclass
class FixtureSite:
    system: str
    file: str
    line: int
    capture: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SystemReport:
    system: str
    state: str
    detail: str
    sites: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    systems: list[SystemReport] = field(default_factory=list)
    unparsed: list[dict] = field(default_factory=list)
    contracts_scanned: int = 0
    external_operations: int = 0
    source_files_scanned: int = 0

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
        by_state: dict[str, int] = {}
        for s in self.systems:
            by_state[s.state] = by_state.get(s.state, 0) + 1
        return {
            "contracts_scanned": self.contracts_scanned,
            "external_operations": self.external_operations,
            "systems_declared": len(self.systems),
            "source_files_scanned": self.source_files_scanned,
            "by_state": by_state,
            "files_unparsed": len(self.unparsed),
        }


def collect_systems(targets: list[Path], report: Report) -> dict[str, int]:
    systems: dict[str, int] = {}
    for path in _contracts_files(targets):
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
            files.extend(sorted(target.rglob("contracts.json")))
        elif target.name == "contracts.json":
            files.append(target)
    return files


def scan_fixture_sites(source_root: Path, report: Report) -> list[FixtureSite]:
    sites: list[FixtureSite] = []
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
        if "@cw-fixture" not in text:
            continue
        for lineno, line in enumerate(text.split("\n"), 1):
            for match in FIXTURE_TAG_RE.finditer(line):
                sites.append(FixtureSite(
                    system=match.group("system"),
                    file=str(path.relative_to(source_root)),
                    line=lineno,
                    capture=match.group("capture"),
                ))
    return sites


def capture_state(source_root: Path, capture: str) -> tuple[str, str]:
    """`recorded` only if the capture exists AND says where it came from."""
    path = source_root / capture
    if not path.is_file():
        return MISSING_CAPTURE, f"cites {capture}, which does not exist"
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # Not every capture is JSON — a raw body dump is a legitimate capture.
        # Non-empty content is the most this can honestly assert about it.
        return ((RECORDED, f"{capture} (non-JSON capture, present and non-empty)")
                if path.stat().st_size > 0
                else (UNATTRIBUTED, f"{capture} is empty — an empty file records nothing"))
    if not isinstance(data, dict) or not any(data.get(k) for k in PROVENANCE_KEYS):
        return (UNATTRIBUTED,
                f"{capture} records neither `captured_at` nor `source` — without them "
                f"an invented file satisfies this gate exactly as well as a real capture")
    return RECORDED, f"{capture}"


def _worst(states: list[str]) -> str:
    """A system is only as recorded as its least-sourced double. One recorded
    fixture must not vouch for a hand-authored sibling."""
    for state in (HAND_AUTHORED, MISSING_CAPTURE, UNATTRIBUTED, RECORDED):
        if state in states:
            return state
    return NO_FIXTURE


def evaluate(systems: dict[str, int], sites: list[FixtureSite],
             source_root: Path) -> list[SystemReport]:
    reports: list[SystemReport] = []
    for system in sorted(systems):
        owned = [s for s in sites if s.system.lower() == system.lower()]
        if not owned:
            reports.append(SystemReport(
                system, NO_FIXTURE,
                "no `@cw-fixture` double declared for this system — reported, not a "
                "finding: not every external system needs one"))
            continue

        states, details = [], []
        for site in owned:
            if not site.capture:
                states.append(HAND_AUTHORED)
                details.append(f"{site.file}:{site.line} cites no capture — invented, so "
                               f"it can agree with the code and both be wrong")
                continue
            state, detail = capture_state(source_root, site.capture)
            states.append(state)
            details.append(f"{site.file}:{site.line}: {detail}")

        reports.append(SystemReport(
            system, _worst(states), "; ".join(details),
            sites=[s.to_dict() for s in owned]))
    return reports


def check(targets: list[Path], source_root: Path) -> Report:
    report = Report()
    systems = collect_systems(targets, report)
    sites = scan_fixture_sites(source_root, report)
    report.systems = evaluate(systems, sites, source_root)
    return report


def render_text(report: Report, gating: bool) -> str:
    m = report.measured
    lines = [
        f"Measured: {m['contracts_scanned']} contracts file(s), "
        f"{m['external_operations']} external operation(s), "
        f"{m['systems_declared']} system(s), "
        f"{m['source_files_scanned']} source file(s) scanned"
    ]
    if report.unparsed:
        lines += ["", "ERROR: input(s) present that could not be read (chief-wiggum#289):"]
        lines += [f"  {u['file']}: {u['reason']}" for u in report.unparsed]

    if report.outcome == "inapplicable":
        lines += ["", "INAPPLICABLE: no external system declared under the given epic; "
                      "nothing was checked (not a pass). See chief-wiggum#350's "
                      "declaration gap."]
        return "\n".join(lines)

    for state, label in (
        (HAND_AUTHORED, "HAND-AUTHORED — the double was invented, not recorded"),
        (MISSING_CAPTURE, "MISSING CAPTURE — cited and not present"),
        (UNATTRIBUTED, "UNATTRIBUTED — the capture records nothing about its origin"),
        (NO_FIXTURE, "NO FIXTURE (reported, not a finding)"),
        (RECORDED, "RECORDED — derived from a real captured interaction"),
    ):
        group = [s for s in report.systems if s.state == state]
        if not group:
            continue
        lines += ["", f"## {label} ({len(group)})"]
        for s in group:
            lines.append(f"  {s.system}: {s.detail}")

    if report.findings and not gating:
        lines += ["", "(report-only: exiting 0. Pass --gate to block — see "
                      "docs/gate-rollout.md)"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A double for an external system must be recorded, not invented (chief-wiggum#351)"
    )
    parser.add_argument("targets", nargs="+", help="Epic directory or contracts.json file(s)")
    parser.add_argument("--source", required=True, help="Repo root")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--gate", action="store_true",
                        help="Block (exit 1) on findings. Report-only without it.")
    args = parser.parse_args(argv)

    targets = [Path(t) for t in args.targets]
    missing = [t for t in targets if not t.exists()]
    source = Path(args.source)
    if not source.exists():
        missing.append(source)
    if missing:
        for t in missing:
            print(f"ERROR: {t} does not exist", file=sys.stderr)
        return EXIT_USAGE

    report = check(targets, source)

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
        emit_gate("check_fixture_provenance", report.outcome, caught=len(report.findings))
    except Exception:
        pass

    if report.unparsed:
        return EXIT_ERROR
    if report.findings and args.gate:
        return EXIT_FINDINGS
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
