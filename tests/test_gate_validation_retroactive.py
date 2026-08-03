"""Retroactive gate-validation trials for check_single_writer and check_traceability
(docs/gate-validation.md, #168), extended by #184 to the FIVE further gates —
ratchet, ci_scaffold, check_architecture, saas_gate, quality_slop_gate (IT-fh-04).

These gates predate the gate-validation protocol; they were wired (or wire-able)
as blockers under the older, prose-only docs/gate-rollout.md rule. This module
actually RUNS every seeded-defect trial each checked-in record
(docs/quality/validation/*.json) claims, against the fixture corpora under
tests/fixtures/gate_validation/ — the comparison is DERIVED from the executions,
keyed by seed_id, so any drift between the shipped records and reality fails
the suite:

- a renamed/removed/added trial (seed_id set mismatch with the executor registry),
- a stale corpus (record `sha` vs a re-derived content digest of the fixture tree),
- a stale scanner (record `scanner_version` vs the gate's live --scanner-version),
- a result that no longer matches what the gate actually does,
- a `passed` flag that disagrees with result-vs-expected.

It also proves check_gate_validation.py accepts the shipped records as passing —
including their ratchet-journal provenance in docs/quality/ratchet-journal.jsonl.

#184 additions (IT-fh-04 — table-driven over ALL FIVE gates): the FH184_GATES
table asserts, per gate, a passing record read via the JSON envelope
(`passing == true`, never the default exit code — CTR-fh-043 / INV-fh-003), a
live-round-tripped scanner_version (INV-fh-005), and — for saas_gate and
quality_slop_gate — a fixture/recorded target, never a live URL or AI band
(CTR-fh-044). Seeded trials for ratchet/ci_scaffold/check_architecture are
re-executed by seed_id here; saas_gate's and quality_slop_gate's are re-executed
in tests/test_saas_gate.py / tests/test_quality_slop_gate.py against their
fixture harnesses (the scripted local HTTP server and the band files). Per
ADR-fh-06, check_architecture additionally proves one genuinely-passing `fire`
trial per frozen CHECKS entry — and a mutation test asserts dropping one seed is
detected, not absorbed.
"""

from __future__ import annotations

import ast
import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import check_architecture as ca
import check_gate_validation as gv
import pytest

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


def _tr_seed_instrument_broken(corpus: Path) -> None:
    """The INSTRUMENT is broken, not the code under test (chief-wiggum#281).

    The epic is re-authored with the two-segment `INV-001` shape the /architect
    skill's own worked example used to model. Every ID becomes unparseable, so
    the scanner finds zero definitions and zero annotation targets — and before
    #281 that reported a green `inapplicable` pass, because "nothing measured"
    and "nothing wrong" were the same output.

    This is the runtime analogue of the instrumentation-deleted evasion class:
    nothing about the source tree changed, only the gate's ability to SEE it.
    Expected: fire.
    """
    _write(corpus / "epic" / "contracts.md", (
        "### CTR-001 — valid date range\n"
        "<!-- @cw-trace realizes BR-001 -->\n\n"
        "REQUIRES: start_date <= end_date\n"
        "ENSURES: order.total > 0\n"
    ))
    _write(corpus / "epic" / "invariants.md", (
        "- **BR-001**: orders must have a positive total\n"
        "- **INV-003**: order status never regresses\n"
    ))


TR_EXECUTORS = {
    "tr-direct-01": _tr_seed_direct,
    "tr-omission-01": _tr_seed_omission,
    "tr-config-indirection-01": _tr_seed_config_indirection,
    "tr-sampling-gap-01": _tr_seed_sampling_gap,
    "tr-instrument-broken-01": _tr_seed_instrument_broken,
}


def _tr_outcome(corpus: Path) -> str:
    report = _run("check_traceability.py", corpus / "epic", corpus / "src")
    c = report["counts"]
    # malformed_ids/unparsed_artifacts MUST be in this sum. They are the #281
    # finding classes, and a trial harness that omits them reports "not-fired"
    # while the gate is firing correctly — reproducing the very bug the gate
    # now catches, one layer up, inside the machinery that certifies it.
    findings = (c["orphan_business_rules"] + c["uncovered_contracts"]
                + c["untested_contracts"] + c["dangling"] + c["invalid_links"]
                + c["malformed_ids"] + c["unparsed_artifacts"])
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


def test_tr_instrument_broken_seed_reports_error_not_merely_fires(tmp_path):
    """The instrument-broken trial must be certified on the STATE, not just on
    fired/not-fired (chief-wiggum#281).

    This seed also produces dangling annotations (the source still points at
    ids the scanner can no longer resolve), so it would report "fired" even
    under the pre-#281 finding sum. A trial that only checks fired/not-fired
    therefore does NOT prove the new finding class works — it would pass just
    as happily if malformed_ids were never implemented.

    So assert the distinguishing facts directly: the outcome is `error` (not a
    green `inapplicable`), the malformed tokens are NAMED rather than merely
    counted, and the measured denominator makes the zero visible.
    """
    corpus = _copy_clean("traceability_clean", tmp_path, "tr-instrument-broken-assert")
    _tr_seed_instrument_broken(corpus)
    report = _run("check_traceability.py", corpus / "epic", corpus / "src")

    assert report["applicability"] == "error", report
    assert report["outcome"] == "error", report
    # artifacts were present and non-empty, but nothing parsed out of them
    assert report["measured"]["id_bearing_artifacts"] == 2, report["measured"]
    assert report["measured"]["defined_ids"] == 0, report["measured"]
    assert report["counts"]["unparsed_artifacts"] == 2, report["counts"]

    tokens = {m["token"] for m in report["malformed_ids"]}
    assert tokens == {"CTR-001", "BR-001", "INV-003"}, report["malformed_ids"]
    for m in report["malformed_ids"]:
        assert m["file"] and m["line"], m


def test_tr_gate_exits_nonzero_on_the_instrument_broken_seed(tmp_path):
    """`error` must fail BOTH gates — the whole point of #281 is that this
    state stops exiting 0."""
    corpus = _copy_clean("traceability_clean", tmp_path, "tr-instrument-broken-exit")
    _tr_seed_instrument_broken(corpus)
    for gate in ("soundness", "coverage"):
        rc = subprocess.run(
            [sys.executable, str(SCRIPTS / "check_traceability.py"), str(corpus / "epic"),
             "--source", str(corpus / "src"), "--gate", gate],
            capture_output=True, text=True).returncode
        assert rc != 0, f"--gate {gate} exited 0 on a broken instrument"


def test_tr_clean_corpus_run_is_backed_by_live_execution(tmp_path):
    def findings_of(r):
        c = r["counts"]
        # Same rule as _tr_outcome: the #281 finding classes belong in the sum,
        # or a clean-corpus run could report 0 findings while the gate fires.
        return (c["orphan_business_rules"] + c["uncovered_contracts"]
                + c["untested_contracts"] + c["dangling"] + c["invalid_links"]
                + c["malformed_ids"] + c["unparsed_artifacts"])

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


def test_ratchet_gate_is_journal_wired_blocking():
    """#208: the verifier-test-hash dimension's promotion to blocking is a
    JOURNALED fact, not workflow prose — the last gate-authority event for
    `ratchet` in chief-wiggum's own hash-chained journal is `wire`, and the
    shipped record still passes, so the tracked authority is `blocking` (a
    later staleness/regression is then detectable as an auto-demotion)."""
    import ratchet as ratchet_mod
    journal = RECORDS_DIR.parent / "ratchet-journal.jsonl"
    assert ratchet_mod.last_authority_action(journal, "ratchet") == "wire", (
        "ratchet gate is not journal-wired — re-run check_gate_validation.py ratchet --wire")
    report = gv.check("ratchet", RECORDS_DIR)
    assert report.passing, report.to_dict()


def test_workflow_adapters_consume_the_wired_verifier_gate():
    """#208 (review finding): the journal says the gate is wired, but the
    workflows are what actually CONSUME that authority — pin the command
    adapters so removing the flag, the record check, or the downgrade
    instruction fails a test instead of silently un-wiring the dimension."""
    close_epic = (ROOT / ".claude" / "commands" / "close-epic.md").read_text()
    implement = (ROOT / ".claude" / "commands" / "implement.md").read_text()
    wave = (ROOT / ".claude" / "commands" / "implement-wave.md").read_text()

    # /close-epic Step 2c2 checks the ratchet record like the other gates...
    assert "check_gate_validation.py\" ratchet" in close_epic, (
        "/close-epic no longer validates the ratchet gate's record before wiring")
    # ...Step 2f passes the blocking flag, with the downgrade posture stated.
    assert close_epic.count("--gate-verifier-tests") >= 2, (
        "/close-epic no longer passes --gate-verifier-tests in the ratchet gate step")
    assert "otherwise drop the flag" in close_epic, (
        "/close-epic lost the downgrade-to-report-only-and-surface instruction")

    # /implement Step 8 4b: same record-gated posture per ticket.
    assert "--gate-verifier-tests" in implement, (
        "/implement Step 8 no longer passes --gate-verifier-tests")
    assert "check_gate_validation.py ratchet" in implement, (
        "/implement Step 8 lost the record check guarding the flag")

    # /implement-wave per-wave staging check: same posture (review finding —
    # otherwise wave promotion is the one consumer where a C1c rewrite reaches
    # main report-only and only blocks at epic close).
    assert "--gate-verifier-tests" in wave, (
        "/implement-wave per-wave check no longer passes --gate-verifier-tests")
    assert "check_gate_validation.py ratchet" in wave, (
        "/implement-wave lost the record check guarding the flag")

    assert all("--amend-verifier" in t for t in (implement, close_epic, wave)), (
        "the journaled human revision path (--amend-verifier) is no longer surfaced")


# ==============================================================================
# #184 — IT-fh-04: table-driven records for ALL FIVE further gates
# ==============================================================================
#
# gate -> its fixture corpus dir under tests/fixtures/gate_validation/. A sixth
# blocking-capable gate added later without a record fails this table (add it
# here + author its record), which is the point of the table-driven shape.
FH184_GATES = {
    "ratchet": "ratchet_clean",
    "ci_scaffold": "ci_scaffold_clean",
    "check_architecture": "check_architecture_clean",
    "saas_gate": "saas_gate_clean",
    "quality_slop_gate": "quality_slop_gate_clean",
}

# The two gates whose live targets are non-deterministic (a live URL, an AI
# band): CTR-fh-044 requires their records to pin a fixture/recorded target.
FIXTURE_TARGET_GATES = ("saas_gate", "quality_slop_gate")


@pytest.mark.parametrize("gate", sorted(FH184_GATES))
def test_fh184_record_passes_gate_of_gates(gate):
    """Validity is the JSON envelope's `passing == true` — NEVER the default
    exit code, which is 0 in report-only mode even when not validated."""
    # @cw-trace verifies CTR-fh-043
    report = gv.check(gate, RECORDS_DIR)
    assert report.record_found, report.to_dict()
    assert report.passing, report.to_dict()


@pytest.mark.parametrize("gate", sorted(FH184_GATES))
def test_fh184_record_scanner_version_round_trips_live(gate):
    """A record authored against an older scanner is stale (INV-fh-005): its
    scanner_version must equal the gate's LIVE --scanner-version output."""
    # @cw-trace verifies CTR-fh-040 CTR-fh-043
    assert _record(gate)["scanner_version"] == _live_scanner_version(f"{gate}.py"), (
        f"{gate} record's scanner_version is stale — re-run the trials and re-author the record")


@pytest.mark.parametrize("gate", sorted(FH184_GATES))
def test_fh184_record_pins_a_fresh_fixture_corpus(gate):
    """Every trial and clean run pins the current content digest of its fixture
    corpus — a changed fixture is detectable staleness, and every corpus lives
    under tests/fixtures/ (a fixture target, not a live dependency)."""
    # @cw-trace verifies CTR-fh-044
    record = _record(gate)
    digest = gv.corpus_digest(FIXTURES / FH184_GATES[gate])
    for trial in record["seeded_defect_trials"]:
        assert trial["repo"].startswith("tests/fixtures/gate_validation/"), (
            f"{gate} trial {trial['seed_id']} does not target an in-repo fixture corpus")
        assert trial["sha"] == digest, (
            f"{gate} trial {trial['seed_id']} pins a stale corpus digest — the fixture "
            "changed; re-run the trials and re-author the record")
    for run in record["clean_corpus_runs"]:
        assert run["repo"].startswith("tests/fixtures/gate_validation/")
        assert run["sha"] == digest, f"{gate} clean-corpus run pins a stale corpus digest"


@pytest.mark.parametrize("gate", FIXTURE_TARGET_GATES)
def test_fh184_nondeterministic_gates_name_fixture_targets(gate):
    """saas_gate / quality_slop_gate records must pin a fixture/recorded target
    (CTR-fh-044): no trial or clean run may name a live URL or AI band — a
    record validated against prod/live-band can never be re-verified."""
    # @cw-trace verifies CTR-fh-044
    record = _record(gate)
    entries = record["seeded_defect_trials"] + record["clean_corpus_runs"]
    for entry in entries:
        assert "http://" not in entry["repo"] and "https://" not in entry["repo"], (
            f"{gate} record targets a live URL: {entry['repo']!r}")
    boundary = json.dumps(record["authority_boundary"])
    assert "fixture" in boundary.lower(), (
        f"{gate} record's authority boundary does not declare its fixture target")


# --- seed executors for the three gates re-executed here ----------------------
#
# saas_gate / quality_slop_gate trials are re-executed by seed_id against their
# fixture harnesses in tests/test_saas_gate.py / tests/test_quality_slop_gate.py
# (the scripted HTTP server needs its own scenario plumbing; the band files feed
# the gate's pure verdict functions). The three gates below follow this module's
# original copy-mutate-run pattern.


def _rt_outcome(corpus: Path) -> str:
    """Re-score the mutated fixture repo, then read `ratchet check`'s JSON.

    Counts the verifier-test findings (#206) alongside the original three —
    one uniform outcome for every ratchet seed, so a verifier seed "fires"
    exactly when the new dimension reports it and the config-indirection
    no-fire seed proves the helper boundary is REALLY quiet (no finding of
    any class)."""
    subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "score",
         "--repo", str(corpus), "--no-quality"],
        capture_output=True, text=True, check=True,
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "check",
         "--repo", str(corpus), "--format", "json"],
        capture_output=True, text=True,
    )
    rep = json.loads(proc.stdout)
    findings = (len(rep["missing_tests"]) + len(rep["weakened_contracts"])
                + len(rep["removed_contracts"])
                + len(rep["weakened_verifier_tests"])
                + len(rep["removed_verifier_tests"]))
    return "fired" if findings else "not-fired"


def _rt_seed_direct(corpus: Path) -> None:
    """CTR-rt-001's REQUIRES wording is changed — contract-hash weakening."""
    p = corpus / "docs" / "epics" / "gv-ratchet" / "contracts.md"
    p.write_text(p.read_text().replace(
        "no longer than 64 characters", "of any length whatsoever"))


def _rt_seed_omission(corpus: Path) -> None:
    """The weakening happens in the STRUCTURED JSON contract (walk_json_ids
    path), not markdown prose — proving the JSON channel is hashed too."""
    p = corpus / "docs" / "epics" / "gv-ratchet" / "models" / "contracts.json"
    doc = json.loads(p.read_text())
    doc["contracts"][0]["ensures"] = "the row may be hard-removed"
    p.write_text(json.dumps(doc, indent=2))


def _rt_seed_config_indirection(corpus: Path) -> None:
    """The whole epic tree is MOVED outside the configured epic_docs root —
    the IDs stop resolving, which must read as removed_contracts, not silence."""
    src = corpus / "docs" / "epics" / "gv-ratchet"
    dst = corpus / "docs" / "archive" / "gv-ratchet"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def _rt_seed_sampling_gap(corpus: Path) -> None:
    """A CONFLICTING redefinition of CTR-rt-001 inside justifications/ — a
    certified NON-coverage boundary (expected: no-fire). If the subtree were
    scanned, the combined definition hash would change and the weakened gate
    would fire; hash_epic_definitions excludes it by design (a waiver's own id
    names the contract it waives, never a new declaration)."""
    p = corpus / "docs" / "epics" / "gv-ratchet" / "justifications" / "waiver.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "## CTR-rt-001 — create_widget validates its name (WAIVED)\n\n"
        "REQUIRES: nothing.\nENSURES: nothing.\n")


_RT_SMOKE = "test_ratchet_fixture_smoke.py"


def _rt_seed_verifier_direct(corpus: Path) -> None:
    """The C1c move from the goalpost-integrity experiment (#206): the
    annotated test's ASSERTION is inverted while its test ID stays green —
    body hash changes, weakened_verifier_tests must fire."""
    p = corpus / _RT_SMOKE
    p.write_text(p.read_text().replace(
        "assert _sum_holds(1, 1, 2)", "assert True  # blessed"))


def _rt_seed_verifier_omission(corpus: Path) -> None:
    """The weakened check hides inside a nested scope WITHIN the annotated
    test — still inside the hashed function span, so it must fire."""
    p = corpus / _RT_SMOKE
    p.write_text(p.read_text().replace(
        "assert _sum_holds(1, 1, 2)",
        "check = lambda: True\n    assert check()"))


def _rt_seed_verifier_config_indirection(corpus: Path) -> None:
    """The shared HELPER the annotated test calls is weakened; the test's own
    span is untouched. Expected NO-FIRE: this is the dimension's documented
    v1 authority boundary (the hash covers the test function's own source
    span), proven here rather than silently claimed."""
    p = corpus / _RT_SMOKE
    p.write_text(p.read_text().replace(
        "return a + b == expected", "return True  # weakened helper"))


def _rt_seed_verifier_sampling_gap(corpus: Path) -> None:
    """An annotated verifier test in a language the extractor doesn't cover
    (.go). Expected NO-FIRE — and `score` surfaces it as unscanned rather
    than staying silent (asserted by tests/test_verifier_hashes.py)."""
    (corpus / "widget_test.go").write_text(
        "// @cw-trace verifies CTR-rt-001\n"
        "func TestWidgetAddition(t *testing.T) {}\n")


RT_EXECUTORS = {
    "rt-direct-01": _rt_seed_direct,
    "rt-omission-01": _rt_seed_omission,
    "rt-config-indirection-01": _rt_seed_config_indirection,
    "rt-sampling-gap-01": _rt_seed_sampling_gap,
    "rt-verifier-direct-01": _rt_seed_verifier_direct,
    "rt-verifier-omission-01": _rt_seed_verifier_omission,
    "rt-verifier-config-indirection-01": _rt_seed_verifier_config_indirection,
    "rt-verifier-sampling-gap-01": _rt_seed_verifier_sampling_gap,
}


def _ci_outcome(corpus: Path) -> str:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ci_scaffold.py"),
         "--repo", str(corpus), "--report", "--json"],
        capture_output=True, text=True,
    )
    rep = json.loads(proc.stdout)
    return "not-fired" if rep["ci_present"] else "fired"


def _ci_seed_direct(corpus: Path) -> None:
    """The only workflow is deleted — the textbook missing-CI state."""
    (corpus / ".github" / "workflows" / "ci.yml").unlink()


def _ci_seed_omission(corpus: Path) -> None:
    """workflows/ exists but holds no *.yml/*.yaml — presence of the directory
    alone must not read as CI."""
    (corpus / ".github" / "workflows" / "ci.yml").unlink()
    (corpus / ".github" / "workflows" / "README.md").write_text("# no workflows here\n")


def _ci_seed_config_indirection(corpus: Path) -> None:
    """A real workflow under a different name — the detector requires SOME
    workflow file, not a specific filename (expected: no-fire)."""
    wf = corpus / ".github" / "workflows"
    (wf / "ci.yml").rename(wf / "deploy-then-test.yaml")


def _ci_seed_sampling_gap(corpus: Path) -> None:
    """A no-op workflow with a valid suffix — content is out of the detector's
    documented scope (presence-only), a certified boundary (expected: no-fire)."""
    (corpus / ".github" / "workflows" / "ci.yml").write_text(
        "name: noop\non: push\njobs:\n  noop:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: 'true'\n")


CI_EXECUTORS = {
    "ci-direct-01": _ci_seed_direct,
    "ci-omission-01": _ci_seed_omission,
    "ci-config-indirection-01": _ci_seed_config_indirection,
    "ci-sampling-gap-01": _ci_seed_sampling_gap,
}


def _arch_outcome(corpus: Path) -> str:
    return _arch_report(corpus)[0]


def _arch_report(corpus: Path) -> tuple[str, dict]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_architecture.py"),
         str(corpus / "architecture.json"),
         "--system-contracts", str(corpus / "system-contracts.json"),
         "--format", "json"],
        capture_output=True, text=True,
    )
    rep = json.loads(proc.stdout)
    return ("fired" if rep["counts"]["findings"] else "not-fired"), rep


def _edit_arch(corpus: Path, mutate) -> None:
    p = corpus / "architecture.json"
    doc = json.loads(p.read_text())
    mutate(doc)
    p.write_text(json.dumps(doc, indent=2))


def _edit_sc(corpus: Path, mutate) -> None:
    p = corpus / "system-contracts.json"
    doc = json.loads(p.read_text())
    mutate(doc)
    p.write_text(json.dumps(doc, indent=2))


def _node(doc: dict, nid: str) -> dict:
    return next(n for n in doc["nodes"] if n["id"] == nid)


def _edge(doc: dict, eid: str) -> dict:
    return next(e for e in doc["edges"] if e["id"] == eid)


def _arch_seed_sampling_gap(corpus: Path) -> None:
    """A retired node on an edge marked active:false — the retired-node check
    covers ACTIVE edges only, a documented exemption (expected: no-fire)."""
    def mutate(doc):
        _node(doc, "ARC-analytics-001")["status"] = "retired"
        _edge(doc, "EDG-gateway-analytics-001")["active"] = False
    _edit_arch(corpus, mutate)


# seed_id -> (executor, CHECKS entry it must fire, or None for the evasions
# whose finding class is broader than a single check)
ARCH_EXECUTORS = {
    "arch-dangling-endpoint-01": (
        lambda c: _edit_arch(c, lambda d: _edge(d, "EDG-gateway-analytics-001").update(
            to="ARC-ghost-999")),
        "dangling-endpoint"),
    "arch-retired-node-edge-01": (
        lambda c: _edit_arch(c, lambda d: _node(d, "ARC-analytics-001").update(
            status="retired")),
        "retired-node-edge"),
    "arch-unlabelled-external-01": (
        lambda c: _edit_arch(c, lambda d: _node(d, "ARC-stt-001").pop("asm_refs")),
        "unlabelled-external"),
    "arch-tier-inversion-01": (
        lambda c: _edit_arch(c, lambda d: _edge(d, "EDG-gateway-analytics-001").update(
            criticality="hard", on_failure={"fallback": None, "degrade_to": None})),
        "tier-inversion"),
    "arch-label-propagation-01": (
        lambda c: _edit_arch(c, lambda d: _edge(d, "EDG-gateway-analytics-001").update(
            carries=["official-sensitive"])),
        "label-propagation"),
    "arch-undeclared-cross-ref-01": (
        lambda c: _edit_sc(c, lambda d: d["chains"][0]["hops"][1].update(
            callee="ARC-does-not-exist-999")),
        "undeclared-cross-ref"),
    "arch-missing-tier-01": (
        lambda c: _edit_arch(c, lambda d: _node(d, "ARC-analytics-001").pop(
            "criticality_tier")),
        "missing-tier"),
    "arch-authored-crossing-label-01": (
        lambda c: _edit_arch(c, lambda d: _edge(d, "EDG-gateway-analytics-001").update(
            trust_zone_crossing="dmz->internal")),
        "authored-crossing-label"),
    "arch-omission-01": (
        lambda c: _edit_arch(c, lambda d: d["nodes"].append(
            {**_node(d, "ARC-analytics-001"), "status": "retired"})),
        None),
    "arch-config-indirection-01": (
        lambda c: _edit_sc(c, lambda d: d["trees"][0]["root"]["children"][0].update(
            telemetry_ref="nonexistent_binding_ms")),
        None),
    "arch-sampling-gap-01": (_arch_seed_sampling_gap, None),
}


def test_ratchet_record_trials_are_backed_by_live_executions(tmp_path):
    # @cw-trace verifies CTR-fh-043
    _assert_record_backed_by_live_trials(
        "ratchet", "ratchet_clean", RT_EXECUTORS, _rt_outcome, "ratchet.py", tmp_path)


def test_ratchet_clean_corpus_run_is_backed_by_live_execution(tmp_path):
    record = _record("ratchet")
    run = record["clean_corpus_runs"][0]
    assert run["sha"] == gv.corpus_digest(FIXTURES / "ratchet_clean")
    corpus = _copy_clean("ratchet_clean", tmp_path, "clean")
    assert _rt_outcome(corpus) == "not-fired"
    sc = json.loads((corpus / "docs" / "quality" / "ratchet-scorecard.json").read_text())
    live_coverage = {"pass_set_size": len(sc["pass_set"]),
                     "contracts_hashed": len(sc["contract_hashes"]),
                     "verifier_tests_hashed": len(sc["verifier_test_hashes"])}
    assert run["findings"] == 0
    assert run["coverage"] == live_coverage, (
        f"ratchet clean-corpus coverage {run['coverage']} does not match live {live_coverage}")


def test_ci_scaffold_record_trials_are_backed_by_live_executions(tmp_path):
    # @cw-trace verifies CTR-fh-043
    _assert_record_backed_by_live_trials(
        "ci_scaffold", "ci_scaffold_clean", CI_EXECUTORS, _ci_outcome,
        "ci_scaffold.py", tmp_path)


def test_ci_scaffold_clean_corpus_run_is_backed_by_live_execution(tmp_path):
    record = _record("ci_scaffold")
    run = record["clean_corpus_runs"][0]
    assert run["sha"] == gv.corpus_digest(FIXTURES / "ci_scaffold_clean")
    corpus = _copy_clean("ci_scaffold_clean", tmp_path, "clean")
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ci_scaffold.py"),
         "--repo", str(corpus), "--report", "--json"],
        capture_output=True, text=True,
    )
    rep = json.loads(proc.stdout)
    assert rep["ci_present"] is True
    live_coverage = {"workflows_found": len(rep["workflows"]),
                     "stacks_detected": len(rep["stack"])}
    assert run["findings"] == 0
    assert run["coverage"] == live_coverage


def test_check_architecture_record_trials_are_backed_by_live_executions(tmp_path):
    # @cw-trace verifies CTR-fh-043
    executors = {sid: fn for sid, (fn, _check) in ARCH_EXECUTORS.items()}
    _assert_record_backed_by_live_trials(
        "check_architecture", "check_architecture_clean", executors, _arch_outcome,
        "check_architecture.py", tmp_path)


def test_check_architecture_clean_corpus_run_is_backed_by_live_execution(tmp_path):
    record = _record("check_architecture")
    run = record["clean_corpus_runs"][0]
    assert run["sha"] == gv.corpus_digest(FIXTURES / "check_architecture_clean")
    corpus = _copy_clean("check_architecture_clean", tmp_path, "clean")
    result, rep = _arch_report(corpus)
    assert result == "not-fired"
    live_coverage = {"nodes_checked": rep["counts"]["nodes"],
                     "edges_checked": rep["counts"]["edges"],
                     "checks_run": len(ca.CHECKS)}
    assert run["findings"] == 0
    assert run["coverage"] == live_coverage


# --- ADR-fh-06: one genuinely-passing fire trial per frozen CHECKS entry ------


def _checks_missing_a_passing_fire_seed(record: dict) -> list[str]:
    """The frozen CHECKS entries lacking a genuinely-passing `fire` trial that
    targets them. Trial->check binding comes from the executor registry (the
    table that actually runs each seed), so a renamed trial can't fake coverage."""
    covered = set()
    passing = {t["seed_id"] for t in record["seeded_defect_trials"]
               if t["expected"] == "fire" and t["result"] == "fired" and t["passed"] is True}
    for seed_id, (_fn, check) in ARCH_EXECUTORS.items():
        if check is not None and seed_id in passing:
            covered.add(check)
    return [check for check in ca.CHECKS if check not in covered]


def test_check_architecture_record_covers_every_frozen_check(tmp_path):
    """ADR-fh-06: the record must carry one genuinely-passing fire trial per
    CHECKS entry — and each targeted trial must fire EXACTLY its check live."""
    # @cw-trace verifies CTR-fh-043
    record = _record("check_architecture")
    assert _checks_missing_a_passing_fire_seed(record) == []
    for seed_id, (fn, check) in ARCH_EXECUTORS.items():
        if check is None:
            continue
        corpus = _copy_clean("check_architecture_clean", tmp_path, seed_id)
        fn(corpus)
        _result, rep = _arch_report(corpus)
        assert check in rep["counts"]["by_check"], (
            f"{seed_id} was expected to fire the {check!r} check but fired "
            f"{rep['counts']['by_check']}")


def test_check_architecture_check_coverage_detects_a_dropped_seed():
    """Mutation guard: removing one check's seed from the record must be
    detected — a missing per-check seed fails, not merely the generic
    required_seed_classes set."""
    record = copy.deepcopy(_record("check_architecture"))
    record["seeded_defect_trials"] = [
        t for t in record["seeded_defect_trials"] if t["seed_id"] != "arch-tier-inversion-01"]
    assert _checks_missing_a_passing_fire_seed(record) == ["tier-inversion"]


# --- CTR-fh-041: the scanner-version dep list is COMPLETE, checked mechanically


SCANNER_VERSION_GATES = (
    "ratchet", "ci_scaffold", "check_architecture", "saas_gate",
    "quality_slop_gate", "check_single_writer", "check_traceability",
)

_CW_IMPORT_RE = re.compile(
    r"^\s*from chief_wiggum(?:\.(\w+))? import (.+)$|^\s*import chief_wiggum\.(\w+)", re.M)
# The local `quality` engine package (scripts/quality/) is a finding-affecting
# dependency exactly like chief_wiggum — quality_slop_gate's verdicts and
# ratchet's quality_regressions are shaped by its modules. Lazy (indented,
# in-function) imports count: they still execute on the finding path.
_QUALITY_IMPORT_RE = re.compile(
    r"^\s*from quality(?:\.(\w+))? import (.+)$|^\s*import quality\.(\w+)", re.M)


def _module_deps(source: str, pattern: re.Pattern) -> set[str]:
    """Every submodule of a local package a gate script imports (both
    `from pkg.X import ...` and `from pkg import X [as alias], Y` forms,
    top-level or lazily inside a function)."""
    deps: set[str] = set()
    for m in pattern.finditer(source):
        if m.group(1):
            deps.add(m.group(1))
        elif m.group(3):
            deps.add(m.group(3))
        else:
            # `from pkg import X [as alias], Y  # comment` — the names ARE modules
            names = m.group(2).split("#")[0].split(",")
            deps.update(n.strip().split(" as ")[0].strip().rstrip(")")
                        for n in names if n.strip())
    return deps


def _chief_wiggum_deps(source: str) -> set[str]:
    return _module_deps(source, _CW_IMPORT_RE)


def _quality_deps(source: str) -> set[str]:
    return _module_deps(source, _QUALITY_IMPORT_RE)


def _scanner_version_hash_inputs(source: str) -> str:
    """The text of the gate's _scanner_version function — where hash inputs are
    declared as `cw_dir / "<module>.py"` entries."""
    start = source.index("def _scanner_version")
    end = source.find("\ndef ", start + 1)
    return source[start:end if end != -1 else len(source)]


@pytest.mark.parametrize("gate", SCANNER_VERSION_GATES)
def test_scanner_version_dep_list_is_complete(gate):
    """INV-fh-005 / CTR-fh-041: for every finding-affecting local module a gate
    imports — the `chief_wiggum` package AND the `quality` engine package —
    that module's file must be among its --scanner-version hash inputs; an
    omitted dep is silent staleness (a change to the dep never invalidates the
    gate's validation record). This caught two real defects:
    check_traceability imported trace_links (suspect-link/sidecar/justification
    logic) without hashing it, and quality_slop_gate/ratchet executed the
    quality engines (survival/duplication; churn/complexity) without hashing
    them."""
    # @cw-trace verifies CTR-fh-041 INV-fh-005
    source = (SCRIPTS / f"{gate}.py").read_text()
    deps = _chief_wiggum_deps(source) | _quality_deps(source)
    assert deps, f"{gate} imports no chief_wiggum/quality modules?"
    block = _scanner_version_hash_inputs(source)
    missing = sorted(d for d in deps if f'"{d}.py"' not in block)
    assert not missing, (
        f"{gate}'s _scanner_version omits imported local module(s) "
        f"{missing} from its hash inputs — an edit there would never mark the "
        "validation record stale (CTR-fh-041)")


# --- #264: the review-authorities module must stay off every gate's dep graph -

def _local_module_index(scripts: Path) -> dict[str, Path]:
    """Every importable local module name -> file, as gates would import it.

    Walks EVERY package under scripts/ rather than a hand-listed few: gates
    import `emitters` as well as `chief_wiggum`/`quality`, and a package missing
    from this index is a hole in the closure — its imports are never followed,
    so anything it reaches looks unreachable.
    """
    index: dict[str, Path] = {}
    for py in scripts.rglob("*.py"):
        rel = py.relative_to(scripts)
        parts = list(rel.parts[:-1])
        if any(p.startswith(".") or p == "__pycache__" for p in parts):
            continue
        if py.stem == "__init__":
            if parts:
                index.setdefault(".".join(parts), py)
            continue
        index.setdefault(".".join([*parts, py.stem]), py)
    return index


def _imports_of(path: Path, index: dict[str, Path], root: Path | None = None) -> set[str]:
    """Local modules `path` imports, top-level OR lazily inside a function.

    AST rather than regex: this must catch `import review_authorities` and
    `from review_authorities import load` on a FLAT top-level module, which the
    package-shaped _CW_IMPORT_RE / _QUALITY_IMPORT_RE do not match at all.
    """
    out: set[str] = set()
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: `from . import x` inside a package
                pkg = ".".join(path.relative_to(root or SCRIPTS).parts[:-1])
                base = f"{pkg}.{node.module}" if node.module else pkg
                names = [base] + [f"{base}.{a.name}" for a in node.names]
            else:
                base = node.module or ""
                names = [base] + [f"{base}.{a.name}" for a in node.names if base]
        else:
            continue
        out.update(n for n in names if n in index)
    return out


@pytest.mark.parametrize("gate", SCANNER_VERSION_GATES)
def test_no_gate_scanner_can_reach_review_authorities(gate):
    """#264: `review_authorities` must never enter a gate's dependency graph.

    `scanner_version` hashes a gate's source PLUS its deps, so an import here —
    direct, or transitive via `artifacts.py`, which every one of these gates
    already imports — would stale that gate's validation record on every edit to
    an operator-authored convention file that has nothing to do with scanning.
    Walked transitively on purpose: checking only direct imports would miss the
    single most likely regression, someone adding the feature to artifacts.py.
    """
    scripts = SCRIPTS
    index = _local_module_index(scripts)
    assert "review_authorities" in index, "module missing from scripts/"

    seen: set[str] = set()
    stack = [gate]
    path_to: dict[str, list[str]] = {gate: [gate]}
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        for dep in sorted(_imports_of(index[mod], index, scripts)):
            if dep not in path_to:
                path_to[dep] = path_to[mod] + [dep]
            if dep not in seen:
                stack.append(dep)

    assert "review_authorities" not in seen, (
        f"{gate} reaches review_authorities via "
        f"{' -> '.join(path_to.get('review_authorities', []))} — that puts an "
        "operator-authored convention file into the gate's scanner_version hash "
        "inputs, staling its validation record for changes unrelated to scanning "
        "(chief-wiggum#264)")


def test_the_import_graph_walk_actually_detects_a_transitive_import(tmp_path):
    """The guard above is only worth having if it can fail. Prove the walk sees
    a dependency two hops away, not just a direct import."""
    (tmp_path / "gatey.py").write_text("import middle\n")
    (tmp_path / "middle.py").write_text("from review_authorities import load\n")
    (tmp_path / "review_authorities.py").write_text("def load(): ...\n")
    index = _local_module_index(tmp_path)
    assert _imports_of(tmp_path / "gatey.py", index, tmp_path) == {"middle"}
    assert "review_authorities" in _imports_of(tmp_path / "middle.py", index, tmp_path)


def test_the_module_index_covers_every_package_under_scripts():
    """Review finding (codex P2). The index once listed chief_wiggum/quality by
    hand, so `emitters` — which check_traceability and check_single_writer both
    import — was absent, and anything reachable THROUGH it was invisible to the
    closure. A package missing here is a hole in the guard, not a smaller guard.
    """
    index = _local_module_index(SCRIPTS)
    for pkg_dir in SCRIPTS.iterdir():
        if not pkg_dir.is_dir() or pkg_dir.name.startswith((".", "__")):
            continue
        if not (pkg_dir / "__init__.py").exists():
            continue
        assert pkg_dir.name in index, f"package {pkg_dir.name} missing from the index"
    assert "emitters" in index


def test_a_package_hop_is_followed(tmp_path):
    """`gate -> emitters -> review_authorities` must be reachable."""
    (tmp_path / "gatey.py").write_text("import pkg\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("from . import inner\n")
    (tmp_path / "pkg" / "inner.py").write_text("import review_authorities\n")
    (tmp_path / "review_authorities.py").write_text("def load(): ...\n")
    index = _local_module_index(tmp_path)
    assert index["pkg"] == tmp_path / "pkg" / "__init__.py"
    assert "pkg.inner" in index
