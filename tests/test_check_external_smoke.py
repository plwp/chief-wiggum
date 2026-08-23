"""Tests for the external-integration smoke gate (chief-wiggum#353)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_external_smoke.py"
sys.path.insert(0, str(REPO / "scripts"))

from check_external_smoke import (  # noqa: E402
    FAILED,
    NEVER_RAN,
    NO_SMOKE,
    SMOKE_DECLARED,
    UNVERIFIED,
    VERIFIED,
    check,
    parse_junit,
)


def _epic(tmp_path: Path, *systems: str, unnamed: int = 0) -> Path:
    ops = [{
        "name": f"Call {s}", "method": "GET", "path": f"/{s.lower()}",
        "external": True, "external_system": s,
        "derived_from": [{"type": "api_doc", "ref": "d"}],
    } for s in systems]
    ops += [{"name": "Unnamed", "method": "GET", "path": "/x", "external": True}
            for _ in range(unnamed)]
    epic = tmp_path / "epic"
    epic.mkdir(exist_ok=True)
    (epic / "contracts.json").write_text(json.dumps({
        "entities": [{"name": "Booking", "operations": ops}]}))
    return epic


def _source(tmp_path: Path, *annotations: str) -> Path:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    body = "\n".join(f"// {a}\nfunc TestThing(t *testing.T) {{}}" for a in annotations)
    (src / "live_smoke_test.go").write_text(body or "package main\n")
    return src


def _junit(tmp_path: Path, *cases, name: str = "results.xml") -> Path:
    """cases are (identifier, state[, file]); state in passed|failed|skipped.

    `file` defaults to the annotated smoke file, so the file fallback matches
    unless a case deliberately belongs elsewhere.
    """
    body = []
    for case in cases:
        identifier, state = case[0], case[1]
        src_file = case[2] if len(case) > 2 else "live_smoke_test.go"
        classname, _, casename = identifier.rpartition("::")
        inner = {"passed": "", "failed": "<failure/>", "skipped": "<skipped/>"}[state]
        body.append(
            f'<testcase classname="{classname}" name="{casename}" '
            f'file="{src_file}">{inner}</testcase>'
        )
    path = tmp_path / name
    path.write_text("<testsuite>" + "".join(body) + "</testsuite>")
    return path


# --- the core distinction: skipped is not passed, and not failed -------------

def test_a_skipped_smoke_is_unverified_never_a_pass(tmp_path):
    """The whole point of #353. Credentials absent, so the smoke skipped —
    that is a visible gap in the epic report, not a green tick."""
    report = check(
        [_epic(tmp_path, "SCP")],
        _source(tmp_path, "@cw-smoke SCP case=TestSCPLive"),
        _junit(tmp_path, ("engine::TestSCPLive", "skipped")),
    )
    assert [s.state for s in report.systems] == [UNVERIFIED]
    assert report.outcome == "findings"
    assert "SKIPPED" in report.systems[0].detail


def test_a_passing_smoke_is_verified(tmp_path):
    report = check(
        [_epic(tmp_path, "SCP")],
        _source(tmp_path, "@cw-smoke SCP case=TestSCPLive"),
        _junit(tmp_path, ("engine::TestSCPLive", "passed")),
    )
    assert [s.state for s in report.systems] == [VERIFIED]
    assert report.outcome == "pass"


def test_a_failing_smoke_is_failed_not_unverified(tmp_path):
    report = check(
        [_epic(tmp_path, "SCP")],
        _source(tmp_path, "@cw-smoke SCP case=TestSCPLive"),
        _junit(tmp_path, ("engine::TestSCPLive", "failed")),
    )
    assert [s.state for s in report.systems] == [FAILED]


def test_junit_parsing_keeps_all_three_states_distinct(tmp_path):
    """ratchet.parse_junit_xml returns passing ids only and drops skipped in
    with failed. Reusing it would recreate the conflation this gate exists to
    prevent."""
    cases = parse_junit(_junit(
        tmp_path,
        ("a::one", "passed"), ("a::two", "failed"), ("a::three", "skipped"),
    ).read_text())
    assert {c.identifier: c.state for c in cases} == {
        "a::one": "passed", "a::two": "failed", "a::three": "skipped"}


def test_one_passing_smoke_does_not_mask_a_skipped_sibling(tmp_path):
    """A system is only as verified as its weakest smoke — masking is the same
    silent-green in a smaller box."""
    report = check(
        [_epic(tmp_path, "SCP")],
        _source(tmp_path, "@cw-smoke SCP case=TestA", "@cw-smoke SCP case=TestB"),
        _junit(tmp_path, ("e::TestA", "passed"), ("e::TestB", "skipped")),
    )
    assert [s.state for s in report.systems] == [UNVERIFIED]


def test_a_failure_outranks_a_skip(tmp_path):
    report = check(
        [_epic(tmp_path, "SCP")],
        _source(tmp_path, "@cw-smoke SCP case=TestA", "@cw-smoke SCP case=TestB"),
        _junit(tmp_path, ("e::TestA", "skipped"), ("e::TestB", "failed")),
    )
    assert [s.state for s in report.systems] == [FAILED]


# --- static half --------------------------------------------------------------

def test_a_declared_system_with_no_smoke_anywhere_is_a_finding(tmp_path):
    report = check([_epic(tmp_path, "SCP")], _source(tmp_path), None)
    assert [s.state for s in report.systems] == [NO_SMOKE]
    assert report.outcome == "findings"


def test_an_annotation_without_results_is_declared_not_verified(tmp_path):
    """An annotation proves a smoke EXISTS. Letting that read as `verified`
    would be the same vacuous pass in a new coat."""
    report = check([_epic(tmp_path, "SCP")],
                   _source(tmp_path, "@cw-smoke SCP case=TestSCPLive"), None)
    assert [s.state for s in report.systems] == [SMOKE_DECLARED]
    assert report.systems[0].state != VERIFIED
    # smoke_declared is not blocking — it is honest ignorance, not a defect.
    assert report.outcome == "pass"


def test_system_matching_is_case_insensitive(tmp_path):
    report = check([_epic(tmp_path, "Stripe")],
                   _source(tmp_path, "@cw-smoke stripe case=TestLive"),
                   _junit(tmp_path, ("b::TestLive", "passed")))
    assert [s.state for s in report.systems] == [VERIFIED]


def test_an_annotation_for_an_undeclared_system_is_not_a_finding(tmp_path):
    """This gate checks declared systems have smokes, not that every smoke has
    a declaration — the reverse direction belongs to #350's declaration gap."""
    report = check([_epic(tmp_path, "SCP")],
                   _source(tmp_path, "@cw-smoke SCP case=T", "@cw-smoke Twilio case=T2"),
                   _junit(tmp_path, ("b::T", "passed")))
    assert [s.system for s in report.systems] == ["SCP"]


# --- results matching ---------------------------------------------------------

def test_annotated_but_absent_from_results_is_never_ran(tmp_path):
    """Distinct from skipped: skipped means the runner saw it and declined;
    never_ran means the suite did not contain it at all."""
    report = check([_epic(tmp_path, "SCP")],
                   _source(tmp_path, "@cw-smoke SCP case=TestSCPLive"),
                   _junit(tmp_path, ("other::TestSomethingElse", "passed")))
    assert [s.state for s in report.systems] == [NEVER_RAN]


def test_never_ran_blocks(tmp_path):
    """Annotated, results supplied, and no case matched: the smoke did not run.
    That is a gap in the evidence, not a pass — the same class as a skip."""
    report = check([_epic(tmp_path, "SCP")],
                   _source(tmp_path, "@cw-smoke SCP case=TestSCPLive"),
                   _junit(tmp_path, ("other::Else", "passed", "other_test.go")))
    assert [s.state for s in report.systems] == [NEVER_RAN]
    assert report.outcome == "findings"
    assert len(report.findings) == 1


def test_an_unpinned_annotation_says_to_pin_it(tmp_path):
    report = check([_epic(tmp_path, "SCP")],
                   _source(tmp_path, "@cw-smoke SCP"),
                   _junit(tmp_path, ("other::Nope", "passed", "unrelated_test.go")))
    assert report.systems[0].state == NEVER_RAN
    assert "case=" in report.systems[0].detail


def test_an_unpinned_pass_cannot_earn_verified(tmp_path):
    """The file fallback matches EVERY case in the annotated file, so an
    unrelated passing test would otherwise award a pass the integration never
    earned. Weak evidence may raise an alarm; it may never grant one."""
    report = check([_epic(tmp_path, "SCP")],
                   _source(tmp_path, "@cw-smoke SCP"),
                   _junit(tmp_path, ("pkg::TestSomethingUnrelated", "passed")))
    assert [s.state for s in report.systems] == [SMOKE_DECLARED]
    assert "not pinned" in report.systems[0].detail


def test_an_unpinned_skip_still_raises_the_alarm(tmp_path):
    """Fail-closed in the direction #353 cares about: a skip in the smoke's own
    file is worth knowing even when the annotation is imprecise."""
    report = check([_epic(tmp_path, "SCP")],
                   _source(tmp_path, "@cw-smoke SCP"),
                   _junit(tmp_path, ("pkg::TestLive", "skipped")))
    assert [s.state for s in report.systems] == [UNVERIFIED]


def test_a_pinned_pass_does_earn_verified(tmp_path):
    report = check([_epic(tmp_path, "SCP")],
                   _source(tmp_path, "@cw-smoke SCP case=TestLive"),
                   _junit(tmp_path, ("pkg::TestLive", "passed")))
    assert [s.state for s in report.systems] == [VERIFIED]


def test_a_partly_pinned_system_is_not_treated_as_pinned(tmp_path):
    """Two smokes, one pinned and one not: the unpinned one can still drag in
    unrelated cases, so the system does not qualify for a verified verdict."""
    report = check([_epic(tmp_path, "SCP")],
                   _source(tmp_path, "@cw-smoke SCP case=TestLive", "@cw-smoke SCP"),
                   _junit(tmp_path, ("pkg::TestLive", "passed")))
    assert [s.state for s in report.systems] == [SMOKE_DECLARED]


# --- five-state discipline (#289) --------------------------------------------

def test_no_declared_external_system_is_inapplicable_not_pass(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text(json.dumps({"entities": [
        {"name": "B", "operations": [{"name": "Own", "method": "GET", "path": "/a"}]}]}))
    report = check([epic], _source(tmp_path), None)
    assert report.outcome == "inapplicable"
    assert report.applicability == "inapplicable"


def test_an_external_operation_with_no_system_name_cannot_be_checked(tmp_path):
    """Counted in the denominator so the gap is visible, but it cannot produce
    a per-system verdict — you cannot demand a smoke for something unnamed."""
    report = check([_epic(tmp_path, unnamed=2)], _source(tmp_path), None)
    assert report.measured["external_operations"] == 2
    assert report.measured["systems_declared"] == 0
    assert report.outcome == "inapplicable"


def test_unreadable_contracts_is_error_not_pass(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text("{ not json")
    report = check([epic], _source(tmp_path), None)
    assert report.outcome == "error"


def test_unreadable_results_is_error_not_a_silent_static_run(tmp_path):
    """Falling back to the static half would turn a broken instrument into a
    weaker-but-passing answer."""
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuite><oops")
    report = check([_epic(tmp_path, "SCP")],
                   _source(tmp_path, "@cw-smoke SCP case=T"), bad)
    assert report.outcome == "error"


def test_counts_travel_with_denominators(tmp_path):
    report = check(
        [_epic(tmp_path, "SCP", "Stripe", "Twilio")],
        _source(tmp_path, "@cw-smoke SCP case=TA", "@cw-smoke Stripe case=TB"),
        _junit(tmp_path, ("e::TA", "passed"), ("e::TB", "skipped")),
    )
    m = report.measured
    assert m["external_operations"] == 3
    assert m["systems_declared"] == 3
    assert m["results_supplied"] is True
    assert m["by_state"] == {VERIFIED: 1, UNVERIFIED: 1, NO_SMOKE: 1}
    assert len(report.findings) == 2


# --- malformed input ----------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {}, {"entities": "nope"}, {"entities": [None, 3]},
    {"entities": [{"name": "B", "operations": None}]},
    {"entities": [{"name": "B", "operations": [None, 7]}]}, [],
])
def test_malformed_contracts_do_not_crash(tmp_path, payload):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text(json.dumps(payload))
    report = check([epic], _source(tmp_path), None)
    assert report.systems == []


def test_a_source_tree_that_does_not_exist_does_not_crash(tmp_path):
    report = check([_epic(tmp_path, "SCP")], tmp_path / "nope", None)
    assert [s.state for s in report.systems] == [NO_SMOKE]


def test_vendor_directories_are_not_scanned(tmp_path):
    src = tmp_path / "src"
    (src / "vendor" / "dep").mkdir(parents=True)
    (src / "vendor" / "dep" / "x_test.go").write_text("// @cw-smoke SCP case=T\n")
    report = check([_epic(tmp_path, "SCP")], src, None)
    assert [s.state for s in report.systems] == [NO_SMOKE]


# --- CLI ----------------------------------------------------------------------

def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


def test_cli_report_only_exits_zero_with_findings(tmp_path):
    epic, src = _epic(tmp_path, "SCP"), _source(tmp_path)
    result = _run(str(epic), "--source", str(src))
    assert result.returncode == 0
    assert "NO SMOKE" in result.stdout
    assert "report-only" in result.stdout


def test_cli_gate_exits_one_on_findings(tmp_path):
    epic, src = _epic(tmp_path, "SCP"), _source(tmp_path)
    assert _run(str(epic), "--source", str(src), "--gate").returncode == 1


def test_cli_gate_exits_one_on_a_skipped_smoke(tmp_path):
    epic = _epic(tmp_path, "SCP")
    src = _source(tmp_path, "@cw-smoke SCP case=TestLive")
    results = _junit(tmp_path, ("e::TestLive", "skipped"))
    assert _run(str(epic), "--source", str(src), "--results", str(results),
                "--gate").returncode == 1


def test_cli_static_run_says_it_is_static(tmp_path):
    epic = _epic(tmp_path, "SCP")
    src = _source(tmp_path, "@cw-smoke SCP case=TestLive")
    out = _run(str(epic), "--source", str(src)).stdout
    assert "STATIC ONLY" in out
    assert "never that it ran" in out


def test_cli_unreadable_results_exits_three_even_report_only(tmp_path):
    epic = _epic(tmp_path, "SCP")
    src = _source(tmp_path, "@cw-smoke SCP case=T")
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuite><oops")
    assert _run(str(epic), "--source", str(src), "--results", str(bad)).returncode == 3


def test_cli_missing_input_is_a_usage_error(tmp_path):
    assert _run(str(tmp_path / "nope"), "--source", str(tmp_path)).returncode == 2
    epic = _epic(tmp_path, "SCP")
    assert _run(str(epic), "--source", str(tmp_path / "nosrc")).returncode == 2


def test_cli_json_carries_outcome_and_states(tmp_path):
    epic = _epic(tmp_path, "SCP")
    src = _source(tmp_path, "@cw-smoke SCP case=TestLive")
    results = _junit(tmp_path, ("e::TestLive", "skipped"))
    payload = json.loads(_run(str(epic), "--source", str(src), "--results",
                              str(results), "--format", "json").stdout)
    assert payload["outcome"] == "findings"
    assert payload["systems"][0]["state"] == UNVERIFIED
    assert payload["measured"]["results_supplied"] is True


def test_cli_inapplicable_says_not_a_pass(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text(json.dumps({"entities": []}))
    result = _run(str(epic), "--source", str(_source(tmp_path)))
    assert result.returncode == 0
    assert "INAPPLICABLE" in result.stdout
    assert "not a pass" in result.stdout
