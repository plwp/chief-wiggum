"""Tests for the fixture-provenance gate (chief-wiggum#351)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_fixture_provenance.py"
sys.path.insert(0, str(REPO / "scripts"))

from check_fixture_provenance import (  # noqa: E402
    HAND_AUTHORED,
    MISSING_CAPTURE,
    NO_FIXTURE,
    RECORDED,
    UNATTRIBUTED,
    check,
)


def _epic(tmp_path: Path, *systems: str) -> Path:
    epic = tmp_path / "epic"
    epic.mkdir(exist_ok=True)
    (epic / "contracts.json").write_text(json.dumps({"entities": [{
        "name": "B",
        "operations": [{"name": f"call {s}", "method": "GET", "path": f"/{s.lower()}",
                        "external": True, "external_system": s} for s in systems],
    }]}))
    return epic


def _src(tmp_path: Path, *annotations: str) -> Path:
    src = tmp_path / "repo"
    src.mkdir(exist_ok=True)
    body = "\n".join(f"// {a}\nfunc newFixture() {{}}" for a in annotations)
    (src / "fixture.go").write_text(body or "package main\n")
    return src


def _capture(src: Path, rel: str, payload=None, raw: str | None = None) -> Path:
    path = src / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw)
    else:
        path.write_text(json.dumps(payload if payload is not None else {
            "captured_at": "2026-08-23T04:11:00Z",
            "source": "https://scp.example.com/venue-info (staging, curl)",
            "response": {"opens": "9am"},
        }))
    return path


# --- the defect this gate exists for ------------------------------------------

def test_a_fixture_with_no_capture_is_hand_authored(tmp_path):
    """The collusion: the fixture routed by TrimPrefix and looked up by tool
    name — the exact same wrong assumption as the client. Both agreed, every
    test was green, and the route bug stayed invisible."""
    report = check([_epic(tmp_path, "SCP")], _src(tmp_path, "@cw-fixture SCP"))
    assert [s.state for s in report.systems] == [HAND_AUTHORED]
    assert report.outcome == "findings"
    assert "invented" in report.systems[0].detail


def test_a_fixture_citing_an_attributed_capture_is_recorded(tmp_path):
    src = _src(tmp_path, "@cw-fixture SCP capture=testdata/scp.json")
    _capture(src, "testdata/scp.json")
    report = check([_epic(tmp_path, "SCP")], src)
    assert [s.state for s in report.systems] == [RECORDED]
    assert report.outcome == "pass"


def test_a_cited_capture_that_does_not_exist_is_a_finding(tmp_path):
    src = _src(tmp_path, "@cw-fixture SCP capture=testdata/nope.json")
    report = check([_epic(tmp_path, "SCP")], src)
    assert [s.state for s in report.systems] == [MISSING_CAPTURE]
    assert report.outcome == "findings"
    assert len(report.findings) == 1


# --- the evasion: an empty or unattributed capture -----------------------------

@pytest.mark.parametrize("payload", [{}, {"response": {"a": 1}},
                                     {"captured_at": "", "source": ""}])
def test_a_capture_with_no_provenance_is_unattributed(tmp_path, payload):
    """Without captured_at/source an invented file satisfies the gate exactly as
    well as a real capture, which would make the whole check theatre."""
    src = _src(tmp_path, "@cw-fixture SCP capture=testdata/scp.json")
    _capture(src, "testdata/scp.json", payload=payload)
    report = check([_epic(tmp_path, "SCP")], src)
    assert [s.state for s in report.systems] == [UNATTRIBUTED]
    assert report.outcome == "findings"
    assert len(report.findings) == 1


@pytest.mark.parametrize("keys", [{"captured_at": "2026-08-23"}, {"source": "curl staging"}])
def test_either_provenance_key_is_enough(tmp_path, keys):
    src = _src(tmp_path, "@cw-fixture SCP capture=testdata/scp.json")
    _capture(src, "testdata/scp.json", payload={**keys, "response": {}})
    assert [s.state for s in check([_epic(tmp_path, "SCP")], src).systems] == [RECORDED]


def test_an_empty_capture_file_is_unattributed(tmp_path):
    src = _src(tmp_path, "@cw-fixture SCP capture=testdata/raw.txt")
    _capture(src, "testdata/raw.txt", raw="")
    assert [s.state for s in check([_epic(tmp_path, "SCP")], src).systems] == [UNATTRIBUTED]


def test_a_non_json_capture_with_content_is_accepted(tmp_path):
    """A raw body dump is a legitimate capture; non-empty content is the most
    that can honestly be asserted about one."""
    src = _src(tmp_path, "@cw-fixture SCP capture=testdata/raw.txt")
    _capture(src, "testdata/raw.txt", raw="HTTP/1.1 200 OK\n\n{\"opens\":\"9am\"}")
    assert [s.state for s in check([_epic(tmp_path, "SCP")], src).systems] == [RECORDED]


# --- one good fixture must not vouch for a bad sibling ------------------------

def test_a_recorded_fixture_does_not_cover_a_hand_authored_one(tmp_path):
    src = _src(tmp_path, "@cw-fixture SCP capture=testdata/scp.json", "@cw-fixture SCP")
    _capture(src, "testdata/scp.json")
    report = check([_epic(tmp_path, "SCP")], src)
    assert [s.state for s in report.systems] == [HAND_AUTHORED]


def test_hand_authored_outranks_a_missing_capture(tmp_path):
    src = _src(tmp_path, "@cw-fixture SCP capture=nope.json", "@cw-fixture SCP")
    assert [s.state for s in check([_epic(tmp_path, "SCP")], src).systems] == [HAND_AUTHORED]


# --- scope --------------------------------------------------------------------

def test_a_system_with_no_fixture_is_reported_but_not_a_finding(tmp_path):
    """Not every external system needs a double; inventing that requirement
    here would be noise."""
    report = check([_epic(tmp_path, "SCP")], _src(tmp_path))
    assert [s.state for s in report.systems] == [NO_FIXTURE]
    assert report.findings == []
    assert report.outcome == "pass"
    # REPORTED, not merely absent — a system that silently vanishes from the
    # listing is indistinguishable from one that was never declared.
    assert "no `@cw-fixture` double declared" in report.systems[0].detail


def test_system_matching_is_case_insensitive(tmp_path):
    src = _src(tmp_path, "@cw-fixture scp capture=testdata/scp.json")
    _capture(src, "testdata/scp.json")
    assert [s.state for s in check([_epic(tmp_path, "SCP")], src).systems] == [RECORDED]


def test_a_fixture_for_an_undeclared_system_is_ignored(tmp_path):
    report = check([_epic(tmp_path, "SCP")],
                   _src(tmp_path, "@cw-fixture SCP capture=x", "@cw-fixture Twilio"))
    assert [s.system for s in report.systems] == ["SCP"]


def test_vendor_directories_are_not_scanned(tmp_path):
    src = tmp_path / "repo"
    (src / "vendor" / "dep").mkdir(parents=True)
    (src / "vendor" / "dep" / "f.go").write_text("// @cw-fixture SCP\n")
    assert [s.state for s in check([_epic(tmp_path, "SCP")], src).systems] == [NO_FIXTURE]


# --- five-state discipline (#289) --------------------------------------------

def test_no_declared_external_system_is_inapplicable(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text(json.dumps({"entities": [{
        "name": "B", "operations": [{"name": "own", "method": "GET", "path": "/a"}]}]}))
    report = check([epic], _src(tmp_path))
    assert report.outcome == "inapplicable"


def test_unreadable_contracts_is_error(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text("{ not json")
    assert check([epic], _src(tmp_path)).outcome == "error"


def test_counts_travel_with_denominators(tmp_path):
    src = _src(tmp_path, "@cw-fixture A capture=testdata/a.json", "@cw-fixture B")
    _capture(src, "testdata/a.json")
    report = check([_epic(tmp_path, "A", "B", "C")], src)
    m = report.measured
    assert m["external_operations"] == 3
    assert m["systems_declared"] == 3
    assert m["by_state"] == {RECORDED: 1, HAND_AUTHORED: 1, NO_FIXTURE: 1}
    assert len(report.findings) == 1


@pytest.mark.parametrize("payload", [
    {}, {"entities": "no"}, {"entities": [None, 3]},
    {"entities": [{"name": "B", "operations": None}]}, [],
])
def test_malformed_contracts_do_not_crash(tmp_path, payload):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text(json.dumps(payload))
    assert check([epic], _src(tmp_path)).systems == []


def test_a_source_tree_that_does_not_exist_does_not_crash(tmp_path):
    report = check([_epic(tmp_path, "SCP")], tmp_path / "nope")
    assert [s.state for s in report.systems] == [NO_FIXTURE]


# --- CLI ----------------------------------------------------------------------

def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


def test_cli_findings_exit_zero_without_gate(tmp_path):
    result = _run(str(_epic(tmp_path, "SCP")), "--source",
                  str(_src(tmp_path, "@cw-fixture SCP")))
    assert result.returncode == 0
    assert "HAND-AUTHORED" in result.stdout
    assert "report-only" in result.stdout


def test_cli_gate_blocks_on_a_hand_authored_fixture(tmp_path):
    assert _run(str(_epic(tmp_path, "SCP")), "--source",
                str(_src(tmp_path, "@cw-fixture SCP")), "--gate").returncode == 1


def test_cli_gate_passes_when_recorded(tmp_path):
    src = _src(tmp_path, "@cw-fixture SCP capture=testdata/scp.json")
    _capture(src, "testdata/scp.json")
    assert _run(str(_epic(tmp_path, "SCP")), "--source", str(src), "--gate").returncode == 0


def test_cli_unreadable_contracts_exits_three_even_report_only(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text("{ not json")
    assert _run(str(epic), "--source", str(_src(tmp_path))).returncode == 3


def test_cli_missing_target_is_a_usage_error(tmp_path):
    assert _run(str(tmp_path / "nope"), "--source", str(tmp_path)).returncode == 2


def test_cli_json_carries_outcome_and_measured(tmp_path):
    payload = json.loads(_run(str(_epic(tmp_path, "SCP")), "--source",
                              str(_src(tmp_path, "@cw-fixture SCP")),
                              "--format", "json").stdout)
    assert payload["outcome"] == "findings"
    assert payload["systems"][0]["state"] == HAND_AUTHORED


def test_cli_inapplicable_says_not_a_pass(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text(json.dumps({"entities": []}))
    result = _run(str(epic), "--source", str(_src(tmp_path)))
    assert result.returncode == 0
    assert "INAPPLICABLE" in result.stdout
    assert "not a pass" in result.stdout
