"""gate_validation_designer.py (#218) — the automated gate-validation designer.

Report-only ALWAYS (exit 0; it proposes, never blocks — it must earn authority
via its own validation record later, per docs/gate-rollout.md). Subcommands:

- audit:  proposes the mandatory seed-class matrix per record and flags
  (a) mandatory classes with no trial, (b) trials with no re-executable entry
  in the retroactive suite, (c) authority-boundary limit claims with no
  no-fire trial proving the boundary (honest heuristic, confidence-labeled),
  (d) clean-corpus runs whose coverage evidence is missing/all-zero, and
  (e) drift between the record's trials and its seeds file.
- matrix: the one-screen gates x seed-classes coverage table.
- escapes: INDEPENDENT escape intake — reads only the factory log + records
  (never the designer's own proposals) and classifies each escape as
  DEMOTION / boundary-consistent / uncertified-class / unclassified.
- extract-seeds: materializes a record's trials into
  docs/quality/validation/seeds/<gate>.seeds.json so seeds version
  independently of gate code (the settled #218 note).
- mutate: best-effort mutmut leg — surviving mutants are proposed seed-class
  candidates; skipped-with-instructions when mutmut is absent, never silent.

Tests use a tiny synthetic gate/record fixture rather than the real 7 (real
runs are operator-invoked); a final smoke test runs the CLI against the real
corpus and asserts only report-only behavior + parseable output.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import gate_validation_designer as gvd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_VALIDATION_DIR = REPO_ROOT / "docs" / "quality" / "validation"
REAL_SEEDS_DIR = REAL_VALIDATION_DIR / "seeds"


# --- synthetic fixture ---------------------------------------------------------


def _trial(seed_id, seed_class, expected="fire", result="fired", passed=True, **extra):
    t = {"seed_id": seed_id, "seed_class": seed_class, "seed_version": "1",
         "repo": "tests/fixtures/gate_validation/toy_clean",
         "sha": "sha256:0000", "injected": extra.pop("injected", f"inject {seed_id}"),
         "expected": expected, "result": result, "passed": passed}
    t.update(extra)
    return t


def _toy_record(**overrides):
    record = {
        "gate": "toy_gate",
        "protocol_version": "1",
        "scanner_version": "abc",
        "telemetry_dependent": False,
        "concurrency_applicable": False,
        "concurrency_note": "single-pass static scan; no concurrent dimension",
        "authority_boundary": {
            "proves": "toy claims",
            "artifact": "a toy source tree",
            "assumptions": [
                "the vendor/ subtree is excluded by design (SKIP_PARTS) — a defect placed there will not be found",
                "a fully dynamic reflection-constructed write is not detectable by the regex scanner",
            ],
        },
        "seeded_defect_trials": [
            _trial("toy-direct-01", "direct"),
            _trial("toy-omission-01", "evasion-omission"),
            _trial("toy-config-indirection-01", "evasion-config-indirection"),
            _trial("toy-sampling-gap-01", "evasion-sampling-gap",
                   expected="no-fire", result="not-fired",
                   injected="an unsanctioned defect placed inside the vendor/ subtree, "
                            "which SKIP_PARTS excludes by design"),
            # instrument-broken is unconditionally mandatory (#289), so a
            # "healthy" toy record must carry it or every audit reports it
            # missing.
            _trial("toy-instrument-broken-01", "instrument-broken",
                   injected="the source tree the gate was pointed at is emptied of "
                            "scannable files; the subject is untouched, only the "
                            "gate's ability to see it is destroyed"),
        ],
        "clean_corpus_runs": [
            {"repo": "tests/fixtures/gate_validation/toy_clean", "sha": "sha256:0000",
             "findings": 0, "coverage": {"files_scanned": 4}, "passed": True},
        ],
        "status": "passed",
        "ratchet_record_id": "rec-00001",
    }
    record.update(overrides)
    return record


@pytest.fixture
def toy_dirs(tmp_path):
    """A synthetic validation dir + tests dir with a retroactive suite that
    re-executes every toy seed except the one each test removes."""
    vdir = tmp_path / "validation"
    vdir.mkdir()
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_gate_validation_retroactive.py").write_text(
        'EXECUTORS = {"toy-direct-01": 1, "toy-omission-01": 2, '
        '"toy-config-indirection-01": 3, "toy-sampling-gap-01": 4, '
        '"toy-instrument-broken-01": 5}\n')
    return vdir, tests_dir


def _write_record(vdir: Path, record: dict) -> Path:
    p = vdir / f"{record['gate']}.json"
    p.write_text(json.dumps(record, indent=2))
    return p


def _audit(vdir, tests_dir, record=None, gate="toy_gate"):
    if record is not None:
        _write_record(vdir, record)
    return gvd.audit_gate(gate, vdir, tests_dir=tests_dir)


def _findings(audit, kind):
    return [f for f in audit["findings"] if f["kind"] == kind]


# --- (a) mandatory seed-class matrix -------------------------------------------


def test_audit_proposes_the_full_mandatory_matrix(toy_dirs):
    vdir, tests_dir = toy_dirs
    audit = _audit(vdir, tests_dir, _toy_record())
    matrix = audit["proposed_matrix"]
    assert set(matrix) == set(gvd.SEED_CLASSES)
    assert matrix["direct"]["status"] == "covered"
    assert matrix["evasion-concurrency"]["status"] == "n/a"
    assert "single-pass" in matrix["evasion-concurrency"]["why"]
    assert matrix["instrumentation-deleted"]["status"] == "n/a"


def test_audit_flags_a_mandatory_class_with_no_trial(toy_dirs):
    vdir, tests_dir = toy_dirs
    record = _toy_record()
    record["seeded_defect_trials"] = [
        t for t in record["seeded_defect_trials"]
        if t["seed_class"] != "evasion-config-indirection"]
    audit = _audit(vdir, tests_dir, record)
    flagged = _findings(audit, "missing-mandatory-class")
    assert [f["seed_class"] for f in flagged] == ["evasion-config-indirection"]
    assert audit["proposed_matrix"]["evasion-config-indirection"]["status"] == "missing"


def test_audit_flags_a_class_whose_only_trial_fails(toy_dirs):
    """A trial that exists but does not genuinely pass (result contradicts
    expected) leaves the class effectively uncovered — 'failing', not covered."""
    vdir, tests_dir = toy_dirs
    record = _toy_record()
    for t in record["seeded_defect_trials"]:
        if t["seed_class"] == "evasion-omission":
            t["result"] = "not-fired"  # forged passed=True stays
    audit = _audit(vdir, tests_dir, record)
    assert audit["proposed_matrix"]["evasion-omission"]["status"] == "failing"
    assert [f["seed_class"] for f in _findings(audit, "missing-mandatory-class")] == \
        ["evasion-omission"]


def test_audit_flags_na_concurrency_without_justification(toy_dirs):
    vdir, tests_dir = toy_dirs
    record = _toy_record()
    record.pop("concurrency_note")
    audit = _audit(vdir, tests_dir, record)
    assert _findings(audit, "na-without-justification")
    assert audit["proposed_matrix"]["evasion-concurrency"]["status"] == "n/a-unjustified"


def test_telemetry_dependent_gate_requires_instrumentation_deleted(toy_dirs):
    vdir, tests_dir = toy_dirs
    record = _toy_record(telemetry_dependent=True)
    audit = _audit(vdir, tests_dir, record)
    assert audit["proposed_matrix"]["instrumentation-deleted"]["status"] == "missing"
    assert "instrumentation-deleted" in [
        f["seed_class"] for f in _findings(audit, "missing-mandatory-class")]


def test_missing_telemetry_dependent_key_is_na_unjustified(toy_dirs):
    """A record that never DECLARES telemetry_dependent gets no free pass:
    omission of the key is flagged, mirroring the concurrency handling."""
    vdir, tests_dir = toy_dirs
    record = _toy_record()
    record.pop("telemetry_dependent")
    audit = _audit(vdir, tests_dir, record)
    entry = audit["proposed_matrix"]["instrumentation-deleted"]
    assert entry["status"] == "n/a-unjustified"
    assert "no telemetry_dependent key" in entry["why"]
    assert "instrumentation-deleted" in [
        f["seed_class"] for f in _findings(audit, "na-without-justification")]


def test_explicit_telemetry_dependent_false_is_justified_na(toy_dirs):
    """An explicit telemetry_dependent: false is a declaration — justified n/a,
    no finding."""
    vdir, tests_dir = toy_dirs
    audit = _audit(vdir, tests_dir, _toy_record())  # fixture: explicit False
    assert audit["proposed_matrix"]["instrumentation-deleted"]["status"] == "n/a"
    assert "instrumentation-deleted" not in [
        f.get("seed_class") for f in _findings(audit, "na-without-justification")]


# --- (b) re-executable seeds ---------------------------------------------------


def test_audit_flags_a_seed_with_no_reexecutable_entry(toy_dirs):
    vdir, tests_dir = toy_dirs
    record = _toy_record()
    record["seeded_defect_trials"].append(_trial("toy-ghost-99", "direct"))
    audit = _audit(vdir, tests_dir, record)
    flagged = _findings(audit, "no-reexecutable-seed")
    assert [f["seed_id"] for f in flagged] == ["toy-ghost-99"]
    # the seeds that ARE in the retroactive suite are located, not flagged
    assert audit["seed_execution"]["toy-direct-01"] == "retroactive"
    assert audit["seed_execution"]["toy-ghost-99"] == "missing"


def test_comment_mention_of_a_seed_is_mention_only_and_flagged(toy_dirs):
    """A seed_id that appears only in a comment (or any bare, unquoted mention)
    is NOT re-executable — classified mention-only:<file> and flagged missing
    with the mention-only note."""
    vdir, tests_dir = toy_dirs
    retro = tests_dir / "test_gate_validation_retroactive.py"
    retro.write_text(retro.read_text() + "# TODO: toy-planned-77 will be executed here\n")
    record = _toy_record()
    record["seeded_defect_trials"].append(_trial("toy-planned-77", "direct"))
    audit = _audit(vdir, tests_dir, record)
    assert audit["seed_execution"]["toy-planned-77"] == \
        "mention-only:test_gate_validation_retroactive.py"
    flagged = _findings(audit, "no-reexecutable-seed")
    assert [f["seed_id"] for f in flagged] == ["toy-planned-77"]
    assert "outside a string literal" in flagged[0]["detail"]
    assert "bare/comment mention" in flagged[0]["detail"]


def test_quoted_literal_seed_counts_as_reexecutable(toy_dirs):
    """A quote-delimited seed_id (registry key / test argument) is the form
    that counts — both double- and single-quoted."""
    vdir, tests_dir = toy_dirs
    (tests_dir / "test_toy_gate.py").write_text("RUN = ['toy-single-quoted-01']\n")
    record = _toy_record()
    record["seeded_defect_trials"].append(_trial("toy-single-quoted-01", "direct"))
    audit = _audit(vdir, tests_dir, record)
    # fixture seeds are double-quoted dict keys in the retroactive suite
    assert audit["seed_execution"]["toy-direct-01"] == "retroactive"
    assert audit["seed_execution"]["toy-single-quoted-01"] == "other:test_toy_gate.py"
    assert not _findings(audit, "no-reexecutable-seed")


def test_seed_reexecuted_outside_retroactive_suite_is_located_not_flagged(toy_dirs):
    """saas_gate/quality_slop_gate seeds are re-executed in their own suites —
    an honest audit reports WHERE, and only flags a seed found nowhere."""
    vdir, tests_dir = toy_dirs
    (tests_dir / "test_toy_gate.py").write_text('RUN = {"toy-elsewhere-01": 1}\n')
    record = _toy_record()
    record["seeded_defect_trials"].append(_trial("toy-elsewhere-01", "direct"))
    audit = _audit(vdir, tests_dir, record)
    assert audit["seed_execution"]["toy-elsewhere-01"] == "other:test_toy_gate.py"
    assert not [f for f in _findings(audit, "no-reexecutable-seed")
                if f["seed_id"] == "toy-elsewhere-01"]
    # surfaced as informational, since the strict protocol home is the retroactive suite
    outside = _findings(audit, "seed-outside-retroactive-suite")
    assert [f["seed_id"] for f in outside] == ["toy-elsewhere-01"]


# --- (c) authority-boundary claims ---------------------------------------------


def test_boundary_claim_proven_by_a_matching_no_fire_trial(toy_dirs):
    vdir, tests_dir = toy_dirs
    audit = _audit(vdir, tests_dir, _toy_record())
    by_claim = {b["assumption"]: b for b in audit["boundary"]}
    vendor_claim = next(a for a in by_claim if "vendor/" in a)
    assert by_claim[vendor_claim]["verdict"] in ("proven-confident", "proven-uncertain")
    assert by_claim[vendor_claim]["trial"] == "toy-sampling-gap-01"


def test_boundary_limit_claim_with_no_no_fire_trial_is_flagged_unproven(toy_dirs):
    vdir, tests_dir = toy_dirs
    audit = _audit(vdir, tests_dir, _toy_record())
    by_claim = {b["assumption"]: b for b in audit["boundary"]}
    dyn_claim = next(a for a in by_claim if "reflection" in a)
    assert by_claim[dyn_claim]["verdict"] == "unproven"
    assert dyn_claim in [f["assumption"] for f in _findings(audit, "unproven-boundary-claim")]


def test_boundary_matching_is_honest_about_being_a_heuristic(toy_dirs):
    vdir, tests_dir = toy_dirs
    audit = _audit(vdir, tests_dir, _toy_record())
    assert "heuristic" in audit["boundary_note"].lower()


# --- (d) clean-corpus coverage evidence ----------------------------------------


def test_audit_flags_all_zero_clean_corpus_coverage(toy_dirs):
    vdir, tests_dir = toy_dirs
    record = _toy_record()
    record["clean_corpus_runs"][0]["coverage"] = {"files_scanned": 0}
    audit = _audit(vdir, tests_dir, record)
    assert _findings(audit, "empty-coverage-clean-run")


def test_audit_flags_missing_clean_corpus_coverage(toy_dirs):
    vdir, tests_dir = toy_dirs
    record = _toy_record()
    record["clean_corpus_runs"][0].pop("coverage")
    audit = _audit(vdir, tests_dir, record)
    assert _findings(audit, "empty-coverage-clean-run")


def test_healthy_record_produces_no_a_or_d_findings(toy_dirs):
    vdir, tests_dir = toy_dirs
    audit = _audit(vdir, tests_dir, _toy_record())
    assert not _findings(audit, "missing-mandatory-class")
    assert not _findings(audit, "empty-coverage-clean-run")


# --- (e) seeds files: extraction + drift ---------------------------------------


def test_extract_seeds_materializes_the_records_trials(toy_dirs, tmp_path):
    vdir, tests_dir = toy_dirs
    _write_record(vdir, _toy_record())
    path = gvd.extract_seeds("toy_gate", vdir)
    assert path == vdir / "seeds" / "toy_gate.seeds.json"
    doc = json.loads(path.read_text())
    assert doc["gate"] == "toy_gate"
    assert [s["seed_id"] for s in doc["seeds"]] == [
        "toy-direct-01", "toy-omission-01", "toy-config-indirection-01",
        "toy-sampling-gap-01", "toy-instrument-broken-01"]
    seed = doc["seeds"][0]
    assert seed["seed_class"] == "direct"
    assert seed["seed_version"] == "1"
    assert seed["corpus"] == "tests/fixtures/gate_validation/toy_clean"
    assert seed["corpus_digest"] == "sha256:0000"
    assert seed["expected"] == "fire"


def test_extract_seeds_is_deterministic(toy_dirs):
    vdir, tests_dir = toy_dirs
    _write_record(vdir, _toy_record())
    first = gvd.extract_seeds("toy_gate", vdir).read_text()
    second = gvd.extract_seeds("toy_gate", vdir).read_text()
    assert first == second


def test_audit_cross_checks_record_against_seeds_file(toy_dirs):
    vdir, tests_dir = toy_dirs
    record = _toy_record()
    _write_record(vdir, record)
    gvd.extract_seeds("toy_gate", vdir)
    # in sync -> no drift finding
    audit = gvd.audit_gate("toy_gate", vdir, tests_dir=tests_dir)
    assert not _findings(audit, "seeds-file-drift")
    # the record's trial set changes without regenerating seeds -> drift
    record["seeded_defect_trials"][0]["expected"] = "no-fire"
    record["seeded_defect_trials"][0]["result"] = "not-fired"
    _write_record(vdir, record)
    audit = gvd.audit_gate("toy_gate", vdir, tests_dir=tests_dir)
    drift = _findings(audit, "seeds-file-drift")
    assert drift and "toy-direct-01" in drift[0]["detail"]


def test_extract_seeds_cli_prints_the_parking_note(toy_dirs):
    """The seeds files live under docs/quality/** — the ratchet-protected
    pathset — so the CLI must warn that a branch touching them parks."""
    vdir, tests_dir = toy_dirs
    _write_record(vdir, _toy_record())
    res = _cli("extract-seeds", "toy_gate", "--validation-dir", str(vdir))
    assert res.returncode == 0, res.stderr
    assert "ratchet-protected pathset (docs/quality/**)" in res.stdout
    assert "parks for human review" in res.stdout
    assert "regenerate only alongside the record change that motivates it" in res.stdout


def test_no_seeds_file_is_reported_but_not_drift(toy_dirs):
    vdir, tests_dir = toy_dirs
    audit = _audit(vdir, tests_dir, _toy_record())
    assert audit["seeds_file"] is None
    assert not _findings(audit, "seeds-file-drift")
    assert _findings(audit, "no-seeds-file")


# --- matrix view ---------------------------------------------------------------


def test_matrix_renders_one_row_per_gate_with_cell_statuses(toy_dirs):
    vdir, tests_dir = toy_dirs
    _write_record(vdir, _toy_record())
    second = _toy_record(gate="toy_gate_two")
    second["seeded_defect_trials"] = [t for t in second["seeded_defect_trials"]
                                      if t["seed_class"] != "evasion-sampling-gap"]
    _write_record(vdir, second)
    rows = gvd.matrix_rows(vdir, tests_dir=tests_dir)
    by_gate = {r["gate"]: r for r in rows}
    assert by_gate["toy_gate"]["cells"]["evasion-sampling-gap"] == "covered"
    assert by_gate["toy_gate_two"]["cells"]["evasion-sampling-gap"] == "missing"
    assert by_gate["toy_gate"]["cells"]["evasion-concurrency"] == "n/a"
    text = gvd.render_matrix(rows)
    assert "toy_gate" in text and "MISSING" in text


# --- escapes: independent intake -----------------------------------------------


def _write_log(tmp_path, events):
    log = tmp_path / "factory-log.jsonl"
    log.write_text("".join(json.dumps(e) + "\n" for e in events))
    return log


def test_escape_matching_a_certified_caught_class_is_a_demotion(toy_dirs, tmp_path):
    vdir, tests_dir = toy_dirs
    _write_record(vdir, _toy_record())
    log = _write_log(tmp_path, [
        {"event": "escape", "missed_by": "toy_gate", "seed_class": "evasion-omission",
         "summary": "slipped past in prod", "severity": "high"}])
    rows = gvd.escapes_view(log, vdir)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "DEMOTION"
    instr = rows[0]["instruction"]
    assert "report-only" in instr and "tracking ticket" in instr
    assert "evasion-omission" in instr
    assert "CAVEAT" not in instr  # unambiguous class: no both-certified caveat


def test_escape_class_certified_both_caught_and_boundary_demotes_with_caveat(toy_dirs, tmp_path):
    """A class with BOTH a passing caught trial and a passing no-fire boundary
    trial still demotes (conservative, parity with factory_log) — but the
    ambiguity is stated explicitly, never silent."""
    vdir, tests_dir = toy_dirs
    record = _toy_record()
    record["seeded_defect_trials"].append(_trial(
        "toy-omission-02", "evasion-omission", expected="no-fire", result="not-fired",
        injected="an omission variant placed in an excluded subtree"))
    _write_record(vdir, record)
    log = _write_log(tmp_path, [
        {"event": "escape", "missed_by": "toy_gate", "seed_class": "evasion-omission",
         "summary": "ambiguous-path escape", "severity": "high"}])
    rows = gvd.escapes_view(log, vdir)
    assert rows[0]["verdict"] == "DEMOTION"
    instr = rows[0]["instruction"]
    assert ("class certified both caught and no-fire — the event cannot distinguish "
            "the path; confirm the escape traversed the certified-caught scenario "
            "before executing the demotion") in instr


def test_escape_through_a_certified_boundary_does_not_demote(toy_dirs, tmp_path):
    vdir, tests_dir = toy_dirs
    _write_record(vdir, _toy_record())
    log = _write_log(tmp_path, [
        {"event": "escape", "missed_by": "toy_gate", "seed_class": "evasion-sampling-gap",
         "summary": "vendor/ writer shipped", "severity": "medium"}])
    rows = gvd.escapes_view(log, vdir)
    assert rows[0]["verdict"] == "boundary-consistent"
    assert "no demotion" in rows[0]["instruction"].lower()


def test_escape_with_uncertified_class_and_unclassified_and_no_record(toy_dirs, tmp_path):
    vdir, tests_dir = toy_dirs
    _write_record(vdir, _toy_record())
    log = _write_log(tmp_path, [
        {"event": "escape", "missed_by": "toy_gate", "seed_class": "evasion-concurrency",
         "summary": "race", "severity": "high"},
        {"event": "escape", "missed_by": "toy_gate", "summary": "untagged", "severity": "low"},
        {"event": "escape", "missed_by": "ghost_gate", "seed_class": "direct",
         "summary": "no record", "severity": "high"},
        {"event": "gate", "name": "toy_gate", "result": "pass"},  # non-escape: ignored
    ])
    rows = gvd.escapes_view(log, vdir)
    assert [r["verdict"] for r in rows] == ["uncertified-class", "unclassified", "no-record"]


def test_escapes_view_filters_to_one_gate(toy_dirs, tmp_path):
    vdir, tests_dir = toy_dirs
    _write_record(vdir, _toy_record())
    log = _write_log(tmp_path, [
        {"event": "escape", "missed_by": "toy_gate", "seed_class": "direct",
         "summary": "a", "severity": "high"},
        {"event": "escape", "missed_by": "other_gate", "seed_class": "direct",
         "summary": "b", "severity": "high"}])
    rows = gvd.escapes_view(log, vdir, gate="toy_gate")
    assert len(rows) == 1 and rows[0]["gate"] == "toy_gate"


def test_escapes_view_reads_only_log_and_records(toy_dirs, tmp_path, monkeypatch):
    """The settled 'independent escape intake' note: the escapes code path must
    not consume the designer's own audit/proposal machinery."""
    vdir, tests_dir = toy_dirs
    _write_record(vdir, _toy_record())
    log = _write_log(tmp_path, [
        {"event": "escape", "missed_by": "toy_gate", "seed_class": "evasion-omission",
         "summary": "x", "severity": "high"}])
    def _boom(*a, **k):  # audit machinery must never run inside escapes_view
        raise AssertionError("escapes_view consulted the designer's audit path")
    monkeypatch.setattr(gvd, "audit_gate", _boom)
    rows = gvd.escapes_view(log, vdir)
    assert rows[0]["verdict"] == "DEMOTION"


# --- mutate: best-effort mutmut leg --------------------------------------------


def test_mutate_without_mutmut_is_skipped_with_instructions(tmp_path):
    result = gvd.mutate_gate("toy_gate", scripts_dir=tmp_path, mutmut_available=False)
    assert result["status"] == "skipped"
    assert "pip install mutmut" in result["instructions"]
    assert "mutate toy_gate" in result["instructions"]
    # version caveat: the flags target the mutmut 2.x interface
    assert "mutmut 2.x" in result["instructions"]
    assert "3.x" in result["instructions"]


def test_mutate_nonzero_run_exit_is_failed_not_clean(tmp_path):
    """A mutmut run that exits nonzero produced no mutation signal — the result
    is 'failed' with the stderr surfaced, results parsing skipped; it must
    never read as a clean zero-survivor run."""
    (tmp_path / "toy_gate.py").write_text("x = 1\n")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 2, stdout="", stderr="Error: no such option: --paths-to-mutate\n")

    result = gvd.mutate_gate("toy_gate", scripts_dir=tmp_path, mutmut_available=True,
                             runner=fake_run)
    assert result["status"] == "failed"
    assert "NOT a clean result" in result["detail"]
    assert "no such option: --paths-to-mutate" in result["detail"]  # stderr tail surfaced
    assert "mutmut 2.x" in result["detail"]  # version caveat on the failure path too
    assert len(calls) == 1  # `mutmut results` was never invoked
    rendered = gvd.render_mutate("toy_gate", result)
    assert "status: failed" in rendered and "NOT a clean result" in rendered


def test_cli_mutate_failed_run_still_exits_zero(monkeypatch, capsys):
    """Report-only always: a failed mutmut run is reported, exit stays 0."""
    monkeypatch.setattr(gvd, "mutate_gate", lambda *a, **k: {
        "status": "failed",
        "detail": "mutmut run failed — no mutation signal; NOT a clean result"})
    rc = gvd.main(["mutate", "toy_gate"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "NOT a clean result" in out


def test_mutate_missing_gate_script_is_reported(tmp_path):
    result = gvd.mutate_gate("toy_gate", scripts_dir=tmp_path, mutmut_available=True)
    assert result["status"] == "error"
    assert "toy_gate.py" in result["detail"]


def test_mutate_parses_surviving_mutants_into_seed_candidates(tmp_path):
    (tmp_path / "toy_gate.py").write_text("def f(x):\n    return x + 1\n")
    (tmp_path / "test_toy_gate.py").write_text("from toy_gate import f\n")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "run" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=(
                "Survived 🙁 (3)\n\n---- toy_gate.py (3) ----\n\n4-5, 9\n"), stderr="")

    result = gvd.mutate_gate(
        "toy_gate", scripts_dir=tmp_path, tests_path=tmp_path / "test_toy_gate.py",
        mutmut_available=True, runner=fake_run, max_mutants=10)
    assert result["status"] == "ran"
    assert result["surviving_mutants"] == [4, 5, 9]
    assert len(result["candidates"]) == 3
    assert "mutmut show 4" in result["candidates"][0]["show"]
    assert "seed" in result["candidates"][0]["proposal"]
    # the run was scoped to the gate's file and its own tests
    run_cmd = next(c for c in calls if "run" in c)
    assert any("toy_gate.py" in str(part) for part in run_cmd)


def test_mutate_caps_reported_candidates_at_max_mutants(tmp_path):
    (tmp_path / "toy_gate.py").write_text("x = 1\n")

    def fake_run(cmd, **kwargs):
        if "run" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Survived 🙁 (5)\n\n---- toy_gate.py (5) ----\n\n1-5\n",
            stderr="")

    result = gvd.mutate_gate("toy_gate", scripts_dir=tmp_path, mutmut_available=True,
                             runner=fake_run, max_mutants=2)
    assert result["surviving_mutants"] == [1, 2, 3, 4, 5]
    assert len(result["candidates"]) == 2
    assert result["truncated"] is True


def test_mutate_timeout_is_partial_not_silent(tmp_path):
    (tmp_path / "toy_gate.py").write_text("x = 1\n")

    def fake_run(cmd, **kwargs):
        if "run" in cmd:
            raise subprocess.TimeoutExpired(cmd, 1)
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Survived 🙁 (1)\n\n---- toy_gate.py (1) ----\n\n7\n",
            stderr="")

    result = gvd.mutate_gate("toy_gate", scripts_dir=tmp_path, mutmut_available=True,
                             runner=fake_run, max_mutants=5)
    assert result["status"] == "partial"
    assert result["surviving_mutants"] == [7]
    assert "timed out" in result["detail"]


# --- CLI: report-only always ---------------------------------------------------


def _cli(*args):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gate_validation_designer.py"), *args],
        capture_output=True, text=True)


def test_cli_audit_all_on_the_real_corpus_is_report_only(tmp_path):
    res = _cli("audit", "--all", "--validation-dir", str(REAL_VALIDATION_DIR))
    assert res.returncode == 0, res.stderr
    assert "report-only" in res.stdout.lower()
    assert "proposes, never blocks" in res.stdout.lower()


def test_cli_audit_json_round_trips(toy_dirs):
    vdir, tests_dir = toy_dirs
    _write_record(vdir, _toy_record())
    res = _cli("audit", "toy_gate", "--validation-dir", str(vdir),
               "--tests-dir", str(tests_dir), "--format", "json")
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["report_only"] is True
    assert payload["gates"][0]["gate"] == "toy_gate"


def test_cli_matrix_on_the_real_corpus(tmp_path):
    res = _cli("matrix", "--validation-dir", str(REAL_VALIDATION_DIR))
    assert res.returncode == 0, res.stderr
    for gate in ("check_single_writer", "check_traceability", "ratchet",
                 "ci_scaffold", "check_architecture", "saas_gate", "quality_slop_gate"):
        assert gate in res.stdout


def test_cli_missing_record_still_exits_zero(toy_dirs):
    vdir, tests_dir = toy_dirs
    res = _cli("audit", "ghost_gate", "--validation-dir", str(vdir),
               "--tests-dir", str(tests_dir))
    assert res.returncode == 0
    assert "no validation record" in res.stdout.lower()


def test_cli_escapes_with_no_log_exits_zero(toy_dirs, tmp_path):
    vdir, tests_dir = toy_dirs
    res = _cli("escapes", "--validation-dir", str(vdir),
               "--factory-log", str(tmp_path / "absent.jsonl"))
    assert res.returncode == 0
    assert "no factory log" in res.stdout.lower()


def test_cli_mutate_skipped_path_exits_zero(monkeypatch, tmp_path):
    # mutmut is not a project dependency; when absent the CLI must say so and
    # exit 0. (If it IS installed in this env, the subprocess would attempt a
    # real run — so call the function instead for the deterministic half and
    # only assert the CLI contract via --help.)
    result = gvd.mutate_gate("check_single_writer", scripts_dir=REPO_ROOT / "scripts",
                             mutmut_available=False)
    assert result["status"] == "skipped"
    res = _cli("mutate", "--help")
    assert res.returncode == 0
    assert "--max-mutants" in res.stdout


# --- the shipped seeds files for the real 7 ------------------------------------


REAL_GATES = ("check_architecture", "check_single_writer", "check_traceability",
              "ci_scaffold", "quality_slop_gate", "ratchet", "saas_gate")


@pytest.mark.parametrize("gate", REAL_GATES)
def test_shipped_seeds_file_exists_and_matches_its_record(gate, tmp_path):
    """The 7 committed seeds files are derived artifacts: regenerating from the
    shipped record must reproduce them byte-for-byte (drift = stale artifact)."""
    shipped = REAL_SEEDS_DIR / f"{gate}.seeds.json"
    assert shipped.is_file(), f"missing shipped seeds file for {gate}"
    scratch = tmp_path / "validation"
    scratch.mkdir()
    (scratch / f"{gate}.json").write_text(
        (REAL_VALIDATION_DIR / f"{gate}.json").read_text())
    regenerated = gvd.extract_seeds(gate, scratch)
    assert regenerated.read_text() == shipped.read_text(), (
        f"{gate}.seeds.json is stale — regenerate with "
        f"gate_validation_designer.py extract-seeds {gate}")


def test_real_audit_reports_no_seeds_drift():
    """With the shipped seeds files in place, the real audit must not report
    record-vs-seeds drift for any of the 7."""
    for gate in REAL_GATES:
        audit = gvd.audit_gate(gate, REAL_VALIDATION_DIR)
        assert not _findings(audit, "seeds-file-drift"), audit["findings"]
        assert not _findings(audit, "no-seeds-file")
