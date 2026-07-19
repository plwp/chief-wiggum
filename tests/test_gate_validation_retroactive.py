"""Retroactive gate-validation trials for check_single_writer and check_traceability
(docs/gate-validation.md, #168).

Both gates predate the gate-validation protocol; they were wired as blockers under
the older, prose-only docs/gate-rollout.md rule. This module actually RUNS every
seeded-defect trial each checked-in record (docs/quality/validation/*.json)
claims, against the fixture corpora under tests/fixtures/gate_validation/ — the
comparison is DERIVED from the executions, keyed by seed_id, so any drift between
the shipped records and reality fails the suite:

- a renamed/removed/added trial (seed_id set mismatch with the executor registry),
- a stale corpus (record `sha` vs a re-derived content digest of the fixture tree),
- a stale scanner (record `scanner_version` vs the gate's live --scanner-version),
- a result that no longer matches what the gate actually does,
- a `passed` flag that disagrees with result-vs-expected.

It also proves check_gate_validation.py accepts the shipped records as passing —
including their ratchet-journal provenance (rec-00001/rec-00002 in
docs/quality/ratchet-journal.jsonl).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import check_gate_validation as gv

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gate_validation"
RECORDS_DIR = ROOT / "docs" / "quality" / "validation"

EXPECTED_TO_RESULT = {"fire": "fired", "no-fire": "not-fired"}


def _run(script: str, epic_dir: Path, source_dir: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script), str(epic_dir), "--source", str(source_dir), "--format", "json"],
        capture_output=True, text=True,
    )
    return json.loads(proc.stdout)


def _live_scanner_version(script: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script), "--scanner-version"],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def _copy_clean(name: str, tmp_path: Path, dest: str) -> Path:
    src = FIXTURES / name
    out = tmp_path / dest
    shutil.copytree(src, out, ignore=shutil.ignore_patterns("__pycache__"))
    return out


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# --- seed executors: one per seed_id in the shipped records --------------------
#
# Each executor mutates a clean corpus copy to inject its seed. The comprehensive
# tests below iterate the RECORD's trials, look up the executor by seed_id (a
# renamed trial has no executor -> fail), run the gate, and compare the LIVE
# outcome to the record's claimed result — the record is evidence of a real run,
# never an aspirational claim.


def _sw_seed_direct(corpus: Path) -> None:
    """ChangePlan re-added: an unsanctioned second writer of provider.stripe_plan."""
    _write(corpus / "src" / "internal" / "admin" / "handlers.go", (
        "package admin\n\n"
        "// ChangePlan is a LEGACY admin control — a SECOND writer of provider.stripe_plan.\n"
        "func ChangePlan(c *mongo.Collection, id ID, newPlan string) {\n"
        '\tc.UpdateOne(ctx, bson.M{"_id": id}, bson.M{"$set": bson.M{"plan": newPlan}})\n'
        "}\n"
    ))


def _sw_seed_omission(corpus: Path) -> None:
    """The write hides inside a nested anonymous closure — no directly-enclosing
    named function at the write site itself."""
    _write(corpus / "src" / "internal" / "db" / "leak.go", (
        "package db\n\n"
        "func doStuff() {\n"
        "\tfunc() {\n"
        "\t\tp.ActiveOwnerCount = p.ActiveOwnerCount - 1\n"
        "\t}()\n"
        "}\n"
    ))


def _sw_seed_config_indirection(corpus: Path) -> None:
    """The write goes through a generically-named wrapper, not the sanctioned symbol."""
    _write(corpus / "src" / "internal" / "wrappers" / "generic.go", (
        "package wrappers\n\n"
        "func SetField(c *mongo.Collection, id ID, newPlan string) {\n"
        '\tc.UpdateOne(ctx, bson.M{"_id": id}, bson.M{"$set": bson.M{"stripe_plan": newPlan}})\n'
        "}\n"
    ))


def _sw_seed_sampling_gap(corpus: Path) -> None:
    """The write lives inside vendor/, which SKIP_PARTS excludes by design —
    a certified NON-coverage boundary (expected: no-fire)."""
    _write(corpus / "src" / "vendor" / "thirdparty" / "patch.go", (
        "package thirdparty\n\n"
        "func Patch(c *mongo.Collection, id ID, v string) {\n"
        '\tc.UpdateOne(ctx, bson.M{"_id": id}, bson.M{"$set": bson.M{"stripe_plan": v}})\n'
        "}\n"
    ))


SW_EXECUTORS = {
    "sw-direct-01": _sw_seed_direct,
    "sw-omission-01": _sw_seed_omission,
    "sw-config-indirection-01": _sw_seed_config_indirection,
    "sw-sampling-gap-01": _sw_seed_sampling_gap,
}


def _sw_outcome(corpus: Path) -> str:
    report = _run("check_single_writer.py", corpus / "epic", corpus / "src")
    return "fired" if report["counts"]["violations"] > 0 else "not-fired"


def _tr_seed_direct(corpus: Path) -> None:
    """The guards annotation is removed entirely."""
    _write(corpus / "src" / "order.py", "def create_order(req):\n    ...\n")


def _tr_seed_omission(corpus: Path) -> None:
    """Guards intact, but INV-order-003 silently dropped from the verifies annotation."""
    _write(corpus / "src" / "test_create_order.py",
           "# @cw-trace verifies CTR-order-001\ndef test_create_order():\n    ...\n")


def _tr_seed_config_indirection(corpus: Path) -> None:
    """The real function loses its guards annotation; a decoy guards annotation is
    placed on an unrelated no-op function. The checker trusts annotation PRESENCE,
    not semantic truthfulness — a certified NON-coverage boundary (expected: no-fire)."""
    _write(corpus / "src" / "order.py", (
        "def create_order(req):\n    ...\n\n\n"
        "# @cw-trace guards CTR-order-001 INV-order-003\n"
        "def unrelated_noop():\n    ...\n"
    ))


def _tr_seed_sampling_gap(corpus: Path) -> None:
    """The real verifies annotation is dropped; a decoy verifies annotation is
    placed in notes.txt, an extension outside SOURCE_EXTS — the decoy must not
    count, so the gate fires on the now-untested contracts."""
    _write(corpus / "src" / "test_create_order.py", "def test_create_order():\n    ...\n")
    _write(corpus / "src" / "notes.txt",
           "# @cw-trace verifies CTR-order-001 INV-order-003\n(decoy — unscanned extension)\n")


TR_EXECUTORS = {
    "tr-direct-01": _tr_seed_direct,
    "tr-omission-01": _tr_seed_omission,
    "tr-config-indirection-01": _tr_seed_config_indirection,
    "tr-sampling-gap-01": _tr_seed_sampling_gap,
}


def _tr_outcome(corpus: Path) -> str:
    report = _run("check_traceability.py", corpus / "epic", corpus / "src")
    c = report["counts"]
    findings = (c["orphan_business_rules"] + c["uncovered_contracts"]
                + c["untested_contracts"] + c["dangling"] + c["invalid_links"])
    return "fired" if findings > 0 else "not-fired"


def _record(gate: str) -> dict:
    return json.loads((RECORDS_DIR / f"{gate}.json").read_text())


def _assert_record_backed_by_live_trials(gate, corpus_name, executors, outcome, script, tmp_path):
    record = _record(gate)
    assert record["gate"] == gate
    # a stale scanner invalidates the record — live --scanner-version is truth
    assert record["scanner_version"] == _live_scanner_version(script), (
        f"{gate} record's scanner_version is stale — re-run the trials and re-author the record")
    digest = gv.corpus_digest(FIXTURES / corpus_name)
    trials = record["seeded_defect_trials"]
    # renamed/removed/added trials fail: record ids and executor registry must agree
    assert {t["seed_id"] for t in trials} == set(executors), (
        f"{gate} record's seed_ids diverge from the executable trial registry")
    for trial in trials:
        assert trial["sha"] == digest, (
            f"{gate} trial {trial['seed_id']} pins a stale corpus digest — the fixture changed; "
            "re-run the trials and re-author the record")
        corpus = _copy_clean(corpus_name, tmp_path, trial["seed_id"])
        executors[trial["seed_id"]](corpus)
        live_result = outcome(corpus)
        assert live_result == trial["result"], (
            f"{gate} trial {trial['seed_id']}: record claims {trial['result']!r} but the live "
            f"gate produced {live_result!r}")
        assert trial["passed"] == (live_result == EXPECTED_TO_RESULT[trial["expected"]]), (
            f"{gate} trial {trial['seed_id']}: passed flag disagrees with result vs expected")


def _assert_clean_run_backed_by_live_execution(gate, corpus_name, script, findings_of, coverage_of, tmp_path):
    record = _record(gate)
    digest = gv.corpus_digest(FIXTURES / corpus_name)
    runs = record["clean_corpus_runs"]
    assert len(runs) == 1
    run = runs[0]
    assert run["sha"] == digest
    corpus = _copy_clean(corpus_name, tmp_path, "clean")
    report = _run(script, corpus / "epic", corpus / "src")
    live_findings = findings_of(report)
    assert live_findings == 0
    assert run["findings"] == live_findings
    live_coverage = coverage_of(report)
    assert run["coverage"] == live_coverage, (
        f"{gate} clean-corpus coverage {run['coverage']} does not match live {live_coverage}")


# --- check_single_writer -------------------------------------------------------


def test_sw_record_trials_are_backed_by_live_executions(tmp_path):
    _assert_record_backed_by_live_trials(
        "check_single_writer", "single_writer_clean", SW_EXECUTORS, _sw_outcome,
        "check_single_writer.py", tmp_path)


def test_sw_clean_corpus_run_is_backed_by_live_execution(tmp_path):
    _assert_clean_run_backed_by_live_execution(
        "check_single_writer", "single_writer_clean", "check_single_writer.py",
        findings_of=lambda r: r["counts"]["violations"] + r["counts"]["malformed"],
        coverage_of=lambda r: {"invariants_checked": r["counts"]["invariants"],
                                "writers_found": r["counts"]["writers"]},
        tmp_path=tmp_path)


# --- check_traceability --------------------------------------------------------


def test_tr_record_trials_are_backed_by_live_executions(tmp_path):
    _assert_record_backed_by_live_trials(
        "check_traceability", "traceability_clean", TR_EXECUTORS, _tr_outcome,
        "check_traceability.py", tmp_path)


def test_tr_clean_corpus_run_is_backed_by_live_execution(tmp_path):
    def findings_of(r):
        c = r["counts"]
        return (c["orphan_business_rules"] + c["uncovered_contracts"]
                + c["untested_contracts"] + c["dangling"] + c["invalid_links"])

    def coverage_of(r):
        # The JSON report doesn't expose an annotation count, so derive it with
        # the gate's own scanners — never hand-assert coverage evidence.
        import check_traceability as ct  # noqa: PLC0415
        corpus = FIXTURES / "traceability_clean"
        scanned = len(ct.scan_epic_annotations(corpus / "epic")) + len(ct.scan_source(corpus / "src"))
        return {"annotations_scanned": scanned, "defined_ids": r["counts"]["defined"]}

    _assert_clean_run_backed_by_live_execution(
        "check_traceability", "traceability_clean", "check_traceability.py",
        findings_of=findings_of,
        coverage_of=coverage_of,
        tmp_path=tmp_path)


# --- the shipped records pass the gate-of-gates (incl. journal provenance) -----


def test_shipped_single_writer_record_passes_check_gate_validation():
    report = gv.check("check_single_writer", RECORDS_DIR)
    assert report.record_found, report.to_dict()
    assert report.passing, report.to_dict()


def test_shipped_traceability_record_passes_check_gate_validation():
    report = gv.check("check_traceability", RECORDS_DIR)
    assert report.record_found, report.to_dict()
    assert report.passing, report.to_dict()


def test_shipped_records_are_journaled_in_the_ratchet_chain():
    """The records' ratchet_record_ids resolve to gate-validation entries in
    chief-wiggum's own hash-chained journal (docs/quality/ratchet-journal.jsonl)."""
    journal = RECORDS_DIR.parent / "ratchet-journal.jsonl"
    assert journal.is_file()
    entries = {json.loads(line)["record_id"]: json.loads(line)
               for line in journal.read_text().splitlines() if line.strip()}
    for gate in ("check_single_writer", "check_traceability"):
        rid = _record(gate)["ratchet_record_id"]
        assert rid in entries, f"{gate} record's {rid} is not journaled"
        assert entries[rid]["event"] == "gate-validation"
        assert entries[rid]["ref"] == gate
