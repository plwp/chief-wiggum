"""Tests for scripts/check_gate_validation.py — the gate-of-gates enforcing the
gate-validation protocol (docs/gate-validation.md, #168).

The threat model these tests exercise is "the validation record can be fooled":
copied records (wrong gate name), stale records (scanner_version drift), forged
records (passed flags contradicting results, non-zero findings marked clean),
and unjournaled records (no chain-verified ratchet provenance). None of those
may grant blocking authority.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import check_gate_validation as gv
from chief_wiggum.hashing import stable_hash

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
        "ratchet_record_id": "rec-00001",
    }
    record.update(overrides)
    return record


def _write_journal(state_dir: Path, entries: list[dict]) -> None:
    """Write a hash-chained ratchet journal, exactly as ratchet.py chains it."""
    prev = "genesis"
    lines = []
    for body in entries:
        body = dict(body)
        body.pop("record_hash", None)
        body["record_hash"] = stable_hash(prev, json.dumps(body, sort_keys=True))
        prev = body["record_hash"]
        lines.append(json.dumps(body, sort_keys=True))
    (state_dir / "ratchet-journal.jsonl").write_text("\n".join(lines) + "\n")


def _write_record(tmp_path: Path, gate: str, record: dict, journal: bool = True,
                  journal_ref: str | None = None, journal_event: str = "gate-validation") -> Path:
    """Write <gate>.json under <tmp>/quality/validation and, by default, a
    chain-valid journal entry corroborating its ratchet_record_id beside it."""
    state = tmp_path / "quality"
    vdir = state / "validation"
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / f"{gate}.json").write_text(json.dumps(record))
    if journal:
        _write_journal(state, [{
            "record_id": record.get("ratchet_record_id", "rec-00001"),
            "event": journal_event,
            "ref": journal_ref if journal_ref is not None else gate,
        }])
    return vdir


# --- check(): the happy path --------------------------------------------------


def test_missing_record_is_not_passing(tmp_path):
    report = gv.check("no_such_gate", tmp_path / "validation")
    assert report.record_found is False
    assert report.passing is False


def test_full_valid_record_passes(tmp_path):
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record())
    report = gv.check("example_gate", vdir)
    assert report.record_found is True
    assert report.schema_errors == []
    assert report.provenance_errors == []
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


# --- provenance: copied / stale / unjournaled records grant nothing -----------


def test_record_for_another_gate_fails(tmp_path):
    """A record copied from another gate's file (its `gate` field names someone
    else) must not grant blocking authority to the gate whose filename it wears."""
    record = _minimal_valid_record(gate="some_other_gate")
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert any("copied" in e or "gate field" in e for e in report.provenance_errors)
    assert report.passing is False


def test_missing_ratchet_record_id_fails(tmp_path):
    record = _minimal_valid_record()
    del record["ratchet_record_id"]
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert any("ratchet_record_id" in e for e in report.provenance_errors)
    assert report.passing is False


def test_missing_journal_fails(tmp_path):
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record(), journal=False)
    report = gv.check("example_gate", vdir)
    assert any("journal not found" in e for e in report.provenance_errors)
    assert report.passing is False


def test_record_id_absent_from_journal_fails(tmp_path):
    record = _minimal_valid_record(ratchet_record_id="rec-00099")
    vdir = _write_record(tmp_path, "example_gate", record)
    # journal helper wrote rec-00099... rewrite it with a different id
    _write_journal(tmp_path / "quality", [{
        "record_id": "rec-00001", "event": "gate-validation", "ref": "example_gate"}])
    report = gv.check("example_gate", vdir)
    assert any("not found in the ratchet journal" in e for e in report.provenance_errors)
    assert report.passing is False


def test_tampered_journal_fails_closed(tmp_path):
    """A journal entry whose content was edited without re-chaining (the classic
    lower-the-bar tamper) must fail the whole corroboration, not just that entry."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record())
    journal = tmp_path / "quality" / "ratchet-journal.jsonl"
    entry = json.loads(journal.read_text())
    entry["ref"] = "example_gate"  # no-op value, but the hash no longer matches...
    entry["notes"] = "edited after the fact"  # ...because of this
    journal.write_text(json.dumps(entry, sort_keys=True) + "\n")
    report = gv.check("example_gate", vdir)
    assert any("chain broken" in e for e in report.provenance_errors)
    assert report.passing is False


def test_journal_entry_for_wrong_gate_fails(tmp_path):
    """A journaled gate-validation entry whose ref names a DIFFERENT gate cannot
    corroborate this record — that would let one journal entry bless every record."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record(),
                         journal_ref="unrelated_gate")
    report = gv.check("example_gate", vdir)
    assert any("does not name gate" in e for e in report.provenance_errors)
    assert report.passing is False


def test_journal_entry_with_wrong_event_fails(tmp_path):
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record(),
                         journal_event="epic-close")
    report = gv.check("example_gate", vdir)
    assert any("expected 'gate-validation'" in e for e in report.provenance_errors)
    assert report.passing is False


def _fake_gate_script(scripts_dir: Path, gate: str, version: str) -> None:
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / f"{gate}.py").write_text(
        "import sys\n"
        "if '--scanner-version' in sys.argv:\n"
        f"    print({version!r})\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n"
    )


def test_stale_scanner_version_fails(tmp_path):
    """A record authored against an older scanner is stale — the live gate's
    --scanner-version output is the source of truth, not the record's claim."""
    record = _minimal_valid_record(scanner_version="old-version-hash")
    vdir = _write_record(tmp_path, "example_gate", record)
    scripts_dir = tmp_path / "scripts"
    _fake_gate_script(scripts_dir, "example_gate", "current-version-hash")
    report = gv.check("example_gate", vdir, scripts_dir=scripts_dir)
    assert any("scanner_version mismatch" in e for e in report.provenance_errors)
    assert report.passing is False


def test_matching_scanner_version_passes(tmp_path):
    record = _minimal_valid_record(scanner_version="current-version-hash")
    vdir = _write_record(tmp_path, "example_gate", record)
    scripts_dir = tmp_path / "scripts"
    _fake_gate_script(scripts_dir, "example_gate", "current-version-hash")
    report = gv.check("example_gate", vdir, scripts_dir=scripts_dir)
    assert report.provenance_errors == []
    assert report.passing is True


def test_record_omitting_scanner_version_fails_when_live_available(tmp_path):
    """When the gate exposes a live --scanner-version, a record that omits its own
    is unverifiable-by-omission — it must not pass."""
    record = _minimal_valid_record()
    record.pop("scanner_version", None)
    vdir = _write_record(tmp_path, "example_gate", record)
    scripts_dir = tmp_path / "scripts"
    _fake_gate_script(scripts_dir, "example_gate", "current-version-hash")
    report = gv.check("example_gate", vdir, scripts_dir=scripts_dir)
    assert any("scanner_version mismatch" in e for e in report.provenance_errors)
    assert report.passing is False


def test_gate_without_scanner_version_support_skips_the_check(tmp_path):
    """No live version to compare against (script absent / flag unsupported) —
    the check is skipped, not failed; the journal still anchors provenance."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record())
    report = gv.check("example_gate", vdir, scripts_dir=tmp_path / "no-scripts-here")
    assert report.provenance_errors == []
    assert report.passing is True


# --- trials: pass/fail is derived, never trusted ------------------------------


def test_forged_passed_flag_fails(tmp_path):
    """`passed: true` on a trial whose result contradicts its expectation is a
    forgery — the checker derives pass/fail from result vs expected."""
    record = _minimal_valid_record()
    record["seeded_defect_trials"][0]["result"] = "not-fired"  # expected: fire
    # passed stays (forged) True
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert len(report.failed_trials) == 1
    assert "direct" in report.missing_seed_classes  # the forged trial doesn't count
    assert report.passing is False


def test_failed_trial_marks_not_passing(tmp_path):
    record = _minimal_valid_record()
    record["seeded_defect_trials"][0]["passed"] = False
    record["seeded_defect_trials"][0]["result"] = "not-fired"
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert len(report.failed_trials) == 1
    assert report.passing is False


def test_no_fire_trial_that_actually_fired_fails_even_with_forged_passed(tmp_path):
    record = _minimal_valid_record()
    record["seeded_defect_trials"][3]["result"] = "fired"  # expected: no-fire
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert len(report.failed_trials) == 1
    assert report.passing is False


def test_trial_genuinely_passed_derivation():
    assert gv.trial_genuinely_passed({"expected": "fire", "result": "fired", "passed": True})
    assert gv.trial_genuinely_passed({"expected": "no-fire", "result": "not-fired", "passed": True})
    # forged flags don't count
    assert not gv.trial_genuinely_passed({"expected": "fire", "result": "not-fired", "passed": True})
    assert not gv.trial_genuinely_passed({"expected": "no-fire", "result": "fired", "passed": True})
    # a truthful passed: false doesn't count either
    assert not gv.trial_genuinely_passed({"expected": "fire", "result": "fired", "passed": False})


# --- mandatory seed classes ---------------------------------------------------


def test_missing_direct_class_fails(tmp_path):
    record = _minimal_valid_record()
    record["seeded_defect_trials"] = [
        t for t in record["seeded_defect_trials"] if t["seed_class"] != "direct"
    ]
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert "direct" in report.missing_seed_classes
    assert report.passing is False


def test_missing_mandatory_evasion_class_fails(tmp_path):
    record = _minimal_valid_record()
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


# --- clean-corpus runs: derived too -------------------------------------------


def test_clean_corpus_run_with_nonzero_findings_fails(tmp_path):
    """`passed: true` on a run with findings != 0 is a forgery — clean means zero."""
    record = _minimal_valid_record()
    record["clean_corpus_runs"][0]["findings"] = 2  # passed stays (forged) True
    vdir = _write_record(tmp_path, "example_gate", record)
    report = gv.check("example_gate", vdir)
    assert len(report.failed_clean_runs) == 1
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


def test_validation_dir_is_defined_once_and_imported():
    """INV-fh-004: docs/quality/validation is defined in exactly one place —
    factory_log.DEFAULT_VALIDATION_DIR — and check_gate_validation IMPORTS it.
    The two constants were previously separate definitions that had already
    drifted in form (absolute vs relative), so identity (not mere equality) is
    asserted: an equality-by-accident re-definition would regress the bug."""
    # @cw-trace verifies INV-fh-004
    import factory_log

    assert gv.DEFAULT_VALIDATION_DIR is factory_log.DEFAULT_VALIDATION_DIR
    assert Path(gv.DEFAULT_VALIDATION_DIR).is_absolute()
    assert Path(gv.DEFAULT_VALIDATION_DIR).parts[-3:] == ("docs", "quality", "validation")
