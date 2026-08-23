"""Tests for the behavioural eval gate (chief-wiggum#354)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_behavioral_eval.py"
sys.path.insert(0, str(REPO / "scripts"))

from check_behavioral_eval import (  # noqa: E402
    NEVER_RAN,
    NO_CASE,
    TOOL_NOT_CALLED,
    UNVERIFIED,
    VERIFIED,
    WRONG_ANSWER,
    check,
)


def _spec(tmp_path: Path, tools=("get_venue_info",), cases=None) -> Path:
    src = tmp_path / "repo"
    (src / "docs" / "quality").mkdir(parents=True, exist_ok=True)
    if cases is None:
        cases = [{"id": "venue-hours", "prompt": "what time do you open?",
                  "expect_tool": "get_venue_info"}]
    (src / "docs" / "quality" / "behavioral-evals.json").write_text(
        json.dumps({"tools": list(tools), "cases": cases}))
    return src


def _results(tmp_path: Path, *rows: dict, name="results.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"results": list(rows)}))
    return path


def _junit(tmp_path: Path, *cases: tuple[str, str], name="results.xml") -> Path:
    body = []
    for case_id, state in cases:
        inner = {"ran": "", "failed": "<failure/>", "skipped": "<skipped/>"}[state]
        body.append(f'<testcase classname="evals" name="{case_id}">{inner}</testcase>')
    path = tmp_path / name
    path.write_text("<testsuite>" + "".join(body) + "</testsuite>")
    return path


# --- the defect this gate exists for ------------------------------------------

def test_a_declared_tool_the_agent_never_calls_is_a_finding(tmp_path):
    """The missing-system-instruction bug: tools were declared and passed to
    the model, the engine tests asserted exactly that, and the agent never
    called one."""
    src = _spec(tmp_path)
    report = check(src, results_path=_results(tmp_path, {
        "id": "venue-hours", "status": "ran", "called_tools": [],
        "output": "I can't retrieve that."}))
    assert [c.state for c in report.cases] == [TOOL_NOT_CALLED]
    assert report.outcome == "findings"
    assert "never reaches for" in report.cases[0].detail


def test_calling_the_expected_tool_verifies_the_case(tmp_path):
    src = _spec(tmp_path)
    report = check(src, results_path=_results(tmp_path, {
        "id": "venue-hours", "status": "ran",
        "called_tools": ["get_venue_info"], "output": "We open at 9am"}))
    assert [c.state for c in report.cases] == [VERIFIED]
    assert report.outcome == "pass"


def test_calling_some_other_tool_is_still_a_finding(tmp_path):
    src = _spec(tmp_path)
    report = check(src, results_path=_results(tmp_path, {
        "id": "venue-hours", "status": "ran", "called_tools": ["list_bookings"]}))
    assert [c.state for c in report.cases] == [TOOL_NOT_CALLED]
    assert "list_bookings" in report.cases[0].detail


def test_the_tool_running_but_its_data_not_reaching_the_user_is_a_finding(tmp_path):
    src = _spec(tmp_path, cases=[{"id": "venue-hours", "expect_tool": "get_venue_info",
                                  "expect_contains": "9am"}])
    report = check(src, results_path=_results(tmp_path, {
        "id": "venue-hours", "status": "ran", "called_tools": ["get_venue_info"],
        "output": "Sorry, I don't know."}))
    assert [c.state for c in report.cases] == [WRONG_ANSWER]


def test_expect_contains_is_case_insensitive(tmp_path):
    src = _spec(tmp_path, cases=[{"id": "h", "expect_tool": "get_venue_info",
                                  "expect_contains": "9AM"}])
    report = check(src, results_path=_results(tmp_path, {
        "id": "h", "status": "ran", "called_tools": ["get_venue_info"],
        "output": "we open at 9am"}))
    assert [c.state for c in report.cases] == [VERIFIED]


# --- a declared tool nothing exercises ----------------------------------------

def test_a_tool_with_no_golden_case_is_a_finding(tmp_path):
    """The structural half: config declares a tool and nothing checks whether
    the agent ever reaches for it."""
    src = _spec(tmp_path, tools=("get_venue_info", "get_date_info"))
    report = check(src, results_path=_results(tmp_path, {
        "id": "venue-hours", "status": "ran", "called_tools": ["get_venue_info"]}))
    uncovered = [t for t in report.tools if t.state == NO_CASE]
    assert [t.tool for t in uncovered] == ["get_date_info"]
    assert report.outcome == "findings"
    assert report.measured["tools_without_a_case"] == 1


def test_a_covered_tool_is_not_a_finding(tmp_path):
    src = _spec(tmp_path)
    report = check(src, results_path=_results(tmp_path, {
        "id": "venue-hours", "status": "ran", "called_tools": ["get_venue_info"]}))
    assert [t.state for t in report.tools] == ["covered"]
    assert report.findings == []


# --- skipped is loud, and is not a pass ---------------------------------------

def test_a_skipped_eval_is_unverified(tmp_path):
    src = _spec(tmp_path)
    report = check(src, results_path=_results(tmp_path, {
        "id": "venue-hours", "status": "skipped"}))
    assert [c.state for c in report.cases] == [UNVERIFIED]
    assert report.outcome == "findings"
    assert "SKIPPED" in report.cases[0].detail


def test_a_case_absent_from_the_results_never_ran(tmp_path):
    src = _spec(tmp_path)
    report = check(src, results_path=_results(tmp_path, {
        "id": "some-other-case", "status": "ran"}))
    assert [c.state for c in report.cases] == [NEVER_RAN]


def test_no_results_at_all_means_every_case_never_ran(tmp_path):
    """A declared golden set that nobody executes proves nothing."""
    src = _spec(tmp_path)
    report = check(src)
    assert [c.state for c in report.cases] == [NEVER_RAN]
    assert report.outcome == "findings"


def test_a_pass_with_no_recorded_tool_calls_cannot_be_verified(tmp_path):
    """Exit status is not behaviour. Without `called_tools` the case may have
    passed for any reason at all, which is the exact gap #354 describes."""
    src = _spec(tmp_path)
    report = check(src, results_path=_results(tmp_path, {
        "id": "venue-hours", "status": "ran"}))
    assert [c.state for c in report.cases] == [UNVERIFIED]
    assert "cannot be shown" in report.cases[0].detail


# --- junit ingestion ----------------------------------------------------------

@pytest.mark.parametrize("state,expected", [
    ("skipped", UNVERIFIED), ("failed", WRONG_ANSWER), ("ran", UNVERIFIED),
])
def test_junit_states_are_kept_distinct(tmp_path, state, expected):
    """junit carries no tool-call detail, so even a PASS is `unverified` here —
    it proves the case ran, never that the tool was reached."""
    src = _spec(tmp_path)
    report = check(src, results_path=_junit(tmp_path, ("venue-hours", state)))
    assert [c.state for c in report.cases] == [expected]


def test_a_case_without_an_expected_tool_passes_on_junit(tmp_path):
    src = _spec(tmp_path, tools=(), cases=[{"id": "smoke"}])
    report = check(src, results_path=_junit(tmp_path, ("smoke", "ran")))
    assert [c.state for c in report.cases] == [VERIFIED]


# --- five-state discipline (#289) --------------------------------------------

def test_no_spec_is_inapplicable_not_pass(tmp_path):
    src = tmp_path / "repo"
    src.mkdir()
    report = check(src)
    assert report.outcome == "inapplicable"
    assert report.measured["spec_found"] is False


def test_an_unreadable_spec_is_error(tmp_path):
    src = _spec(tmp_path)
    (src / "docs" / "quality" / "behavioral-evals.json").write_text("{ not json")
    report = check(src)
    assert report.outcome == "error"


def test_unreadable_results_are_error_not_a_silent_never_ran(tmp_path):
    src = _spec(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    report = check(src, results_path=bad)
    assert report.outcome == "error"


def test_malformed_junit_is_error(tmp_path):
    src = _spec(tmp_path)
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuite><oops")
    assert check(src, results_path=bad).outcome == "error"


def test_counts_travel_with_denominators(tmp_path):
    src = _spec(tmp_path, tools=("a", "b", "c"), cases=[
        {"id": "one", "expect_tool": "a"}, {"id": "two", "expect_tool": "b"},
    ])
    report = check(src, results_path=_results(
        tmp_path,
        {"id": "one", "status": "ran", "called_tools": ["a"]},
        {"id": "two", "status": "skipped"}))
    m = report.measured
    assert m["declared_tools"] == 3
    assert m["declared_cases"] == 2
    assert m["results_supplied"] is True
    assert m["cases_by_state"] == {VERIFIED: 1, UNVERIFIED: 1}
    assert m["tools_without_a_case"] == 1
    assert len(report.findings) == 2


# --- malformed input ----------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {}, {"tools": "no", "cases": "no"}, {"tools": [1, None], "cases": [1, None]},
    {"cases": [{"no_id": 1}]}, [],
])
def test_malformed_specs_do_not_crash(tmp_path, payload):
    src = tmp_path / "repo"
    (src / "docs" / "quality").mkdir(parents=True)
    (src / "docs" / "quality" / "behavioral-evals.json").write_text(json.dumps(payload))
    report = check(src)
    assert report.cases == []


def test_results_rows_of_the_wrong_shape_are_ignored(tmp_path):
    src = _spec(tmp_path)
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"results": [None, 3, {"no_id": 1}]}))
    report = check(src, results_path=path)
    assert [c.state for c in report.cases] == [NEVER_RAN]


def test_a_bare_results_list_is_accepted(tmp_path):
    src = _spec(tmp_path)
    path = tmp_path / "r.json"
    path.write_text(json.dumps([{"id": "venue-hours", "status": "ran",
                                 "called_tools": ["get_venue_info"]}]))
    assert [c.state for c in check(src, results_path=path).cases] == [VERIFIED]


# --- CLI ----------------------------------------------------------------------

def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


def test_cli_no_spec_is_inapplicable_and_exits_zero(tmp_path):
    src = tmp_path / "repo"
    src.mkdir()
    result = _run("--source", str(src))
    assert result.returncode == 0
    assert "INAPPLICABLE" in result.stdout
    assert "not a pass" in result.stdout


def test_cli_findings_exit_zero_without_gate(tmp_path):
    src = _spec(tmp_path)
    result = _run("--source", str(src))
    assert result.returncode == 0
    assert "NEVER RAN" in result.stdout
    assert "report-only" in result.stdout


def test_cli_gate_blocks_on_findings(tmp_path):
    src = _spec(tmp_path)
    assert _run("--source", str(src), "--gate").returncode == 1


def test_cli_gate_passes_when_clean(tmp_path):
    src = _spec(tmp_path)
    results = _results(tmp_path, {"id": "venue-hours", "status": "ran",
                                  "called_tools": ["get_venue_info"]})
    assert _run("--source", str(src), "--results", str(results), "--gate").returncode == 0


def test_cli_unreadable_spec_exits_three_even_report_only(tmp_path):
    src = _spec(tmp_path)
    (src / "docs" / "quality" / "behavioral-evals.json").write_text("{ not json")
    assert _run("--source", str(src)).returncode == 3


def test_cli_missing_input_is_a_usage_error(tmp_path):
    assert _run("--source", str(tmp_path / "nope")).returncode == 2


def test_cli_json_carries_outcome_and_measured(tmp_path):
    src = _spec(tmp_path)
    results = _results(tmp_path, {"id": "venue-hours", "status": "ran",
                                  "called_tools": []})
    payload = json.loads(_run("--source", str(src), "--results", str(results),
                              "--format", "json").stdout)
    assert payload["outcome"] == "findings"
    assert payload["cases"][0]["state"] == TOOL_NOT_CALLED
    assert payload["measured"]["results_supplied"] is True
