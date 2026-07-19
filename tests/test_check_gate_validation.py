"""Tests for scripts/check_gate_validation.py — the gate-of-gates enforcing the
gate-validation protocol (docs/gate-validation.md, #168)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import check_gate_validation as gv

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _minimal_valid_record(**overrides) -> dict:
    record = {
        "gate": "example_gate",
        "protocol_version": "1",
        "telemetry_dependent": False,
        "concurrency_applicable": False,
        "concurrency_note": "static analysis has no concurrent dimension",
        "authority_boundary": {
            "proves": "the gate catches the defect it claims to",
            "artifact": "a git worktree copy of a shipped repo",
            "assumptions": ["the metadata is well-formed"],
        },
        "seeded_defect_trials": [
            {"seed_id": "d1", "seed_class": "direct", "repo": "r", "expected": "fire",
             "result": "fired", "passed": True},
            {"seed_id": "o1", "seed_class": "evasion-omission", "repo": "r", "expected": "fire",
             "result": "fired", "passed": True},
            {"seed_id": "c1", "seed_class": "evasion-config-indirection", "repo": "r",
             "expected": "fire", "result": "fired", "passed": True},
            {"seed_id": "s1", "seed_class": "evasion-sampling-gap", "repo": "r",
             "expected": "no-fire", "result": "not-fired", "passed": True},
        ],
        "clean_corpus_runs": [
            {"repo": "r", "sha": "abc123", "findings": 0,
             "coverage": {"writers_found": 3}, "passed": True},
        ],
        "status": "passed",
    }
    record.update(overrides)
    return record


def _write_record(tmp_path, gate: str, record: dict) -> Path:
    d = tmp_path / "validation"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{gate}.json"
    path.write_text(json.dumps(record))
    return d


# --- check() ------------------------------------------------------------------


def test_missing_record_is_not_passing(tmp_path):
    report = gv.check("no_such_gate", tmp_path / "validation")
    assert report.record_found is False
    assert report.passing is False


def test_full_valid_record_passes(tmp_path):
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record())
    report = gv.check("example_gate", vdir)
    assert report.record_found is True
    assert report.schema_errors == []
    assert report.missing_seed_classes == []
    assert report.failed_trials == []
    assert report.failed_clean_runs == []
    assert report.passing is True


def test_schema_errors_when_authority_boundary_missing(tmp_path):
    record = _minimal_valid_record()
    del record["authority_boundary"]
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert report.record_found is True
    assert report.schema_errors
    assert report.passing is False


def test_missing_mandatory_evasion_class_fails(tmp_path):
    record = _minimal_valid_record()
    # Drop the evasion-sampling-gap trial — a mandatory class.
    record["seeded_defect_trials"] = [
        t for t in record["seeded_defect_trials"] if t["seed_class"] != "evasion-sampling-gap"
    ]
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert "evasion-sampling-gap" in report.missing_seed_classes
    assert report.passing is False


def test_concurrency_not_required_when_inapplicable(tmp_path):
    record = _minimal_valid_record()  # concurrency_applicable already False
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert "evasion-concurrency" not in report.missing_seed_classes
    assert report.passing is True


def test_concurrency_required_when_applicable(tmp_path):
    record = _minimal_valid_record(concurrency_applicable=True)
    del record["concurrency_note"]
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert "evasion-concurrency" in report.missing_seed_classes
    assert report.passing is False


def test_instrumentation_deleted_required_when_telemetry_dependent(tmp_path):
    record = _minimal_valid_record(telemetry_dependent=True)
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert "instrumentation-deleted" in report.missing_seed_classes
    assert report.passing is False

    # adding a passing instrumentation-deleted trial satisfies it
    record["seeded_defect_trials"].append(
        {"seed_id": "i1", "seed_class": "instrumentation-deleted", "repo": "r",
         "expected": "fire", "result": "fired", "passed": True}
    )
    vdir2 = _write_record(tmp_path / "v2", "example_gate", record)
    report2 = gv.check("example_gate", vdir2)
    assert "instrumentation-deleted" not in report2.missing_seed_classes
    assert report2.passing is True


def test_failed_trial_marks_not_passing(tmp_path):
    record = _minimal_valid_record()
    record["seeded_defect_trials"][0]["passed"] = False
    record["seeded_defect_trials"][0]["result"] = "not-fired"
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert len(report.failed_trials) == 1
    assert report.passing is False


def test_clean_corpus_run_with_no_coverage_fails(tmp_path):
    record = _minimal_valid_record()
    record["clean_corpus_runs"][0]["coverage"] = {}
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert len(report.failed_clean_runs) == 1
    assert report.passing is False


def test_clean_corpus_run_with_all_zero_coverage_fails(tmp_path):
    record = _minimal_valid_record()
    record["clean_corpus_runs"][0]["coverage"] = {"writers_found": 0}
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert len(report.failed_clean_runs) == 1
    assert report.passing is False


def test_status_field_must_be_passed(tmp_path):
    record = _minimal_valid_record(status="failed")
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert report.passing is False


def test_no_fire_trial_that_actually_fired_fails():
    """A trial's `passed` field is the source of truth (result == expected);
    check() trusts it rather than re-deriving — the record itself must set
    passed correctly when authoring it."""
    trial = {"seed_id": "x", "seed_class": "evasion-sampling-gap", "repo": "r",
              "expected": "no-fire", "result": "fired", "passed": False}
    assert trial["passed"] is False


# --- CLI ------------------------------------------------------------------


def test_cli_report_only_exits_0_even_when_not_passing(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_gate_validation.py"), "no_such_gate",
         "--validation-dir", str(tmp_path / "validation"), "--format", "json"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["passing"] is False


def test_cli_gate_mode_exits_1_when_not_passing(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_gate_validation.py"), "no_such_gate",
         "--validation-dir", str(tmp_path / "validation"), "--gate", "--format", "json"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1


def test_cli_gate_mode_exits_0_when_passing(tmp_path):
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record())
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_gate_validation.py"), "example_gate",
         "--validation-dir", str(vdir), "--gate", "--format", "json"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["passing"] is True


def test_cli_text_format_reports_verdict(tmp_path):
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record())
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_gate_validation.py"), "example_gate",
         "--validation-dir", str(vdir)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert "PASSING" in proc.stdout
