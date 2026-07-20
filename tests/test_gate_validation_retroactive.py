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
    """Re-score the mutated fixture repo, then read `ratchet check`'s JSON."""
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
                + len(rep["removed_contracts"]))
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


RT_EXECUTORS = {
    "rt-direct-01": _rt_seed_direct,
    "rt-omission-01": _rt_seed_omission,
    "rt-config-indirection-01": _rt_seed_config_indirection,
    "rt-sampling-gap-01": _rt_seed_sampling_gap,
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
                     "contracts_hashed": len(sc["contract_hashes"])}
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
