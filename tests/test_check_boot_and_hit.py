"""Tests for the boot-and-hit gate (chief-wiggum#352)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_boot_and_hit.py"
sys.path.insert(0, str(REPO / "scripts"))

from check_boot_and_hit import (  # noqa: E402
    ERROR_STATUS,
    NOT_PROBED,
    NOT_SERVED,
    SERVED,
    SERVED_GATED,
    UNPROBEABLE,
    UNREACHABLE,
    check,
    classify,
    substitute,
)


def _epic(tmp_path: Path, *ops: dict) -> Path:
    epic = tmp_path / "epic"
    epic.mkdir(exist_ok=True)
    (epic / "contracts.json").write_text(json.dumps({
        "entities": [{"name": "Booking", "operations": list(ops)}]}))
    return epic


def _op(method="GET", path="/health", **over) -> dict:
    base = {"name": f"{method} {path}", "method": method, "path": path}
    base.update(over)
    return base


def _src(tmp_path: Path, *files: str) -> Path:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    for rel in files:
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("package main\n")
    return src


def _probe(mapping: dict[str, int], fail: set[str] | None = None):
    """A fake probe. Tests never touch the network."""
    def probe(url: str, method: str) -> int:
        if fail and any(f in url for f in fail):
            raise ConnectionRefusedError("connection refused")
        for suffix, status in mapping.items():
            if url.endswith(suffix):
                return status
        return 404
    return probe


# --- the defect this gate exists for ------------------------------------------

def test_a_declared_route_the_service_does_not_serve_is_a_finding(tmp_path):
    """The mux-wiring bug: contracts declare /widget.js, the assembled binary
    never registered it, package tests were green throughout."""
    report = check([_epic(tmp_path, _op(path="/widget.js"))], _src(tmp_path),
                   base_url="http://localhost:8080",
                   probe=_probe({"/widget.js": 404}))
    assert [r.state for r in report.routes] == [NOT_SERVED]
    assert report.outcome == "findings"


def test_a_served_route_passes(tmp_path):
    report = check([_epic(tmp_path, _op(path="/health"))], _src(tmp_path),
                   base_url="http://localhost:8080",
                   probe=_probe({"/health": 200}))
    assert [r.state for r in report.routes] == [SERVED]
    assert report.outcome == "pass"


def test_a_crash_looping_service_is_unreachable_not_a_pass(tmp_path):
    """The startup panic. Every route fails to answer — that is the loudest
    possible signal and must never be silence."""
    report = check([_epic(tmp_path, _op(path="/health"))], _src(tmp_path),
                   base_url="http://localhost:8080",
                   probe=_probe({}, fail={"/health"}))
    assert [r.state for r in report.routes] == [UNREACHABLE]
    assert report.outcome == "findings"


def test_a_five_hundred_is_registered_but_erroring(tmp_path):
    report = check([_epic(tmp_path, _op(path="/health"))], _src(tmp_path),
                   base_url="http://x", probe=_probe({"/health": 503}))
    assert [r.state for r in report.routes] == [ERROR_STATUS]


# --- what counts as "registered" ----------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_gated_route_is_registered(tmp_path, status):
    """The question is whether the composition wired the route, not whether an
    anonymous caller may use it. An auth layer answering proves registration."""
    report = check([_epic(tmp_path, _op(path="/admin"))], _src(tmp_path),
                   base_url="http://x", probe=_probe({"/admin": status}))
    assert [r.state for r in report.routes] == [SERVED_GATED]
    assert report.outcome == "pass"


def test_a_status_the_contract_declares_is_served(tmp_path):
    """A 409 the operation itself declares is the route working."""
    op = _op(path="/orders", error_cases=[{"status": 409, "condition": "duplicate"}])
    report = check([_epic(tmp_path, op)], _src(tmp_path),
                   base_url="http://x", probe=_probe({"/orders": 409}))
    assert [r.state for r in report.routes] == [SERVED]


@pytest.mark.parametrize("status,expected", [
    (200, SERVED), (204, SERVED), (302, SERVED), (401, SERVED_GATED),
    (403, SERVED_GATED), (404, NOT_SERVED), (405, NOT_SERVED),
    (500, ERROR_STATUS), (502, ERROR_STATUS),
])
def test_status_classification(status, expected):
    assert classify(status, set())[0] == expected


def test_a_declared_404_is_served_not_a_finding():
    """An operation that documents its own 404 is not evidence of bad wiring."""
    assert classify(404, {404})[0] == SERVED


# --- safety: never fire a mutating request by default -------------------------

@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_mutating_methods_are_not_probed_by_default(tmp_path, method):
    """Firing DELETE /orders/1 at a real service destroys data. A gate that
    damages the system it inspects is worse than no gate."""
    called = []

    def probe(url, m):
        called.append((m, url))
        return 200

    report = check([_epic(tmp_path, _op(method=method, path="/orders"))],
                   _src(tmp_path), base_url="http://x", probe=probe)
    assert called == []
    assert [r.state for r in report.routes] == [NOT_PROBED]


def test_mutating_methods_are_probed_when_explicitly_opted_in(tmp_path):
    called = []

    def probe(url, m):
        called.append((m, url))
        return 201

    report = check([_epic(tmp_path, _op(method="POST", path="/orders"))],
                   _src(tmp_path), base_url="http://x", probe_mutating=True, probe=probe)
    assert called == [("POST", "http://x/orders")]
    assert [r.state for r in report.routes] == [SERVED]


def test_not_probed_is_not_a_finding(tmp_path):
    """Declining to probe is an honest abstention, not a defect in the code."""
    report = check([_epic(tmp_path, _op(method="DELETE", path="/orders"))],
                   _src(tmp_path), base_url="http://x", probe=_probe({}))
    assert report.findings == []
    assert report.outcome == "pass"


# --- safety: never guess a path parameter -------------------------------------

@pytest.mark.parametrize("path", ["/orders/:id", "/orders/{id}", "/orders/<id>"])
def test_parameterized_paths_are_not_guessed(tmp_path, path):
    """A 404 from an invented id means 'no such record' as readily as 'not
    registered' — which is the exact question being asked."""
    called = []

    def probe(url, m):
        called.append(url)
        return 404

    report = check([_epic(tmp_path, _op(path=path))], _src(tmp_path),
                   base_url="http://x", probe=probe)
    assert called == []
    assert [r.state for r in report.routes] == [UNPROBEABLE]
    assert report.findings == []


def test_a_supplied_substitution_makes_the_route_probeable(tmp_path):
    called = []

    def probe(url, m):
        called.append(url)
        return 200

    report = check([_epic(tmp_path, _op(path="/orders/:id"))], _src(tmp_path),
                   base_url="http://x", params={"id": "123"}, probe=probe)
    assert called == ["http://x/orders/123"]
    assert [r.state for r in report.routes] == [SERVED]


def test_substitute_reports_only_the_unfilled_parameters():
    filled, missing = substitute("/a/:x/b/{y}/c/<z>", {"x": "1", "z": "3"})
    assert filled == "/a/1/b/{y}/c/3"
    assert missing == ["y"]


def test_a_partly_substituted_path_is_still_unprobeable(tmp_path):
    report = check([_epic(tmp_path, _op(path="/o/:a/p/:b"))], _src(tmp_path),
                   base_url="http://x", params={"a": "1"}, probe=_probe({}))
    assert [r.state for r in report.routes] == [UNPROBEABLE]
    assert "b" in report.routes[0].detail


# --- scope: external operations belong to #353 --------------------------------

def test_external_operations_are_skipped(tmp_path):
    report = check([_epic(tmp_path, _op(path="/own"), _op(path="/vendor", external=True))],
                   _src(tmp_path), base_url="http://x", probe=_probe({"/own": 200}))
    assert [r.path for r in report.routes] == ["/own"]
    assert report.measured["external_skipped"] == 1
    assert report.measured["declared_operations"] == 2


# --- the entrypoint conjunction -----------------------------------------------

def test_an_entrypoint_with_no_tests_and_no_probe_is_a_finding(tmp_path):
    report = check([_epic(tmp_path, _op())], _src(tmp_path, "cmd/server/main.go"))
    assert [e.state for e in report.entrypoints] == ["untested_and_unbooted"]
    assert len(report.findings) == 1


def test_an_entrypoint_with_tests_is_fine(tmp_path):
    src = _src(tmp_path, "cmd/server/main.go", "cmd/server/main_test.go")
    report = check([_epic(tmp_path, _op())], src)
    assert [e.state for e in report.entrypoints] == ["tested"]
    assert report.findings == []


def test_a_successful_probe_covers_an_untested_entrypoint(tmp_path):
    """#352 asks for the CONJUNCTION: no test AND no boot-and-hit coverage. An
    entrypoint whose routes just answered IS exercised."""
    report = check([_epic(tmp_path, _op(path="/health"))],
                   _src(tmp_path, "cmd/server/main.go"),
                   base_url="http://x", probe=_probe({"/health": 200}))
    assert [e.state for e in report.entrypoints] == ["covered_by_probe"]
    assert report.findings == []


def test_a_failed_probe_does_not_cover_an_untested_entrypoint(tmp_path):
    """A sweep where nothing answered is not coverage."""
    report = check([_epic(tmp_path, _op(path="/health"))],
                   _src(tmp_path, "cmd/server/main.go"),
                   base_url="http://x", probe=_probe({}, fail={"/health"}))
    assert [e.state for e in report.entrypoints] == ["untested_and_unbooted"]


def test_an_auth_gated_probe_still_counts_as_booted(tmp_path):
    report = check([_epic(tmp_path, _op(path="/admin"))],
                   _src(tmp_path, "cmd/server/main.go"),
                   base_url="http://x", probe=_probe({"/admin": 401}))
    assert [e.state for e in report.entrypoints] == ["covered_by_probe"]


def test_a_test_file_is_not_itself_reported_as_an_entrypoint(tmp_path):
    src = _src(tmp_path, "cmd/server/main.go", "cmd/server/main_test.go")
    report = check([_epic(tmp_path, _op())], src)
    assert [e.path for e in report.entrypoints] == ["cmd/server/main.go"]


# --- five-state discipline (#289) --------------------------------------------

def test_no_base_url_is_inapplicable_not_pass(tmp_path):
    """A green build and green package tests do not exercise the assembled
    binary — that IS the gate."""
    report = check([_epic(tmp_path, _op())], _src(tmp_path))
    assert report.outcome == "inapplicable"
    assert report.applicability == "inapplicable"


def test_no_declared_route_is_inapplicable(tmp_path):
    report = check([_epic(tmp_path)], _src(tmp_path), base_url="http://x")
    assert report.outcome == "inapplicable"


def test_unreadable_contracts_is_error(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text("{ not json")
    report = check([epic], _src(tmp_path), base_url="http://x")
    assert report.outcome == "error"


def test_counts_travel_with_denominators(tmp_path):
    report = check(
        [_epic(tmp_path,
               _op(path="/ok"), _op(path="/missing"),
               _op(method="POST", path="/create"), _op(path="/o/:id"),
               _op(path="/vendor", external=True))],
        _src(tmp_path), base_url="http://x",
        probe=_probe({"/ok": 200, "/missing": 404}))
    m = report.measured
    assert m["declared_operations"] == 5
    assert m["external_skipped"] == 1
    assert m["routes_considered"] == 4
    assert m["by_state"] == {SERVED: 1, NOT_SERVED: 1, NOT_PROBED: 1, UNPROBEABLE: 1}
    assert m["base_url_supplied"] is True


# --- malformed input ----------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {}, {"entities": "no"}, {"entities": [None, 3]},
    {"entities": [{"name": "B", "operations": None}]},
    {"entities": [{"name": "B", "operations": [None, 7]}]}, [],
])
def test_malformed_contracts_do_not_crash(tmp_path, payload):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text(json.dumps(payload))
    report = check([epic], _src(tmp_path), base_url="http://x")
    assert report.routes == []


def test_an_operation_without_a_path_is_skipped(tmp_path):
    report = check([_epic(tmp_path, {"name": "x", "method": "GET"})],
                   _src(tmp_path), base_url="http://x")
    assert report.routes == []


def test_a_source_tree_that_does_not_exist_does_not_crash(tmp_path):
    report = check([_epic(tmp_path, _op())], tmp_path / "nope", base_url="http://x",
                   probe=_probe({"/health": 200}))
    assert report.entrypoints == []


# --- CLI ----------------------------------------------------------------------

def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


def test_cli_without_base_url_says_inapplicable_and_exits_zero(tmp_path):
    result = _run(str(_epic(tmp_path, _op())), "--source", str(_src(tmp_path)))
    assert result.returncode == 0
    assert "INAPPLICABLE" in result.stdout
    assert "not a pass" in result.stdout


def test_cli_reports_the_entrypoint_finding(tmp_path):
    result = _run(str(_epic(tmp_path, _op())), "--source",
                  str(_src(tmp_path, "cmd/server/main.go")))
    assert "untested_and_unbooted" in result.stdout


def test_cli_findings_exit_zero_without_gate(tmp_path):
    """The report-only contract: findings are printed and do not block."""
    result = _run(str(_epic(tmp_path, _op())), "--source",
                  str(_src(tmp_path, "cmd/server/main.go")))
    assert result.returncode == 0
    assert "untested_and_unbooted" in result.stdout
    assert "report-only" in result.stdout


def test_cli_gate_blocks_on_the_entrypoint_finding(tmp_path):
    assert _run(str(_epic(tmp_path, _op())), "--source",
                str(_src(tmp_path, "cmd/server/main.go")), "--gate").returncode == 1


def test_cli_unreadable_contracts_exits_three_even_report_only(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text("{ not json")
    assert _run(str(epic), "--source", str(_src(tmp_path))).returncode == 3


def test_cli_missing_target_is_a_usage_error(tmp_path):
    assert _run(str(tmp_path / "nope"), "--source", str(tmp_path)).returncode == 2


def test_cli_malformed_path_param_is_a_usage_error(tmp_path):
    result = _run(str(_epic(tmp_path, _op())), "--source", str(_src(tmp_path)),
                  "--path-param", "noequals")
    assert result.returncode == 2


def test_cli_json_carries_outcome_and_measured(tmp_path):
    result = _run(str(_epic(tmp_path, _op())), "--source", str(_src(tmp_path)),
                  "--format", "json")
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "inapplicable"
    assert payload["measured"]["base_url_supplied"] is False
