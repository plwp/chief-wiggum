#!/usr/bin/env python3
"""Config that declares tools is not an agent that uses them.

chief-wiggum#354. The Gemini config was built with its tools declared, and the
engine tests asserted the tools were PASSED to the model (`toGenaiTools` and
friends) -- structurally correct, and green. But there was **no system
instruction**, so the model never called any tool and answered "I can't
retrieve that."

Nothing checked the BEHAVIOUR: given a tool-shaped question, does the agent
actually call the tool. Every test in the repo asked whether the config was
well-formed, which it was.

What this gate reads
--------------------
A **behavioural eval spec** the target declares -- by default
``docs/quality/behavioral-evals.json`` -- naming the agent's tools and a small
golden set of cases::

    {
      "tools": ["get_venue_info", "list_bookings", "get_date_info"],
      "cases": [
        {"id": "venue-hours", "prompt": "what time do you open?",
         "expect_tool": "get_venue_info", "expect_contains": "9am"},
        {"id": "bookings-today", "prompt": "any bookings today?",
         "expect_tool": "list_bookings"}
      ]
    }

and the RESULTS of running that set against the real model (``--results``),
either this gate's own JSON shape or junit-xml::

    {"results": [{"id": "venue-hours", "called_tools": ["get_venue_info"],
                  "output": "We open at 9am", "status": "ran"}]}

Running the golden set is the target's job -- it needs the target's model,
credentials and harness. Requiring it, and refusing to call an unrun set a
pass, is this gate's.

Per-case states
---------------
  verified          the case ran and the expected tool was called
  tool_not_called   it ran, and the expected tool was NOT called. THE finding:
                    this is precisely the missing-system-prompt bug
  wrong_answer      the tool was called and `expect_contains` was absent
  unverified        the case was SKIPPED (no credentials, eval suite off).
                    Loud, never a pass
  never_ran         declared in the spec, absent from the results

And per-tool: a declared tool with no case at all is ``no_case`` -- the
structural half of #354, config declaring what nothing exercises.

What it deliberately does NOT do
--------------------------------
#354 also notes that ``get_date_info``'s schema was well-typed but
*semantically circular*: it required an ``isoDate`` in order to tell you what
today's date is. That is a purpose-level contradiction, and the ticket is right
that schema validation cannot see it.

No lint for it ships here. The plausible heuristic -- a ``get_<X>`` tool whose
REQUIRED parameters include something matching ``<X>`` -- is exactly the kind
of prose-intent guess that was measured and rejected for chief-wiggum#350's
hedge lint, and there is no corpus of real tool schemas to measure its
precision against. Shipping it unmeasured would be guessing, which is the habit
this whole family of gates exists to break. Recorded in docs/behavioral-eval.md
as unmechanized rather than silently dropped.

Gate status: REPORT-ONLY per ``docs/gate-rollout.md``. ``--gate`` exits 1 on
findings; no workflow passes it until a passing
``docs/quality/validation/check_behavioral_eval.json`` record exists.

CLI::

    python3 scripts/check_behavioral_eval.py --source <repo-root>
        [--spec <path>] [--results <path>] [--format text|json] [--gate]

Exit codes: 0 ok / report-only / inapplicable, 1 findings under --gate,
2 usage error, 3 an input was present and unreadable.
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

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_ERROR = 3

DEFAULT_SPEC = Path("docs/quality/behavioral-evals.json")

VERIFIED = "verified"
TOOL_NOT_CALLED = "tool_not_called"
WRONG_ANSWER = "wrong_answer"
UNVERIFIED = "unverified"
NEVER_RAN = "never_ran"
NO_CASE = "no_case"

BLOCKING_STATES = {TOOL_NOT_CALLED, WRONG_ANSWER, UNVERIFIED, NEVER_RAN, NO_CASE}


@dataclass
class CaseReport:
    id: str
    expect_tool: str | None
    state: str
    detail: str
    called_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ToolReport:
    tool: str
    state: str
    detail: str
    case_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    cases: list[CaseReport] = field(default_factory=list)
    tools: list[ToolReport] = field(default_factory=list)
    unparsed: list[dict] = field(default_factory=list)
    spec_found: bool = False
    results_supplied: bool = False
    declared_tools: int = 0
    declared_cases: int = 0

    @property
    def findings(self) -> list:
        return ([c for c in self.cases if c.state in BLOCKING_STATES]
                + [t for t in self.tools if t.state in BLOCKING_STATES])

    @property
    def applicability(self) -> str:
        if self.unparsed:
            return "error"
        if not self.spec_found:
            return "inapplicable"
        return "applicable"

    @property
    def outcome(self) -> str:
        if self.unparsed:
            return "error"
        if not self.spec_found:
            return "inapplicable"
        return "findings" if self.findings else "pass"

    @property
    def measured(self) -> dict:
        by_state: dict[str, int] = {}
        for c in self.cases:
            by_state[c.state] = by_state.get(c.state, 0) + 1
        return {
            "spec_found": self.spec_found,
            "results_supplied": self.results_supplied,
            "declared_tools": self.declared_tools,
            "declared_cases": self.declared_cases,
            "cases_by_state": by_state,
            "tools_without_a_case": sum(1 for t in self.tools if t.state == NO_CASE),
            "files_unparsed": len(self.unparsed),
        }


def load_spec(path: Path, report: Report) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        report.unparsed.append({"file": str(path), "reason": f"{type(exc).__name__}: {exc}"})
        return {}
    report.spec_found = True
    return data if isinstance(data, dict) else {}


def load_results(path: Path, report: Report) -> dict[str, dict] | None:
    """Native JSON or junit-xml, keyed by case id.

    junit's three outcomes are kept distinct for the same reason
    ``check_external_smoke.py`` parses its own: a SKIPPED eval is `unverified`,
    which is neither a pass nor a failure.
    """
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        report.unparsed.append({"file": str(path), "reason": f"{type(exc).__name__}: {exc}"})
        return None

    stripped = text.lstrip()
    if stripped.startswith("<"):
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            report.unparsed.append({"file": str(path), "reason": f"ParseError: {exc}"})
            return None
        out: dict[str, dict] = {}
        for case in root.iter("testcase"):
            tags = {child.tag for child in case}
            status = ("skipped" if "skipped" in tags
                      else "failed" if tags & {"failure", "error"} else "ran")
            name = case.get("name", "")
            # junit carries no tool-call detail; a passing case is taken at its
            # word, a failing one is a failure, a skipped one is unverified.
            out[name] = {"status": status, "called_tools": None, "output": None}
        report.results_supplied = True
        return out

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        report.unparsed.append({"file": str(path), "reason": f"JSONDecodeError: {exc}"})
        return None
    rows = data.get("results") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        report.unparsed.append({"file": str(path), "reason": "no `results` list"})
        return None
    report.results_supplied = True
    keyed: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            keyed[row["id"]] = row
    return keyed


def evaluate_case(case: dict, result: dict | None) -> CaseReport:
    case_id = str(case.get("id") or "")
    expect_tool = case.get("expect_tool")
    expect_tool = expect_tool if isinstance(expect_tool, str) else None

    if result is None:
        return CaseReport(case_id, expect_tool, NEVER_RAN,
                          "declared in the spec and absent from the results")

    status = result.get("status")
    if status == "skipped":
        return CaseReport(case_id, expect_tool, UNVERIFIED,
                          "the eval was SKIPPED — credentials absent or the suite off. "
                          "The agent's behaviour is UNVERIFIED, which is not a pass")
    if status == "failed":
        return CaseReport(case_id, expect_tool, WRONG_ANSWER, "the eval ran and FAILED")

    called = result.get("called_tools")
    if expect_tool and isinstance(called, list):
        if expect_tool not in called:
            return CaseReport(
                case_id, expect_tool, TOOL_NOT_CALLED,
                f"ran, and `{expect_tool}` was NOT called (called: "
                f"{', '.join(called) if called else 'nothing'}). The config can declare a "
                f"tool the agent never reaches for — a missing system instruction does "
                f"exactly this",
                called_tools=called)
        expected_text = case.get("expect_contains")
        output = result.get("output")
        if isinstance(expected_text, str) and isinstance(output, str) \
                and expected_text.lower() not in output.lower():
            return CaseReport(case_id, expect_tool, WRONG_ANSWER,
                              f"`{expect_tool}` was called, but the answer does not contain "
                              f"{expected_text!r} — the tool ran and its data did not reach "
                              f"the user",
                              called_tools=called)
        return CaseReport(case_id, expect_tool, VERIFIED,
                          f"`{expect_tool}` was called and the answer carried its data",
                          called_tools=called)

    if expect_tool and called is None:
        return CaseReport(
            case_id, expect_tool, UNVERIFIED,
            f"the case passed, but the results record no tool calls, so it cannot be "
            f"shown that `{expect_tool}` was reached — emit `called_tools` to verify "
            f"behaviour rather than exit status")

    return CaseReport(case_id, expect_tool, VERIFIED, "the case ran and passed",
                      called_tools=called or [])


def check(source_root: Path, spec_path: Path | None = None,
          results_path: Path | None = None) -> Report:
    report = Report()
    spec_file = spec_path if spec_path is not None else source_root / DEFAULT_SPEC
    spec = load_spec(spec_file, report)
    if not report.spec_found:
        return report

    tools = [t for t in (spec.get("tools") or []) if isinstance(t, str)]
    cases = [c for c in (spec.get("cases") or []) if isinstance(c, dict) and c.get("id")]
    report.declared_tools = len(tools)
    report.declared_cases = len(cases)

    results = load_results(results_path, report) if results_path is not None else None

    for case in cases:
        if results is None:
            report.cases.append(CaseReport(
                str(case.get("id")), case.get("expect_tool"), NEVER_RAN,
                "no results supplied, so it is unknown whether this case ran — a "
                "declared golden set that nobody executes proves nothing"))
        else:
            report.cases.append(evaluate_case(case, results.get(str(case.get("id")))))

    for tool in tools:
        owning = [str(c.get("id")) for c in cases if c.get("expect_tool") == tool]
        if owning:
            report.tools.append(ToolReport(tool, "covered",
                                           f"{len(owning)} case(s) exercise it", owning))
        else:
            report.tools.append(ToolReport(
                tool, NO_CASE,
                "declared to the model and no golden case exercises it — nothing "
                "checks whether the agent ever reaches for it"))
    return report


def render_text(report: Report, gating: bool) -> str:
    m = report.measured
    lines = [
        f"Measured: spec {'found' if m['spec_found'] else 'NOT found'}, "
        f"{m['declared_tools']} tool(s), {m['declared_cases']} case(s), "
        + ("results supplied" if m["results_supplied"] else "NO results supplied")
    ]

    if report.unparsed:
        lines += ["", "ERROR: input(s) present that could not be read (chief-wiggum#289):"]
        lines += [f"  {u['file']}: {u['reason']}" for u in report.unparsed]

    if report.outcome == "inapplicable":
        lines += ["", f"INAPPLICABLE: no behavioural eval spec at {DEFAULT_SPEC} — nothing "
                      f"was checked, which is not a pass. If this product ships an agent "
                      f"with tools, its absence IS the gap (chief-wiggum#354)."]
        return "\n".join(lines)

    for state, label in (
        (TOOL_NOT_CALLED, "TOOL NOT CALLED — declared, and the agent never reached for it"),
        (WRONG_ANSWER, "WRONG ANSWER — the tool ran, its data did not reach the user"),
        (UNVERIFIED, "UNVERIFIED — the eval was SKIPPED"),
        (NEVER_RAN, "NEVER RAN — declared in the spec, absent from the results"),
        (VERIFIED, "VERIFIED — the agent called the tool and used its data"),
    ):
        group = [c for c in report.cases if c.state == state]
        if not group:
            continue
        lines += ["", f"## {label} ({len(group)})"]
        for c in group:
            lines.append(f"  {c.id}: {c.detail}")

    uncovered = [t for t in report.tools if t.state == NO_CASE]
    if uncovered:
        lines += ["", f"## TOOLS WITH NO CASE ({len(uncovered)})"]
        for t in uncovered:
            lines.append(f"  {t.tool}: {t.detail}")

    if report.findings and not gating:
        lines += ["", "(report-only: exiting 0. Pass --gate to block — see "
                      "docs/gate-rollout.md)"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="An agent must actually call its tools, not merely declare them (chief-wiggum#354)"
    )
    parser.add_argument("--source", required=True, help="Repo root")
    parser.add_argument("--spec", help=f"Eval spec (default: <source>/{DEFAULT_SPEC})")
    parser.add_argument("--results", help="Golden-set results (this gate's JSON, or junit-xml)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--gate", action="store_true",
                        help="Block (exit 1) on findings. Report-only without it.")
    args = parser.parse_args(argv)

    source = Path(args.source)
    missing = [] if source.exists() else [source]
    spec = Path(args.spec) if args.spec else None
    if spec is not None and not spec.exists():
        missing.append(spec)
    results = Path(args.results) if args.results else None
    if results is not None and not results.exists():
        missing.append(results)
    if missing:
        for t in missing:
            print(f"ERROR: {t} does not exist", file=sys.stderr)
        return EXIT_USAGE

    report = check(source, spec, results)

    if args.format == "json":
        print(json.dumps({
            "cases": [c.to_dict() for c in report.cases],
            "tools": [t.to_dict() for t in report.tools],
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
        emit_gate("check_behavioral_eval", report.outcome, caught=len(report.findings))
    except Exception:
        pass

    if report.unparsed:
        return EXIT_ERROR
    if report.findings and args.gate:
        return EXIT_FINDINGS
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
