"""Gate-validation record guards for the three #184 gates ratchet, ci_scaffold,
and check_architecture (docs/gate-validation.md).

These gates were wired report-only under the prose-only docs/gate-rollout.md rule;
#184 authors their retroactive validation records. This module locks the shipped
records to reality WITHOUT re-implementing the seeded-defect executions (a later
phase extends tests/test_gate_validation_retroactive.py with the table-driven
re-execution of every trial). Here we prove, mechanically:

- check_gate_validation.py accepts each record as PASSING (schema + derived
  trials + mandatory seed classes + clean-corpus coverage + status),
- provenance holds: the record's scanner_version equals the gate's LIVE
  --scanner-version, and its ratchet_record_id is a gate-validation entry naming
  the gate in the hash-chain-verified journal,
- every trial/clean-run `sha` equals a freshly re-derived content digest of its
  fixture corpus (a changed fixture is detectable staleness).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import check_gate_validation as gv

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gate_validation"
RECORDS_DIR = ROOT / "docs" / "quality" / "validation"

# gate name -> (record filename == gate, fixture corpus dir, scanner-version invocation)
GATES = {
    "ratchet": "ratchet_clean",
    "ci_scaffold": "ci_scaffold_clean",
    "check_architecture": "check_architecture_clean",
}


def _record(gate: str) -> dict:
    return json.loads((RECORDS_DIR / f"{gate}.json").read_text())


def _live_scanner_version(gate: str) -> str:
    # ratchet's --scanner-version works with no subcommand; the others take the flag directly.
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / f"{gate}.py"), "--scanner-version"],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def test_shipped_records_pass_check_gate_validation():
    # @cw-trace verifies CTR-fh-043
    for gate in GATES:
        report = gv.check(gate, RECORDS_DIR)
        assert report.record_found, report.to_dict()
        assert report.passing, report.to_dict()


def test_record_scanner_versions_match_live_gates():
    for gate in GATES:
        assert _record(gate)["scanner_version"] == _live_scanner_version(gate), (
            f"{gate} record's scanner_version is stale — re-run the trials and re-author it")


def test_record_shas_match_fresh_corpus_digests():
    for gate, corpus in GATES.items():
        digest = gv.corpus_digest(FIXTURES / corpus)
        rec = _record(gate)
        for trial in rec["seeded_defect_trials"]:
            assert trial["sha"] == digest, (
                f"{gate} trial {trial['seed_id']} pins a stale corpus digest")
        for run in rec["clean_corpus_runs"]:
            assert run["sha"] == digest, f"{gate} clean-corpus run pins a stale corpus digest"


def test_records_are_journaled_in_the_ratchet_chain():
    journal = RECORDS_DIR.parent / "ratchet-journal.jsonl"
    assert journal.is_file()
    entries = {json.loads(line)["record_id"]: json.loads(line)
               for line in journal.read_text().splitlines() if line.strip()}
    for gate in GATES:
        rid = _record(gate)["ratchet_record_id"]
        assert rid in entries, f"{gate} record's {rid} is not journaled"
        assert entries[rid]["event"] == "gate-validation"
        assert entries[rid]["ref"] == gate
