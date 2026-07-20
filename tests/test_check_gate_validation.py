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


# --- IT-fh-06: stale-while-blocking auto-demotion (chief-wiggum#198) ----------
#
# Blocking authority is a JOURNALED fact (a `gate-authority` wire/unwire event
# in the ratchet hash chain), never a forgeable sidecar file. "Was this gate
# blocking?" is read from those events over the verified chain prefix; the
# current-blocking verdict is `last event == wire AND check() passing now`. A
# journaled-wired gate whose record is NOT currently passing (stale scanner,
# broken chain, missing/invalid record) is demoted (fail-to-report-only) and
# emits the generic DEMOTION — reading "was blocking" from the journal means the
# demotion fires even when the CURRENT record/chain is what broke.
# @cw-trace verifies IT-fh-06 INV-fh-003 INV-fh-005


def _journal(vdir):
    return Path(vdir).resolve().parent / "ratchet-journal.jsonl"


def test_failure_kind_classifies_stale_vs_invalid(tmp_path):
    """A record that would otherwise pass except for scanner/journal drift is
    'stale'; anything else non-passing (missing record, schema errors, forged/
    failed trials or clean runs, wrong status, a copied/unjournaled record) is
    'invalid' — the model's two distinct demotion triggers (G-008 vs G-014)."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record(scanner_version="old"))
    scripts_dir = tmp_path / "scripts"
    _fake_gate_script(scripts_dir, "example_gate", "current")
    stale_report = gv.check("example_gate", vdir, scripts_dir=scripts_dir)
    assert gv.failure_kind(stale_report) == "stale"

    missing_report = gv.check("no_such_gate", vdir)
    assert gv.failure_kind(missing_report) == "invalid"

    record = _minimal_valid_record()
    record["seeded_defect_trials"][0]["passed"] = False
    record["seeded_defect_trials"][0]["result"] = "not-fired"
    vdir2 = _write_record(tmp_path / "v2", "example_gate", record)
    invalid_report = gv.check("example_gate", vdir2)
    assert gv.failure_kind(invalid_report) == "invalid"

    passing_report = gv.check("example_gate", _write_record(tmp_path / "v3", "example_gate", _minimal_valid_record()))
    assert gv.failure_kind(passing_report) is None


def test_stale_while_blocking_auto_demotes(tmp_path):
    """Author a passing record, --wire it (journals a wire event), then edit a
    hashed dependency so the live --scanner-version changes. The NEXT check must
    transition blocking -> demoted (never silently stay blocking), emit the
    generic DEMOTION with details='stale' (no seed_class), and report
    previous_authority='blocking' read from the journaled wire event."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record(scanner_version="v1"))
    scripts_dir = tmp_path / "scripts"
    _fake_gate_script(scripts_dir, "example_gate", "v1")

    report, transition = gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir, wire=True)
    assert report.passing is True
    assert transition.new_state == "blocking"
    assert gv.ratchet_last_authority_action(_journal(vdir), "example_gate") == "wire"

    _fake_gate_script(scripts_dir, "example_gate", "v2-after-scanner-edit")

    report2, transition2 = gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir)
    assert report2.passing is False
    assert gv.failure_kind(report2) == "stale"
    assert transition2.new_state == "demoted"
    assert transition2.demoted is True
    assert transition2.demotion_reason == "stale"
    assert transition2.previous_authority == "blocking"
    assert transition2.instruction and "stale" in transition2.instruction


def test_record_missing_while_blocking_demotes(tmp_path):
    """The record-missing variant of the same edge (G-014): a record deleted out
    from under a journaled-wired gate must demote, not silently keep blocking."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record())
    _, transition = gv.check_and_transition("example_gate", vdir, wire=True)
    assert transition.new_state == "blocking"

    (Path(vdir) / "example_gate.json").unlink()

    report2, transition2 = gv.check_and_transition("example_gate", vdir)
    assert report2.passing is False
    assert report2.record_found is False
    assert transition2.new_state == "demoted"
    assert transition2.demoted is True
    assert transition2.demotion_reason == "record_missing"
    assert transition2.previous_authority == "blocking"


def test_schema_invalid_while_blocking_demotes_as_record_missing(tmp_path):
    """The schema-invalid variant: the record file still exists but no longer
    validates. Classified the same as 'missing' — it grants no blocking
    authority to a journaled-wired gate."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record())
    _, transition = gv.check_and_transition("example_gate", vdir, wire=True)
    assert transition.new_state == "blocking"

    broken = _minimal_valid_record()
    del broken["authority_boundary"]
    (Path(vdir) / "example_gate.json").write_text(json.dumps(broken))

    report2, transition2 = gv.check_and_transition("example_gate", vdir)
    assert report2.schema_errors
    assert transition2.new_state == "demoted"
    assert transition2.demotion_reason == "record_missing"
    assert transition2.previous_authority == "blocking"


def test_chain_broken_while_blocking_still_demotes(tmp_path):
    """FINDING 1 REGRESSION: a broken journal chain is itself a stale condition
    that must demote a wired gate. Because "was wired" is read from the verified
    chain PREFIX (the wire event survives a LATER tamper), the demotion still
    fires — the exact case the fail-closed sidecar masked (it returned unknown
    on a broken chain and silently downgraded with no DEMOTION)."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record())
    _, transition = gv.check_and_transition("example_gate", vdir, wire=True)
    assert transition.new_state == "blocking"

    # Append a tamper entry AFTER the wire event: the chain breaks at the tail,
    # but the earlier wire event is still in the verified prefix.
    journal = _journal(vdir)
    lines = journal.read_text().splitlines()
    lines.append(json.dumps({"record_id": "rec-99", "event": "x", "record_hash": "deadbeef"}))
    journal.write_text("\n".join(lines) + "\n")

    report2, transition2 = gv.check_and_transition("example_gate", vdir)
    assert report2.passing is False  # check() reports the broken chain
    assert gv.ratchet_last_authority_action(journal, "example_gate") == "wire"  # survives the tamper
    assert transition2.new_state == "demoted"
    assert transition2.demoted is True
    assert transition2.previous_authority == "blocking"


def test_re_journaled_new_rid_recovery_reaches_blocking_not_stuck(tmp_path):
    """FINDING 2 REGRESSION: real re-derivation normally produces a NEW rid.
    Because blocking authority is read from journaled wire EVENTS (not by
    matching the wire event's rid to the record's), a demoted gate re-authored
    with a fresh rid recovers cleanly: the gate is still journaled-wired, the new
    record passes, so the verdict is 'blocking' again — never stuck-untrusted,
    never a spurious straight-to-blocking bypass of a rid check that no longer
    exists."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record(scanner_version="v1"))
    scripts_dir = tmp_path / "scripts"
    _fake_gate_script(scripts_dir, "example_gate", "v1")
    gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir, wire=True)

    _fake_gate_script(scripts_dir, "example_gate", "v2-drifted")
    _, demoted = gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir)
    assert demoted.new_state == "demoted"

    # Re-author against the current scanner with a DIFFERENT rid, journaled anew.
    fixed = _minimal_valid_record(scanner_version="v2-drifted", ratchet_record_id="rec-00002")
    (Path(vdir) / "example_gate.json").write_text(json.dumps(fixed))
    _write_journal(tmp_path / "quality", [
        {"record_id": "rec-00001", "event": "gate-validation", "ref": "example_gate"},
        {"record_id": "rec-00002", "event": "gate-validation", "ref": "example_gate"},
    ])
    # (the wire event was in the prior chain; re-establish it after the rewrite)
    gv._append_authority(_journal(vdir), "example_gate", "wire", wired_rid="rec-00001")

    report, transition = gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir)
    assert report.passing is True
    assert transition.new_state == "blocking"
    assert transition.demoted is False


def test_stale_while_not_wired_downgrades_not_demotes(tmp_path):
    """A record that goes stale while the gate is NOT journaled-wired downgrades
    to report_only — no demotion event, since nothing was blocking (G-015). A
    plain check never journals a wire, so a never-wired gate simply reports."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record(scanner_version="v1"))
    scripts_dir = tmp_path / "scripts"
    _fake_gate_script(scripts_dir, "example_gate", "v1")

    _, t1 = gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir)
    assert t1.new_state == "validated"  # passing, not wired

    _fake_gate_script(scripts_dir, "example_gate", "v2-drifted")
    report2, t2 = gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir)
    assert report2.passing is False
    assert t2.new_state == "report_only"
    assert t2.demoted is False
    assert t2.demotion_reason is None


def test_unwire_then_stale_does_not_demote(tmp_path):
    """An operator who intentionally un-wires a gate (journaled unwire) is no
    longer blocking, so a later stale record downgrades rather than demoting —
    unwire is the clean voluntary edge when the record still passes."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record(scanner_version="v1"))
    scripts_dir = tmp_path / "scripts"
    _fake_gate_script(scripts_dir, "example_gate", "v1")
    gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir, wire=True)
    _, unwired = gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir, unwire=True)
    assert unwired.new_state == "validated"
    assert gv.ratchet_last_authority_action(_journal(vdir), "example_gate") == "unwire"

    _fake_gate_script(scripts_dir, "example_gate", "v2-drifted")
    _, t = gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir)
    assert t.new_state == "report_only"
    assert t.demoted is False


def test_forged_authority_sidecar_asserts_nothing(tmp_path):
    """FINDING 3 REGRESSION: authority now comes ONLY from journaled wire events
    in the verified chain — a hand-written <gate>.authority.json (or any loose
    file) asserts nothing and cannot manufacture a false demotion. Dropping the
    bare sidecar as a trust source is the fix."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record())
    # Forge a blocking sidecar the OLD design would have read.
    (Path(vdir) / "example_gate.authority.json").write_text(json.dumps(
        {"gate": "example_gate", "authority": "blocking", "previous_authority": "validated",
         "ratchet_record_id": "rec-00001"}))
    # No journaled wire event exists -> the gate is NOT blocking.
    assert gv.ratchet_last_authority_action(_journal(vdir), "example_gate") is None
    _, transition = gv.check_and_transition("example_gate", vdir)
    assert transition.new_state == "validated"  # passing, not wired — sidecar ignored
    assert transition.demoted is False


def test_no_trust_write_on_plain_or_missing_gate_checks(tmp_path):
    """FINDING 4 REGRESSION + P5: no ad-hoc/plain check and no missing-gate op
    writes anything trust-bearing. Only --wire/--unwire append journal events;
    plain checks and no-record ops leave the validation dir untouched (beyond the
    record/journal the test itself seeded)."""
    # No record at all: nothing is created.
    empty = tmp_path / "empty"
    empty.mkdir()
    gv.check_and_transition("no_such_gate", empty)
    assert list(empty.iterdir()) == []
    gv.check_and_transition("no_such_gate", empty, unwire=True)  # missing-gate unwire
    # unwire appends to the (nonexistent) journal beside empty -> it is the
    # journal, not the validation dir, and only for an explicit op; the
    # validation dir stays empty.
    assert list(empty.iterdir()) == []

    # Plain check of a passing record: no authority file appears.
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record())
    before = sorted(pp.name for pp in Path(vdir).iterdir())
    gv.check_and_transition("example_gate", vdir)  # plain
    after = sorted(pp.name for pp in Path(vdir).iterdir())
    assert before == after  # no <gate>.authority.json, nothing new


def test_wire_non_passing_never_yields_or_journals_blocking(tmp_path):
    """FINDING/P1: --wire with a NON-passing record must never reach 'blocking'
    and must not journal a wire event (INV-fh-003). On a fresh no-record gate it
    refuses, stays off 'blocking', and writes nothing."""
    vdir = tmp_path / "validation"
    vdir.mkdir()
    report, transition = gv.check_and_transition("no_such_gate", vdir, wire=True)
    assert report.passing is False
    assert transition.new_state != "blocking"
    assert "refused" in (transition.instruction or "")
    assert gv.ratchet_last_authority_action(_journal(vdir), "no_such_gate") is None


def test_wire_on_stale_blocking_record_demotes_never_stays_blocking(tmp_path):
    """P1: calling --wire again on a journaled-wired gate whose record has gone
    stale must NOT keep it blocking — a non-passing --wire journals no wire,
    surfaces the demotion (blocking -> demoted) and emits, and reports the
    refusal. Wiring blocking authority requires passing==true."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record(scanner_version="v1"))
    scripts_dir = tmp_path / "scripts"
    _fake_gate_script(scripts_dir, "example_gate", "v1")
    gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir, wire=True)

    _fake_gate_script(scripts_dir, "example_gate", "v2-drifted")
    report, transition = gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir, wire=True)
    assert report.passing is False
    assert transition.new_state == "demoted"
    assert transition.demoted is True
    assert transition.demotion_reason == "stale"
    assert transition.previous_authority == "blocking"
    assert "refused" in (transition.instruction or "")


def test_unwire_from_stale_blocking_emits_demotion_not_silent_report_only(tmp_path, monkeypatch):
    """P1: --unwire must not mask a stale/missing-while-blocking demotion. From a
    journaled-wired gate with a NON-passing (stale) record, --unwire surfaces and
    EMITS the demotion (read from the PRE-unwire journal) while still recording
    the unwire."""
    log_path = tmp_path / "factory-log.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log_path))
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record(scanner_version="v1"))
    scripts_dir = tmp_path / "scripts"
    _fake_gate_script(scripts_dir, "example_gate", "v1")
    gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir, wire=True)

    _fake_gate_script(scripts_dir, "example_gate", "v2-drifted")
    _, transition = gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir, unwire=True)
    assert transition.new_state == "demoted"
    assert transition.demoted is True
    assert transition.demotion_reason == "stale"

    demotions = [json.loads(line) for line in log_path.read_text().splitlines()
                 if json.loads(line).get("event") == "demotion"]
    assert len(demotions) == 1 and demotions[0]["details"] == "stale"


def test_unwire_is_intentional_not_a_demotion(tmp_path):
    """Opus (i)/codex: an operator dropping --gate on purpose is a normal edge
    (unwire_gate), not a demotion — the record still passes -> validated."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record())
    gv.check_and_transition("example_gate", vdir, wire=True)

    report, transition = gv.check_and_transition("example_gate", vdir, unwire=True)
    assert report.passing is True
    assert transition.new_state == "validated"
    assert transition.demoted is False


def test_stale_demotion_emits_generic_demotion_not_emit_demotion(tmp_path, monkeypatch):
    """The telemetry event this auto-demotion emits must be the GENERIC
    `emit_stale_demotion`/`DEMOTION` — never `emit_demotion`, which stamps
    `details=f"seed_class={{...}}"` and requires one. A staleness demotion has no
    seed_class at all (nothing escaped in production)."""
    log_path = tmp_path / "factory-log.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log_path))

    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record(scanner_version="v1"))
    scripts_dir = tmp_path / "scripts"
    _fake_gate_script(scripts_dir, "example_gate", "v1")
    gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir, wire=True)

    _fake_gate_script(scripts_dir, "example_gate", "v2-drifted")
    gv.check_and_transition("example_gate", vdir, scripts_dir=scripts_dir)

    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    demotions = [r for r in lines if r.get("event") == "demotion"]
    assert len(demotions) == 1
    d = demotions[0]
    assert d["name"] == "example_gate"
    assert d["details"] == "stale"
    assert d["previous_authority"] == "blocking"
    assert "seed_class" not in d  # never emit_demotion's seed_class= detail


def test_cli_wire_then_record_missing_reports_demotion_in_json(tmp_path):
    """End-to-end through the actual CLI (subprocess), matching how /close-epic
    invokes this script. --wire journals a wire event; deleting the record then
    demotes on the next plain check, surfaced in the JSON envelope + stderr."""
    vdir = _write_record(tmp_path, "example_gate", _minimal_valid_record())

    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_gate_validation.py"), "example_gate",
         "--validation-dir", str(vdir), "--wire", "--format", "json"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["authority"]["new_state"] == "blocking"

    (Path(vdir) / "example_gate.json").unlink()

    proc2 = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_gate_validation.py"), "example_gate",
         "--validation-dir", str(vdir), "--format", "json"],
        capture_output=True, text=True,
    )
    assert proc2.returncode == 0  # report-only CLI mode; JSON envelope carries the verdict
    out2 = json.loads(proc2.stdout)
    assert out2["passing"] is False
    assert out2["authority"]["demoted"] is True
    assert out2["authority"]["demotion_reason"] == "record_missing"
    assert out2["authority"]["previous_authority"] == "blocking"
    assert "DEMOTION" in proc2.stderr


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
