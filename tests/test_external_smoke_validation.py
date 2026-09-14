"""check_external_smoke's validation record must stay backed by live runs.

chief-wiggum#353 + the gate-validation protocol (docs/gate-validation.md).

A record is only worth what its trials actually prove. `gate_validation_designer
audit` names the failure mode precisely: a seed_id that appears in no test suite
"cannot be re-executed, so the record is an aspirational claim for it". These
tests re-execute every seed the record cites, on every CI run, so the record
cannot quietly drift into asserting a trial nobody has run since the day it was
written.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RECORD = REPO / "docs" / "quality" / "validation" / "check_external_smoke.json"
VALIDATION_DIR = REPO / "docs" / "quality" / "validation"
sys.path.insert(0, str(REPO / "scripts"))

import check_external_smoke as gate  # noqa: E402
from check_gate_validation import corpus_digest  # noqa: E402


def record() -> dict:
    return json.loads(RECORD.read_text())


def seeds() -> list[dict]:
    return record()["seeded_defect_trials"]


def test_the_record_exists_and_passes():
    assert record()["status"] == "passed"


def test_every_mandatory_seed_class_is_present():
    """The protocol's mandatory classes for a non-telemetry gate. A record
    missing one is not a lighter record, it is an unproven one."""
    classes = {t["seed_class"] for t in seeds()}
    assert {"direct", "evasion-omission", "evasion-config-indirection",
            "evasion-sampling-gap", "instrument-broken"} <= classes


# The seed ids, named literally. Two reasons: renaming a seed in the record
# without updating its executor now fails here loudly, and
# `gate_validation_designer audit` greps tests/ for each id to confirm the trial
# is re-executable rather than an aspirational claim in a JSON file.
EXPECTED_SEED_IDS = {
    "es-direct-01",
    "es-omission-01",
    "es-config-indirection-01",
    "es-sampling-gap-01",
    "es-sampling-gap-02",
    "es-instrument-broken-01",
}


def test_the_record_cites_exactly_the_known_seeds():
    assert {s["seed_id"] for s in seeds()} == EXPECTED_SEED_IDS
    assert set(gate.SEED_EXECUTORS) == EXPECTED_SEED_IDS


def test_there_are_seeds_to_replay():
    """A denominator: an empty trial list must never let this file pass by
    checking nothing."""
    assert len(seeds()) >= 6


@pytest.mark.parametrize("seed", seeds(), ids=lambda s: s["seed_id"])
def test_each_recorded_trial_is_backed_by_a_live_execution(seed):
    """Re-run the seed and compare against what the record CLAIMS."""
    expected = {"fire": "fired", "no-fire": "not-fired"}[seed["expected"]]
    live = gate.replay_seeded_trial({"seed_id": seed["seed_id"]})
    assert live == expected, (
        f"{seed['seed_id']}: the record claims {expected}, a live replay got "
        f"{live} — re-run `gate_validation_designer.py revalidate check_external_smoke`")
    assert seed["result"] == live
    assert seed["passed"] is True


def test_every_seed_has_an_executor():
    """A seed in the record with no executor is unreplayable, which is the
    aspirational-claim failure the designer audit exists to catch."""
    missing = [s["seed_id"] for s in seeds() if s["seed_id"] not in gate.SEED_EXECUTORS]
    assert missing == [], f"seeds with no executor: {missing}"


def test_the_direct_seed_reports_unverified_not_merely_fires():
    """Firing is not enough. The point of this gate is that a SKIPPED smoke is
    `unverified` — a distinct state from failed and from passed — so a trial
    that fired for the wrong reason would still be a lie."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        corpus = Path(tmp) / "corpus"
        shutil.copytree(gate.GV_CORPUS, corpus)
        gate._seed_direct(corpus)
        epic, src, results = gate._gv_paths(corpus)
        report = gate.check([epic], src, results)
    assert [s.state for s in report.systems] == [gate.UNVERIFIED]


def test_the_instrument_broken_seed_reports_error_not_merely_fires():
    """It must fire because the scanner could not READ its input, not because
    it found a finding — `error` and `findings` are different verdicts."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        corpus = Path(tmp) / "corpus"
        shutil.copytree(gate.GV_CORPUS, corpus)
        gate._seed_instrument_broken(corpus)
        epic, src, results = gate._gv_paths(corpus)
        report = gate.check([epic], src, results)
    assert report.outcome == "error"
    assert report.unparsed


def test_the_clean_corpus_run_is_backed_by_a_live_execution():
    live = gate.replay_clean_corpus()
    stored = record()["clean_corpus_runs"][0]
    assert live["findings"] == stored["findings"] == 0
    assert live["coverage"] == stored["coverage"]
    assert live["passed"] is True
    assert any(live["coverage"].values()), "the clean run scanned nothing"


def test_the_pinned_corpus_digest_still_matches():
    """The record pins a corpus by sha. If the fixture changed, the trials were
    run against something else."""
    assert record()["clean_corpus_runs"][0]["sha"] == corpus_digest(gate.GV_CORPUS)
    for seed in seeds():
        assert seed["sha"] == corpus_digest(gate.GV_CORPUS)


def test_the_scanner_version_is_not_stale():
    """INV-fh-005: any edit to the gate or to chief_wiggum/annotations.py must
    stale this record rather than silently inherit its certification."""
    assert record()["scanner_version"] == gate._scanner_version(), (
        "the record certifies a different version of the scanner than the one on "
        "disk — re-run `gate_validation_designer.py revalidate check_external_smoke`")


def test_the_scanner_version_flag_round_trips():
    """check_gate_validation probes this over the CLI; a broken probe is never
    evidence of a clean record."""
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "check_external_smoke.py"),
         "--scanner-version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == gate._scanner_version()


def test_the_shipped_record_passes_the_protocol_checker():
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "check_gate_validation.py"),
         "check_external_smoke", "--validation-dir", str(VALIDATION_DIR), "--gate"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout
    assert "PASSING" in result.stdout


def test_the_record_is_journaled_in_the_ratchet_chain():
    """The chain is the tamper-evidence: a record id that is not journaled
    grants no provenance (docs/gate-validation.md)."""
    rid = record()["ratchet_record_id"]
    journal = REPO / "docs" / "quality" / "ratchet-journal.jsonl"
    entries = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    match = next((e for e in entries if e.get("record_id") == rid), None)
    assert match is not None, f"{rid} is not in the ratchet journal"
    assert match["event"] == "gate-validation"
    assert match["ref"] == "check_external_smoke"
