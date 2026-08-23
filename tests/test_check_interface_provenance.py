"""Tests for the external-interface provenance gate (chief-wiggum#350)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_interface_provenance.py"
sys.path.insert(0, str(REPO / "scripts"))

from check_interface_provenance import (  # noqa: E402
    VERIFIED_SOURCE_TYPES,
    Report,
    main,
    scan,
)


def _contracts(*operations: dict) -> dict:
    return {"entities": [{"name": "Booking", "operations": list(operations)}]}


def _op(**over) -> dict:
    base = {"name": "Fetch venue info", "method": "GET", "path": "/api/gx-agent/venue-info"}
    base.update(over)
    return base


def _write(tmp_path: Path, payload: dict, name: str = "contracts.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


# --- the defect this gate exists for -----------------------------------------

def test_external_operation_derived_only_from_a_ticket_is_a_finding(tmp_path):
    """The original escape: schemas and routes faithfully derived from the
    ticket, and nobody ever looked at the real system."""
    _write(tmp_path, _contracts(_op(
        external=True,
        external_system="SCP",
        derived_from=[{"type": "ticket", "ref": "#42"}],
    )))
    report = scan([tmp_path])
    assert len(report.findings) == 1
    assert report.findings[0].external_system == "SCP"
    assert report.findings[0].cited_types == ["ticket"]
    assert report.outcome == "findings"


@pytest.mark.parametrize("kind", sorted(VERIFIED_SOURCE_TYPES))
def test_verification_grade_provenance_clears_the_operation(tmp_path, kind):
    _write(tmp_path, _contracts(_op(
        external=True, derived_from=[{"type": kind, "ref": "captured 2026-08-01"}],
    )))
    report = scan([tmp_path])
    assert report.findings == []
    assert report.measured["external_sourced"] == 1
    assert report.outcome == "pass"


@pytest.mark.parametrize("kind", ["ticket", "acceptance_criterion", "user_input", "epic_invariant"])
def test_request_grade_provenance_does_not_clear_the_operation(tmp_path, kind):
    """These record who ASKED for the interface, never that anyone saw it."""
    _write(tmp_path, _contracts(_op(external=True, derived_from=[{"type": kind, "ref": "x"}])))
    assert len(scan([tmp_path]).findings) == 1


def test_a_verified_cite_alongside_request_grade_ones_still_clears(tmp_path):
    _write(tmp_path, _contracts(_op(external=True, derived_from=[
        {"type": "ticket", "ref": "#42"},
        {"type": "observed_fact", "ref": "curl capture"},
    ])))
    assert scan([tmp_path]).findings == []


def test_provenance_is_not_inherited_from_the_entity(tmp_path):
    """One api_doc cite at entity level must not cover twenty guessed routes —
    that IS the shape of the original defect."""
    _write(tmp_path, {"entities": [{
        "name": "Booking",
        "derived_from": [{"type": "api_doc", "ref": "https://vendor/docs"}],
        "operations": [_op(external=True), _op(external=True, path="/api/gx-agent/bookings")],
    }]})
    assert len(scan([tmp_path]).findings) == 2


# --- the marker escape hatch --------------------------------------------------

@pytest.mark.parametrize("text", [
    "TBD: confirm the real route against SCP",
    "UNRESOLVED: which envelope does this return?",
    "UNVERIFIED whether this path exists",
])
def test_an_unresolved_marker_stands_this_gate_down(tmp_path, text):
    """check_unresolved.py already blocks dependent work on a marker; counting
    it here too would report one unknown twice."""
    _write(tmp_path, _contracts(_op(external=True, description=text)))
    report = scan([tmp_path])
    assert report.findings == []
    assert report.measured["external_marked_unresolved"] == 1


def test_a_marker_is_found_anywhere_under_the_operation(tmp_path):
    _write(tmp_path, _contracts(_op(
        external=True,
        error_cases=[{"status": 404, "condition": "TBD: unknown error envelope"}],
    )))
    assert scan([tmp_path]).findings == []


def test_lowercase_tbd_in_prose_is_not_a_marker(tmp_path):
    """Precision: a lowercase 'tbd' must not silently excuse a guessed route."""
    _write(tmp_path, _contracts(_op(external=True, description="route tbd maybe")))
    assert len(scan([tmp_path]).findings) == 1


# --- scope: only declared-external operations are checked ---------------------

def test_own_api_operations_are_never_findings(tmp_path):
    _write(tmp_path, _contracts(_op(), _op(name="Create", method="POST", path="/api/b")))
    report = scan([tmp_path])
    assert report.findings == []
    assert report.measured["operations_total"] == 2
    assert report.measured["external_declared"] == 0


@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_only_a_literal_true_declares_an_operation_external(tmp_path, value):
    """A truthy string must not enrol an operation, and must not exempt one."""
    _write(tmp_path, _contracts(_op(external=value)))
    assert scan([tmp_path]).measured["external_declared"] == 0


# --- the declaration gap ------------------------------------------------------

def test_operations_but_none_external_is_a_named_gap_not_a_clean_pass(tmp_path):
    """Zero of zero verified is not evidence. Reporting it as `pass` is the
    vacuous-pass shape (#289) — found on the #350 dry-run, where 15 of 22 real
    epics reported `pass` while this gate checked nothing at all."""
    _write(tmp_path, _contracts(_op()))
    report = scan([tmp_path])
    assert report.declaration_gap is True
    assert report.measured["declaration_gap"] is True
    assert report.outcome == "inapplicable"
    assert report.applicability == "inapplicable"


def test_the_declaration_gap_is_never_reported_as_pass_via_the_cli(tmp_path):
    _write(tmp_path, _contracts(_op()))
    result = _run(str(tmp_path), "--format", "json")
    assert json.loads(result.stdout)["outcome"] == "inapplicable"
    assert result.returncode == 0


def test_the_gap_survives_under_gate_without_blocking(tmp_path):
    """--gate must not turn a nothing-checked run into a block; it also must
    not turn it into a pass."""
    _write(tmp_path, _contracts(_op()))
    result = _run(str(tmp_path), "--gate", "--format", "json")
    assert result.returncode == 0
    assert json.loads(result.stdout)["outcome"] == "inapplicable"


def test_one_declared_external_operation_makes_the_gate_applicable(tmp_path):
    _write(tmp_path, _contracts(
        _op(),
        _op(path="/vendor", external=True, derived_from=[{"type": "api_doc", "ref": "d"}]),
    ))
    report = scan([tmp_path])
    assert report.applicability == "applicable"
    assert report.outcome == "pass"


def test_declaring_an_external_operation_closes_the_gap(tmp_path):
    _write(tmp_path, _contracts(_op(external=True, derived_from=[{"type": "api_doc", "ref": "d"}])))
    assert scan([tmp_path]).declaration_gap is False


def test_no_operations_at_all_is_not_a_declaration_gap(tmp_path):
    """Nothing to declare is inapplicable, a different state from 'declared
    things and named none of them external'."""
    _write(tmp_path, {"entities": [{"name": "Booking", "operations": []}]})
    report = scan([tmp_path])
    assert report.declaration_gap is False
    assert report.outcome == "inapplicable"


def test_the_gap_note_is_printed_and_says_inapplicable(tmp_path, capsys):
    _write(tmp_path, _contracts(_op()))
    main([str(tmp_path)])
    out = capsys.readouterr().out
    assert "declaration gap" in out
    assert "INAPPLICABLE" in out
    assert "not a pass" in out
    # The gap branch must be the LAST word. Falling through to the generic
    # inapplicable branch would append "contracts.json declares no operations",
    # which is false here — it declares one — and would contradict the
    # denominator printed two lines above it.
    assert "declares no operations" not in out


def test_no_operations_says_so_rather_than_claiming_a_declaration_gap(tmp_path, capsys):
    _write(tmp_path, {"entities": [{"name": "Booking", "operations": []}]})
    main([str(tmp_path)])
    out = capsys.readouterr().out
    assert "declares no operations" in out
    assert "declaration gap" not in out


# --- five-state discipline (#289) --------------------------------------------

def test_no_contracts_file_is_inapplicable_not_pass(tmp_path):
    (tmp_path / "invariants.md").write_text("# nothing to see")
    report = scan([tmp_path])
    assert report.outcome == "inapplicable"
    assert report.measured["files_scanned"] == 0


def test_unreadable_contracts_file_is_error_not_pass(tmp_path):
    (tmp_path / "contracts.json").write_text("{ not json")
    report = scan([tmp_path])
    assert report.outcome == "error"
    assert report.applicability == "error"
    assert report.measured["files_unparsed"] == 1
    assert report.measured["files_scanned"] == 0


def test_a_clean_scan_of_declared_external_operations_is_pass(tmp_path):
    _write(tmp_path, _contracts(_op(external=True, derived_from=[{"type": "api_doc", "ref": "d"}])))
    assert scan([tmp_path]).outcome == "pass"


def test_every_count_travels_with_its_denominator(tmp_path):
    _write(tmp_path, _contracts(
        _op(external=True, derived_from=[{"type": "ticket", "ref": "#1"}]),
        _op(external=True, path="/b", derived_from=[{"type": "api_doc", "ref": "d"}]),
        _op(external=True, path="/c", description="TBD: unknown"),
        _op(path="/own"),
    ))
    m = scan([tmp_path]).measured
    assert m["operations_total"] == 4
    assert m["external_declared"] == 3
    assert m["external_sourced"] == 1
    assert m["external_marked_unresolved"] == 1
    assert m["external_unsourced"] == 1


# --- malformed input must not crash the scanner ------------------------------

@pytest.mark.parametrize("payload", [
    {},
    {"entities": "not a list"},
    {"entities": [None, 3, "x"]},
    {"entities": [{"name": "B", "operations": "not a list"}]},
    {"entities": [{"name": "B", "operations": [None, 7]}]},
    [],
])
def test_malformed_contracts_do_not_crash(tmp_path, payload):
    _write(tmp_path, payload)
    report = scan([tmp_path])
    assert isinstance(report, Report)
    assert report.findings == []


def test_derived_from_of_the_wrong_shape_is_treated_as_no_cite(tmp_path):
    _write(tmp_path, _contracts(_op(external=True, derived_from=["api_doc", None, 3])))
    assert len(scan([tmp_path]).findings) == 1


def test_derived_from_null_is_treated_as_no_cite(tmp_path):
    _write(tmp_path, _contracts(_op(external=True, derived_from=None)))
    assert len(scan([tmp_path]).findings) == 1


# --- CLI: report-only by default ---------------------------------------------

def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


def test_cli_findings_exit_zero_without_gate(tmp_path):
    _write(tmp_path, _contracts(_op(external=True, derived_from=[{"type": "ticket", "ref": "#1"}])))
    result = _run(str(tmp_path))
    assert result.returncode == 0
    assert "report-only" in result.stdout
    assert "UNSOURCED EXTERNAL INTERFACES" in result.stdout


def test_cli_findings_exit_one_under_gate(tmp_path):
    _write(tmp_path, _contracts(_op(external=True, derived_from=[{"type": "ticket", "ref": "#1"}])))
    assert _run(str(tmp_path), "--gate").returncode == 1


def test_cli_clean_exits_zero_under_gate(tmp_path):
    _write(tmp_path, _contracts(_op(external=True, derived_from=[{"type": "api_doc", "ref": "d"}])))
    assert _run(str(tmp_path), "--gate").returncode == 0


def test_cli_unreadable_artifact_exits_three_even_report_only(tmp_path):
    """Report-only means findings do not block; it never means the scanner may
    fail silently."""
    (tmp_path / "contracts.json").write_text("{ not json")
    result = _run(str(tmp_path))
    assert result.returncode == 3
    assert "could NOT be read" in result.stdout


def test_cli_missing_target_is_a_usage_error(tmp_path):
    assert _run(str(tmp_path / "nope")).returncode == 2


def test_cli_json_carries_outcome_and_denominators(tmp_path):
    _write(tmp_path, _contracts(_op(external=True, derived_from=[{"type": "ticket", "ref": "#1"}])))
    result = _run(str(tmp_path), "--format", "json")
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "findings"
    assert payload["gating"] is False
    assert payload["measured"]["external_declared"] == 1
    assert payload["count"] == 1


def test_cli_reports_inapplicable_rather_than_ok(tmp_path):
    (tmp_path / "notes.md").write_text("nothing")
    result = _run(str(tmp_path))
    assert result.returncode == 0
    assert "INAPPLICABLE" in result.stdout
    assert "not a pass" in result.stdout


# --- the schema admits the field ---------------------------------------------

def test_operation_schema_declares_external_and_is_not_required():
    schema = json.loads((REPO / "templates/formal-models/contracts-schema.json").read_text())
    operation = schema["$defs"]["operation"]
    assert "external" in operation["properties"]
    assert operation["properties"]["external"]["type"] == "boolean"
    assert "external_system" in operation["properties"]
    # Required would force an author to answer before they know. The gate's
    # declaration-gap note is what covers the omission instead.
    assert "external" not in operation["required"]


def test_a_declared_external_operation_validates_against_the_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((REPO / "templates/formal-models/contracts-schema.json").read_text())
    operation = _op(external=True, external_system="Stripe",
                    derived_from=[{"type": "api_doc", "ref": "https://stripe.com/docs"}])
    jsonschema.validate(operation, {**schema["$defs"]["operation"], "$defs": schema["$defs"]})
