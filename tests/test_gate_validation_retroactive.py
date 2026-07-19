"""Retroactive gate-validation trials for check_single_writer and check_traceability
(docs/gate-validation.md, #168).

Both gates predate the gate-validation protocol; they were wired as blockers under
the older, prose-only docs/gate-rollout.md rule. This module actually RUNS the
seeded-defect trials and clean-corpus run each checked-in record
(docs/gate-validation/records/*.json) claims, against the fixture corpora under
tests/fixtures/gate_validation/ — so the record's claims can never silently drift
from what the gate actually does. It also proves check_gate_validation.py accepts
the shipped records as passing.
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
RECORDS_DIR = ROOT / "docs" / "gate-validation" / "records"


def _run(script: str, epic_dir: Path, source_dir: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script), str(epic_dir), "--source", str(source_dir), "--format", "json"],
        capture_output=True, text=True,
    )
    return json.loads(proc.stdout)


def _copy_clean(name: str, tmp_path: Path, dest: str) -> Path:
    src = FIXTURES / name
    out = tmp_path / dest
    shutil.copytree(src, out)
    return out


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# --- check_single_writer: seeded-defect trials ---------------------------------


def test_sw_clean_corpus_has_zero_violations_with_coverage(tmp_path):
    corpus = _copy_clean("single_writer_clean", tmp_path, "clean")
    report = _run("check_single_writer.py", corpus / "epic", corpus / "src")
    assert report["counts"]["violations"] == 0
    assert report["counts"]["malformed"] == 0
    assert report["counts"]["writers"] == 3  # coverage evidence: it actually found the sanctioned writers


def test_sw_seed_direct_fires(tmp_path):
    """sw-direct-01: ChangePlan is re-added as a second writer of provider.stripe_plan."""
    corpus = _copy_clean("single_writer_clean", tmp_path, "direct")
    _write(corpus / "src" / "internal" / "admin" / "handlers.go", (
        "package admin\n\n"
        "// ChangePlan is a LEGACY admin control — a SECOND writer of provider.stripe_plan.\n"
        "func ChangePlan(c *mongo.Collection, id ID, newPlan string) {\n"
        '\tc.UpdateOne(ctx, bson.M{"_id": id}, bson.M{"$set": bson.M{"plan": newPlan}})\n'
        "}\n"
    ))
    report = _run("check_single_writer.py", corpus / "epic", corpus / "src")
    assert report["counts"]["violations"] == 1  # expected="fire" -> result="fired"


def test_sw_seed_evasion_omission_fires(tmp_path):
    """sw-omission-01: the write is hidden inside a nested anonymous closure with
    no directly-enclosing named function — the scanner must still resolve the
    nearest NAMED enclosing function/file and flag it as unsanctioned."""
    corpus = _copy_clean("single_writer_clean", tmp_path, "omission")
    _write(corpus / "src" / "internal" / "db" / "leak.go", (
        "package db\n\n"
        "func doStuff() {\n"
        "\tfunc() {\n"
        "\t\tp.ActiveOwnerCount = p.ActiveOwnerCount - 1\n"
        "\t}()\n"
        "}\n"
    ))
    report = _run("check_single_writer.py", corpus / "epic", corpus / "src")
    assert report["counts"]["violations"] == 1  # expected="fire" -> result="fired"


def test_sw_seed_evasion_config_indirection_fires(tmp_path):
    """sw-config-indirection-01: the write goes through a generically-named
    wrapper (not the sanctioned symbol), but the literal field token is still
    visible in a mutation context — the scanner is not fooled by the indirection."""
    corpus = _copy_clean("single_writer_clean", tmp_path, "config")
    _write(corpus / "src" / "internal" / "wrappers" / "generic.go", (
        "package wrappers\n\n"
        "func SetField(c *mongo.Collection, id ID, newPlan string) {\n"
        '\tc.UpdateOne(ctx, bson.M{"_id": id}, bson.M{"$set": bson.M{"stripe_plan": newPlan}})\n'
        "}\n"
    ))
    report = _run("check_single_writer.py", corpus / "epic", corpus / "src")
    assert report["counts"]["violations"] == 1  # expected="fire" -> result="fired"


def test_sw_seed_evasion_sampling_gap_does_not_fire(tmp_path):
    """sw-sampling-gap-01: the write lives inside vendor/, which SKIP_PARTS
    excludes by design — expected="no-fire", proving the documented scope
    boundary holds (not a silent miss of an in-scope defect)."""
    corpus = _copy_clean("single_writer_clean", tmp_path, "sampling")
    _write(corpus / "src" / "vendor" / "thirdparty" / "patch.go", (
        "package thirdparty\n\n"
        "func Patch(c *mongo.Collection, id ID, v string) {\n"
        '\tc.UpdateOne(ctx, bson.M{"_id": id}, bson.M{"$set": bson.M{"stripe_plan": v}})\n'
        "}\n"
    ))
    report = _run("check_single_writer.py", corpus / "epic", corpus / "src")
    assert report["counts"]["violations"] == 0  # expected="no-fire" -> result="not-fired"


# --- check_traceability: seeded-defect trials ----------------------------------


def test_tr_clean_corpus_has_zero_findings_with_coverage(tmp_path):
    corpus = _copy_clean("traceability_clean", tmp_path, "clean")
    report = _run("check_traceability.py", corpus / "epic", corpus / "src")
    counts = report["counts"]
    assert counts["uncovered_contracts"] == 0
    assert counts["untested_contracts"] == 0
    assert counts["orphan_business_rules"] == 0
    assert counts["dangling"] == 0
    assert counts["invalid_links"] == 0
    assert counts["defined"] == 3  # coverage evidence


def test_tr_seed_direct_fires(tmp_path):
    """tr-direct-01: the guards annotation is removed entirely."""
    corpus = _copy_clean("traceability_clean", tmp_path, "direct")
    _write(corpus / "src" / "order.py", "def create_order(req):\n    ...\n")
    report = _run("check_traceability.py", corpus / "epic", corpus / "src")
    assert report["counts"]["uncovered_contracts"] > 0  # expected="fire" -> result="fired"


def test_tr_seed_evasion_omission_fires(tmp_path):
    """tr-omission-01: guards annotation intact, but INV-order-003 silently
    dropped from the verifies annotation."""
    corpus = _copy_clean("traceability_clean", tmp_path, "omission")
    _write(corpus / "src" / "test_create_order.py",
           "# @cw-trace verifies CTR-order-001\ndef test_create_order():\n    ...\n")
    report = _run("check_traceability.py", corpus / "epic", corpus / "src")
    assert report["counts"]["untested_contracts"] == 1  # expected="fire" -> result="fired"


def test_tr_seed_evasion_config_indirection_does_not_fire(tmp_path):
    """tr-config-indirection-01: the real function loses its guards annotation, but
    a decoy `@cw-trace guards ...` is placed on an unrelated no-op function. The
    checker trusts annotation PRESENCE, not semantic truthfulness — expected="no-fire",
    an honest documented blind spot, not a silent in-scope miss."""
    corpus = _copy_clean("traceability_clean", tmp_path, "config")
    _write(corpus / "src" / "order.py", (
        "def create_order(req):\n    ...\n\n\n"
        "# @cw-trace guards CTR-order-001 INV-order-003\n"
        "def unrelated_noop():\n    ...\n"
    ))
    report = _run("check_traceability.py", corpus / "epic", corpus / "src")
    assert report["counts"]["uncovered_contracts"] == 0  # expected="no-fire" -> result="not-fired"


def test_tr_seed_evasion_sampling_gap_fires(tmp_path):
    """tr-sampling-gap-01: the real verifies annotation is dropped; a decoy verifies
    annotation is placed in notes.txt, an extension outside SOURCE_EXTS — proving
    the extension-scope boundary holds (the decoy does not count)."""
    corpus = _copy_clean("traceability_clean", tmp_path, "sampling")
    _write(corpus / "src" / "test_create_order.py", "def test_create_order():\n    ...\n")
    _write(corpus / "src" / "notes.txt",
           "# @cw-trace verifies CTR-order-001 INV-order-003\n(decoy — unscanned extension)\n")
    report = _run("check_traceability.py", corpus / "epic", corpus / "src")
    assert report["counts"]["untested_contracts"] == 2  # expected="fire" -> result="fired"


# --- the shipped records validate ----------------------------------------------


def test_shipped_single_writer_record_passes_check_gate_validation():
    report = gv.check("check_single_writer", RECORDS_DIR)
    assert report.record_found, report.to_dict()
    assert report.passing, report.to_dict()


def test_shipped_traceability_record_passes_check_gate_validation():
    report = gv.check("check_traceability", RECORDS_DIR)
    assert report.record_found, report.to_dict()
    assert report.passing, report.to_dict()


def test_shipped_records_match_live_trial_outcomes():
    """The checked-in JSON records' per-trial `result` fields must match what the
    live trials above actually observe — the record is EVIDENCE of a real run,
    not an aspirational claim that could quietly drift from the gate's behavior."""
    sw_record = json.loads((RECORDS_DIR / "check_single_writer.json").read_text())
    by_class = {t["seed_class"]: t for t in sw_record["seeded_defect_trials"]}
    assert by_class["direct"]["result"] == "fired"
    assert by_class["evasion-omission"]["result"] == "fired"
    assert by_class["evasion-config-indirection"]["result"] == "fired"
    assert by_class["evasion-sampling-gap"]["result"] == "not-fired"

    tr_record = json.loads((RECORDS_DIR / "check_traceability.json").read_text())
    by_class_tr = {t["seed_class"]: t for t in tr_record["seeded_defect_trials"]}
    assert by_class_tr["direct"]["result"] == "fired"
    assert by_class_tr["evasion-omission"]["result"] == "fired"
    assert by_class_tr["evasion-config-indirection"]["result"] == "not-fired"
    assert by_class_tr["evasion-sampling-gap"]["result"] == "fired"
