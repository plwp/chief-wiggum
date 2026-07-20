"""Tests for scripts/ratchet.py."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ratchet  # noqa: E402

# ---- fixtures -----------------------------------------------------------------


def make_repo(tmp_path, contracts_md=None, suites=None):
    """Lay out a minimal target repo with a ratchet config."""
    epic = tmp_path / "docs" / "epics" / "order-lifecycle"
    epic.mkdir(parents=True)
    (epic / "contracts.md").write_text(
        contracts_md
        or "### CTR-order-001 — valid date range\n"
        "REQUIRES: start_date <= end_date\n"
        "\n"
        "### INV-order-002 — totals non-negative\n"
        "**INV-order-002**: order.total_cents >= 0\n"
    )
    state = tmp_path / "docs" / "quality"
    state.mkdir(parents=True)
    (state / "ratchet.json").write_text(json.dumps({
        "suites": suites or [],
        "epic_docs": "docs/epics",
        "protected_paths": ratchet.DEFAULT_PROTECTED,
    }))
    return ratchet.load_config(tmp_path)


def test_uppercase_stable_ids_are_hashed(tmp_path):
    """Regression (chief-wiggum#86 class): uppercase INV-/CTR- ids must be detected
    for weakening-hashing, not silently skipped by a lowercase-only grammar.
    Keys are CANONICAL (uppercase kind, lowercase slug — PR #181 review) so they
    join against the traceability scanner's canonicalized annotation targets."""
    cfg = make_repo(
        tmp_path,
        contracts_md=(
            "### CTR-BIL-001 — customer uniqueness\n"
            "REQUIRES: one customer per provider\n"
            "\n"
            "- **INV-FOWR-004** — unknown price id is fatal, no floor fallback\n"
        ),
    )
    hashes = ratchet.load_contract_hashes(cfg)
    assert "CTR-bil-001" in hashes
    assert "INV-fowr-004" in hashes
    # raw-cased keys must NOT appear — one canonical key per declared ID
    assert "CTR-BIL-001" not in hashes and "INV-FOWR-004" not in hashes


def test_highwater_from_precanonicalization_journal_is_not_falsely_removed(tmp_path):
    """Back-compat (PR #181 review): journals written before hash keys were
    canonicalized carry raw-cased IDs (CTR-BIL-001). derive_highwater/violations
    must canonicalize both sides of the join, or every such contract would
    falsely read as *removed* against a new canonical scorecard and block."""
    cfg = make_repo(
        tmp_path,
        contracts_md="### CTR-BIL-001 — customer uniqueness\nREQUIRES: one customer per provider\n",
    )
    current = scorecard_from(cfg, set())  # canonical keys: CTR-bil-001
    # Old-style journal record: same hash VALUE (it covers block content only),
    # raw-cased KEY — exactly what a pre-#181 scorecard recorded.
    old_sc = dict(current)
    old_sc["contract_hashes"] = {"CTR-BIL-001": current["contract_hashes"]["CTR-bil-001"]}
    append_record(cfg, old_sc, merged=True)
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert set(hw["contract_hashes"]) == {"CTR-bil-001"}
    v = ratchet.violations(current, hw)
    assert v["removed_contracts"] == [] and v["weakened_contracts"] == []


def scorecard_from(cfg, pass_set):
    return {
        "passed": len(pass_set),
        "pass_set": sorted(pass_set),
        "contract_hashes": ratchet.load_contract_hashes(cfg),
        "tests_run": True,
    }


def append_record(cfg, sc, merged=True, amended=None, retired=None):
    records = ratchet.load_journal(cfg)
    body = {
        "record_id": f"rec-{len(records) + 1:05d}",
        "event": "ticket",
        "ref": "#1",
        "gate_result": "pass",
        "merged": merged,
        "scorecard": sc,
        "amended": amended or {},
        "retired": retired or [],
        "ratchet_status": "held",
        "notes": "",
    }
    prev = records[-1]["record_hash"] if records else "genesis"
    body["record_hash"] = ratchet.stable_hash(prev, json.dumps(body, sort_keys=True))
    cfg.journal.parent.mkdir(parents=True, exist_ok=True)
    with cfg.journal.open("a") as f:
        f.write(json.dumps(body, sort_keys=True) + "\n")
    return body


# ---- contract definition hashing ------------------------------------------------


def test_contract_hashes_found_in_markdown_and_json(tmp_path):
    cfg = make_repo(tmp_path)
    models = tmp_path / "docs" / "epics" / "order-lifecycle" / "models"
    models.mkdir()
    (models / "contracts.json").write_text(json.dumps({
        "contracts": [{"id": "CTR-order-003", "requires": ["qty > 0"]}],
    }))
    hashes = ratchet.load_contract_hashes(cfg)
    assert set(hashes) == {"CTR-order-001", "INV-order-002", "CTR-order-003"}


def test_reformatting_does_not_change_hash_but_rewording_does(tmp_path):
    cfg = make_repo(tmp_path)
    before = ratchet.load_contract_hashes(cfg)
    md = tmp_path / "docs" / "epics" / "order-lifecycle" / "contracts.md"
    # trailing whitespace only — normalized away
    md.write_text(md.read_text().replace("end_date\n", "end_date   \n"))
    assert ratchet.load_contract_hashes(cfg)["CTR-order-001"] == before["CTR-order-001"]
    # weakening the condition changes the hash
    md.write_text(md.read_text().replace("start_date <= end_date", "True"))
    after = ratchet.load_contract_hashes(cfg)
    assert after["CTR-order-001"] != before["CTR-order-001"]
    assert after["INV-order-002"] == before["INV-order-002"]


# ---- high-water derivation + violations ----------------------------------------


def test_merged_records_grow_highwater_unmerged_do_not(tmp_path):
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::t2"}), merged=True)
    append_record(cfg, scorecard_from(cfg, {"s::t3"}), merged=False)
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert hw["pass_set"] == ["s::t1", "s::t2"]
    assert set(hw["contract_hashes"]) == {"CTR-order-001", "INV-order-002"}


def test_regression_and_weakening_detected(tmp_path):
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::t2"}))
    md = tmp_path / "docs" / "epics" / "order-lifecycle" / "contracts.md"
    md.write_text(md.read_text().replace("start_date <= end_date", "True"))
    current = scorecard_from(cfg, {"s::t1"})  # t2 regressed, CTR weakened
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    v = ratchet.violations(current, hw)
    assert v["missing_tests"] == ["s::t2"]
    assert v["weakened_contracts"] == ["CTR-order-001"]
    assert v["removed_contracts"] == []


def test_removed_contract_detected(tmp_path):
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, set()))
    md = tmp_path / "docs" / "epics" / "order-lifecycle" / "contracts.md"
    md.write_text("### CTR-order-001 — valid date range\nREQUIRES: start_date <= end_date\n")
    v = ratchet.violations(
        scorecard_from(cfg, set()), ratchet.derive_highwater(ratchet.load_journal(cfg))
    )
    assert v["removed_contracts"] == ["INV-order-002"]


def test_amend_and_retire_move_the_baseline(tmp_path):
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, set()))
    md = tmp_path / "docs" / "epics" / "order-lifecycle" / "contracts.md"
    md.write_text(md.read_text().replace("start_date <= end_date", "start_date < end_date"))
    sc = scorecard_from(cfg, set())
    # journaled human decision: accept the revised CTR, retire the INV
    append_record(
        cfg, sc,
        amended={"CTR-order-001": sc["contract_hashes"]["CTR-order-001"]},
        retired=["INV-order-002"],
    )
    # the amended definition is now the baseline; the retired INV is gone
    v = ratchet.violations(
        scorecard_from(cfg, set()), ratchet.derive_highwater(ratchet.load_journal(cfg))
    )
    assert v["weakened_contracts"] == []
    assert v["removed_contracts"] == []


# ---- tamper evidence -------------------------------------------------------------


def test_journal_tamper_fails_closed(tmp_path):
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::t2"}))
    # lower the bar: rewrite the record's pass_set without re-chaining
    doctored = json.loads(cfg.journal.read_text())
    doctored["scorecard"]["pass_set"] = ["s::t1"]
    cfg.journal.write_text(json.dumps(doctored, sort_keys=True) + "\n")
    with pytest.raises(ratchet.TamperError):
        ratchet.load_journal(cfg)


def test_chain_of_multiple_records_verifies(tmp_path):
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1"}))
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::t2"}))
    assert len(ratchet.load_journal(cfg)) == 2


# ---- suite parsers ----------------------------------------------------------------


def test_parse_go_test_json():
    out = "\n".join([
        json.dumps({"Package": "pkg/a", "Test": "TestX", "Action": "pass"}),
        json.dumps({"Package": "pkg/a", "Test": "TestY", "Action": "fail"}),
        json.dumps({"Package": "pkg/a", "Action": "pass"}),  # package event, no Test
        "ok  pkg/a 0.1s",  # non-JSON noise
    ])
    assert ratchet.parse_go_test_json(out) == {"pkg/a::TestX"}


def test_parse_junit_xml():
    xml = (
        '<testsuite><testcase classname="a.b" name="t1"/>'
        '<testcase classname="a.b" name="t2"><failure/></testcase>'
        '<testcase classname="a.b" name="t3"><skipped/></testcase></testsuite>'
    )
    assert ratchet.parse_junit_xml(xml) == {"a.b::t1"}


def test_parse_pass_fail_lines():
    assert ratchet.parse_pass_fail_lines("PASS case-a\nFAIL case-b\nnoise\nPASS: case-c\n") == {
        "case-a", "case-c",
    }


def test_run_suite_namespaces_cases(tmp_path):
    cfg = make_repo(tmp_path, suites=[
        {"name": "smoke", "cmd": "printf 'PASS one\\nPASS two\\n'", "cwd": ".", "parser": "pass-fail-lines"},
    ])
    assert ratchet.run_suite(cfg, cfg.suites[0]) == {"smoke::one", "smoke::two"}


# ---- protected pathset -------------------------------------------------------------


def test_protected_hits_matches_goalpost_files(tmp_path):
    cfg = make_repo(tmp_path)
    changed = [
        "docs/epics/order-lifecycle/contracts.md",
        "docs/epics/order-lifecycle/models/contracts.json",
        "docs/quality/ratchet-journal.jsonl",
        "internal/orders/service.go",
        "docs/epics/order-lifecycle/retrospective.md",
    ]
    assert ratchet.protected_hits(cfg, changed) == [
        "docs/epics/order-lifecycle/contracts.md",
        "docs/epics/order-lifecycle/models/contracts.json",
        "docs/quality/ratchet-journal.jsonl",
    ]


# ---- complexity + churn (report-only dimension) --------------------------------


def quality_scorecard(cfg, pass_set, quality):
    sc = scorecard_from(cfg, pass_set)
    sc["quality"] = quality
    return sc


def test_quality_block_is_recorded_and_hash_chained(tmp_path):
    """A quality block rides inside the scorecard, so it is covered by the
    per-record hash and survives chain verification untouched."""
    cfg = make_repo(tmp_path)
    q = {"functions": 100, "total_loc": 5000, "ccn_mean": 3.1,
         "pct_ccn_gt10": 4.0, "relative_churn": 0.2, "churned_loc": 1000}
    append_record(cfg, quality_scorecard(cfg, {"s::t1"}, q))
    recs = ratchet.load_journal(cfg)  # raises TamperError if the chain is broken
    assert recs[0]["scorecard"]["quality"]["ccn_mean"] == 3.1


def test_quality_highwater_is_the_lowest_merged_value(tmp_path):
    """Direction check: complexity ratchets DOWN — best-seen = the minimum."""
    cfg = make_repo(tmp_path)
    append_record(cfg, quality_scorecard(cfg, set(),
        {"ccn_mean": 5.2, "pct_ccn_gt10": 16.7, "relative_churn": 0.4}), merged=True)
    append_record(cfg, quality_scorecard(cfg, set(),
        {"ccn_mean": 3.1, "pct_ccn_gt10": 4.0, "relative_churn": 0.2}), merged=True)
    # a WORSE unmerged snapshot must not pollute the high-water mark
    append_record(cfg, quality_scorecard(cfg, set(),
        {"ccn_mean": 9.9, "pct_ccn_gt10": 40.0, "relative_churn": 0.9}), merged=False)
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))["quality"]
    assert hw == {"ccn_mean": 3.1, "pct_ccn_gt10": 4.0, "relative_churn": 0.2}


def test_quality_regression_when_complexity_rises_beyond_tolerance(tmp_path):
    cfg = make_repo(tmp_path)
    append_record(cfg, quality_scorecard(cfg, set(),
        {"ccn_mean": 3.0, "pct_ccn_gt10": 4.0, "relative_churn": 0.2}), merged=True)
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))["quality"]
    # within band (3.0 * 1.1 + 0.5 = 3.8): no regression
    ok = {"ccn_mean": 3.7, "pct_ccn_gt10": 4.0, "relative_churn": 0.2}
    assert ratchet.quality_regressions(ok, hw, cfg.quality_tolerance) == []
    # beyond band: 5.2 > 3.8 -> ccn_mean regresses
    bad = {"ccn_mean": 5.2, "pct_ccn_gt10": 4.0, "relative_churn": 0.2}
    regs = ratchet.quality_regressions(bad, hw, cfg.quality_tolerance)
    assert [r["metric"] for r in regs] == ["ccn_mean"]
    assert regs[0]["best"] == 3.0 and regs[0]["current"] == 5.2


def _write_scorecard(cfg, sc):
    ratchet._write_json(cfg.scorecard, sc)


def test_check_quality_regression_is_report_only_by_default(tmp_path, capsys):
    """A complexity regression prints but MUST NOT change check's exit code
    unless --gate-quality is passed — the pass-set/contract gates are unchanged."""
    cfg = make_repo(tmp_path)
    append_record(cfg, quality_scorecard(cfg, {"s::t1"},
        {"ccn_mean": 3.0, "pct_ccn_gt10": 4.0, "relative_churn": 0.2}), merged=True)
    # current snapshot: pass-set intact, but complexity blew past the band
    _write_scorecard(cfg, quality_scorecard(cfg, {"s::t1"},
        {"ccn_mean": 9.9, "pct_ccn_gt10": 40.0, "relative_churn": 0.9}))

    report = argparse.Namespace(repo=str(tmp_path), format="text", gate_quality=False)
    assert ratchet.cmd_check(report) == 0  # report-only: exits OK
    assert "regressions" in capsys.readouterr().err

    gated = argparse.Namespace(repo=str(tmp_path), format="text", gate_quality=True)
    assert ratchet.cmd_check(gated) == 1  # opt-in gate: now blocks


def test_check_pass_set_gate_unchanged_by_quality(tmp_path):
    """Existing blocking behavior is preserved: a missing high-water test still
    exits 1 regardless of the (absent/held) quality dimension."""
    cfg = make_repo(tmp_path)
    append_record(cfg, quality_scorecard(cfg, {"s::t1", "s::t2"},
        {"ccn_mean": 3.0, "pct_ccn_gt10": 4.0, "relative_churn": 0.2}), merged=True)
    _write_scorecard(cfg, quality_scorecard(cfg, {"s::t1"},  # t2 regressed
        {"ccn_mean": 3.0, "pct_ccn_gt10": 4.0, "relative_churn": 0.2}))
    args = argparse.Namespace(repo=str(tmp_path), format="text", gate_quality=False)
    assert ratchet.cmd_check(args) == 1


def test_backward_compat_journal_without_quality(tmp_path):
    """Pre-existing records carry no quality block. Chain verification, high-water
    derivation, and regression checks must all tolerate that and not crash."""
    cfg = make_repo(tmp_path)
    # scorecard_from() deliberately omits the quality field (old shape)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::t2"}), merged=True)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::t2", "s::t3"}), merged=True)
    recs = ratchet.load_journal(cfg)  # verifies fine
    hw = ratchet.derive_highwater(recs)
    assert hw["pass_set"] == ["s::t1", "s::t2", "s::t3"]
    assert hw["quality"] == {}  # no quality high-water derivable — empty, not error
    # a current snapshot without quality yields no quality regressions
    assert ratchet.quality_regressions({}, hw["quality"], cfg.quality_tolerance) == []


def test_skipped_quality_snapshot_never_regresses(tmp_path):
    """If lizard was absent, the snapshot is {'skipped': ...}; it must be inert
    for both high-water derivation and regression detection."""
    cfg = make_repo(tmp_path)
    append_record(cfg, quality_scorecard(cfg, set(),
        {"ccn_mean": 3.0, "pct_ccn_gt10": 4.0, "relative_churn": 0.2}), merged=True)
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))["quality"]
    assert ratchet.quality_regressions({"skipped": "lizard not found"}, hw,
                                       cfg.quality_tolerance) == []
    # and a skipped record contributes nothing to the high-water mark
    append_record(cfg, quality_scorecard(cfg, set(),
        {"skipped": "lizard not found"}), merged=True)
    hw2 = ratchet.derive_highwater(ratchet.load_journal(cfg))["quality"]
    assert hw2 == {"ccn_mean": 3.0, "pct_ccn_gt10": 4.0, "relative_churn": 0.2}


# ---- suspect-link visibility (#169) ----------------------------------------


def _write_sidecar(cfg, links):
    from chief_wiggum.trace_links import write_sidecar
    write_sidecar(cfg.repo / ratchet.SIDECAR_RELPATH, {"links": links})


def test_suspect_links_for_flags_a_changed_contract_hash(tmp_path):
    cfg = make_repo(tmp_path)
    hashes = ratchet.load_contract_hashes(cfg)
    _write_sidecar(cfg, [{
        "verb": "guards", "target": "CTR-order-001", "file": "order.go", "line": 10,
        "source_kind": "code", "definition_hash": "stale-hash",
    }])
    sc = scorecard_from(cfg, set())
    assert hashes["CTR-order-001"] != "stale-hash"
    susp = ratchet.suspect_links_for(cfg, sc)
    assert len(susp) == 1
    assert susp[0]["target"] == "CTR-order-001"


def test_suspect_links_for_is_empty_when_hash_matches(tmp_path):
    cfg = make_repo(tmp_path)
    hashes = ratchet.load_contract_hashes(cfg)
    _write_sidecar(cfg, [{
        "verb": "guards", "target": "CTR-order-001", "file": "order.go", "line": 10,
        "source_kind": "code", "definition_hash": hashes["CTR-order-001"],
    }])
    sc = scorecard_from(cfg, set())
    assert ratchet.suspect_links_for(cfg, sc) == []


def test_suspect_links_for_is_empty_when_no_sidecar_written(tmp_path):
    cfg = make_repo(tmp_path)
    sc = scorecard_from(cfg, set())
    assert ratchet.suspect_links_for(cfg, sc) == []


def test_cmd_check_surfaces_suspect_links_visibly_but_does_not_block(tmp_path, capsys):
    """AC3 (#169): a definition-hash change with surviving suspect links must be
    VISIBLE in `check`'s output, never silently absorbed into 'the ratchet held'
    — but suspect propagation ships report-only, so it must not change the exit
    code (docs/gate-rollout.md)."""
    cfg = make_repo(tmp_path)
    hashes = ratchet.load_contract_hashes(cfg)
    _write_sidecar(cfg, [{
        "verb": "guards", "target": "CTR-order-001", "file": "order.go", "line": 10,
        "source_kind": "code", "definition_hash": "stale-hash",
    }])
    sc = scorecard_from(cfg, set())
    append_record(cfg, sc, merged=True)
    _write_scorecard(cfg, sc)
    assert hashes["CTR-order-001"] != "stale-hash"

    args = argparse.Namespace(repo=str(tmp_path), format="text", gate_quality=False)
    assert ratchet.cmd_check(args) == 0  # visible, but does not block
    err = capsys.readouterr().err
    assert "suspect link" in err
    assert "CTR-order-001" in err

    args_json = argparse.Namespace(repo=str(tmp_path), format="json", gate_quality=False)
    ratchet.cmd_check(args_json)
    data = json.loads(capsys.readouterr().out)
    assert data["suspect_links"][0]["target"] == "CTR-order-001"


def test_cmd_regressed_includes_suspect_links(tmp_path, capsys):
    cfg = make_repo(tmp_path)
    _write_sidecar(cfg, [{
        "verb": "guards", "target": "CTR-order-001", "file": "order.go", "line": 10,
        "source_kind": "code", "definition_hash": "stale-hash",
    }])
    sc = scorecard_from(cfg, set())
    append_record(cfg, sc, merged=True)
    _write_scorecard(cfg, sc)
    args = argparse.Namespace(repo=str(tmp_path))
    ratchet.cmd_regressed(args)
    data = json.loads(capsys.readouterr().out)
    assert data["suspect_links"][0]["target"] == "CTR-order-001"


@pytest.mark.skipif(shutil.which("lizard") is None,
                    reason="lizard required for the end-to-end quality snapshot")
def test_score_quality_end_to_end_on_a_real_repo(tmp_path):
    """score_quality runs the code-metrics engines against chief-wiggum itself."""
    repo = Path(__file__).resolve().parent.parent  # chief-wiggum repo root
    cfg = make_repo(tmp_path)
    cfg.repo = repo
    q = ratchet.score_quality(cfg)
    assert "skipped" not in q, q
    assert q["functions"] > 0 and q["ccn_mean"] is not None
    assert q["total_loc"] > 0 and 0 <= q["pct_ccn_gt10"] <= 100
    # relative_churn requires git history; chief-wiggum has plenty
    assert q["relative_churn"] is None or q["relative_churn"] >= 0


# ---- gate-validation event (docs/gate-validation.md, #168) --------------------


def test_record_accepts_gate_validation_event(tmp_path):
    """`ratchet.py record --event gate-validation` journals a gate-validation-protocol
    run — --ref names the gate, per docs/gate-validation.md's "Recording results"."""
    cfg = make_repo(tmp_path)
    subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "score", "--repo", str(tmp_path),
         "--no-tests", "--no-quality"],
        capture_output=True, text=True, check=True,
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "gate-validation", "--ref", "check_single_writer", "--merged",
         "--notes", "seeded-defect + clean-corpus trials passed"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    records = ratchet.load_journal(cfg)
    assert len(records) == 1
    assert records[0]["event"] == "gate-validation"
    assert records[0]["ref"] == "check_single_writer"


def test_record_rejects_unknown_event(tmp_path):
    make_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "not-a-real-event", "--ref", "x"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0


def test_it_fh_06_real_journal_corroborates_stale_while_blocking_demotion(tmp_path):
    """IT-fh-06 (chief-wiggum#198) through the REAL `ratchet.py record` CLI —
    not a hand-written journal fixture. Journal a gate-validation event for
    real, author a validation record whose ratchet_record_id names it, wire the
    gate blocking, then simulate #184's scenario (a scanner edit bumps
    --scanner-version) and prove check_gate_validation.check_and_transition
    auto-demotes it (blocking -> demoted), recording previous_authority."""
    import check_gate_validation as gv  # noqa: PLC0415

    cfg = make_repo(tmp_path)
    subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "score", "--repo", str(tmp_path),
         "--no-tests", "--no-quality"],
        capture_output=True, text=True, check=True,
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "gate-validation", "--ref", "example_gate", "--merged",
         "--notes", "IT-fh-06 fixture"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    record_id = ratchet.load_journal(cfg)[0]["record_id"]

    validation_dir = cfg.journal.parent / "validation"
    validation_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir = tmp_path / "fake_scripts"

    def _write_fake_gate(version: str) -> None:
        scripts_dir.mkdir(exist_ok=True)
        (scripts_dir / "example_gate.py").write_text(
            "import sys\n"
            "if '--scanner-version' in sys.argv:\n"
            f"    print({version!r})\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(2)\n"
        )

    _write_fake_gate("v1")
    record = {
        "gate": "example_gate",
        "protocol_version": "1",
        "scanner_version": "v1",
        "telemetry_dependent": False,
        "concurrency_applicable": False,
        "concurrency_note": "static analysis has no concurrent dimension",
        "authority_boundary": {"proves": "fixture", "artifact": "fixture", "assumptions": ["fixture"]},
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
            {"repo": "r", "sha": "abc", "findings": 0, "coverage": {"n": 1}, "passed": True},
        ],
        "status": "passed",
        "ratchet_record_id": record_id,
    }
    (validation_dir / "example_gate.json").write_text(json.dumps(record))

    report, transition = gv.check_and_transition(
        "example_gate", validation_dir, scripts_dir=scripts_dir, wire=True,
    )
    assert report.passing is True, (report.provenance_errors, report.schema_errors)
    assert transition.new_state == "blocking"

    _write_fake_gate("v2-after-scanner-edit")  # #184: a scanner edit bumps --scanner-version

    report2, transition2 = gv.check_and_transition(
        "example_gate", validation_dir, scripts_dir=scripts_dir,
    )
    assert report2.passing is False
    assert transition2.previous_state == "blocking"
    assert transition2.new_state == "demoted"
    assert transition2.demotion_reason == "stale"
    assert transition2.previous_authority == "blocking"


# ---- gate-authority journal primitives: tamper-tolerance (chief-wiggum#198) ----


def _chain(journal_path: Path, bodies: list[dict]) -> None:
    """Write a hash-chained journal (same chaining as ratchet.append_authority_event)."""
    from chief_wiggum.hashing import stable_hash  # noqa: PLC0415
    prev = "genesis"
    lines = []
    for body in bodies:
        body = {k: v for k, v in body.items() if k != "record_hash"}
        body["record_hash"] = stable_hash(prev, json.dumps(body, sort_keys=True))
        prev = body["record_hash"]
        lines.append(json.dumps(body, sort_keys=True))
    journal_path.write_text("\n".join(lines) + "\n")


def test_last_authority_action_ignores_bogus_details_after_wire(tmp_path):
    """FINDING 1: a hash-VALID gate-authority event carrying a bogus `details`
    (e.g. 'noop') after a real wire must NOT flip the gate to un-wired — only
    'wire'/'unwire' are authority actions; anything else is ignored, so the
    prior genuine wire still stands."""
    journal = tmp_path / "ratchet-journal.jsonl"
    _chain(journal, [
        {"record_id": "rec-00001", "event": "gate-authority", "ref": "g", "details": "wire"},
        {"record_id": "rec-00002", "event": "gate-authority", "ref": "g", "details": "noop"},
    ])
    # The bogus 'noop' is ignored; the last GENUINE action is still 'wire'.
    assert ratchet.last_authority_action(journal, "g") == "wire"


def test_last_authority_action_respects_a_real_unwire(tmp_path):
    """Control for finding 1: a genuine 'unwire' after a 'wire' DOES un-wire —
    the filter ignores only non-action details, never a real unwire."""
    journal = tmp_path / "ratchet-journal.jsonl"
    _chain(journal, [
        {"record_id": "rec-00001", "event": "gate-authority", "ref": "g", "details": "wire"},
        {"record_id": "rec-00002", "event": "gate-authority", "ref": "g", "details": "unwire"},
    ])
    assert ratchet.last_authority_action(journal, "g") == "unwire"


def test_verified_prefix_stops_before_a_non_json_trailing_line(tmp_path):
    """FINDING 2: malformed JSON in the journal tail after a valid wire must not
    crash the read — verified_prefix parses line-by-line and stops before the
    first unparseable line, so the wire is still read (and a stale record still
    demotes)."""
    journal = tmp_path / "ratchet-journal.jsonl"
    _chain(journal, [
        {"record_id": "rec-00001", "event": "gate-authority", "ref": "g", "details": "wire"},
    ])
    # Corrupt the tail with a non-JSON garbage line.
    with journal.open("a") as f:
        f.write("this is not json at all\n")

    prefix = ratchet.verified_prefix(journal)
    assert len(prefix) == 1  # the wire survives; the garbage tail is dropped
    assert ratchet.last_authority_action(journal, "g") == "wire"


def test_append_authority_event_fails_closed_on_garbled_tail(tmp_path):
    """A garbled tail is a broken chain: append_authority_event must raise
    TamperError (never a JSONDecodeError crash) so callers can handle it."""
    journal = tmp_path / "ratchet-journal.jsonl"
    _chain(journal, [
        {"record_id": "rec-00001", "event": "gate-authority", "ref": "g", "details": "wire"},
    ])
    with journal.open("a") as f:
        f.write("garbage\n")
    with pytest.raises(ratchet.TamperError):
        ratchet.append_authority_event(journal, "g", "unwire")


# ---- --scanner-version (#184) --------------------------------------------------


def test_cli_scanner_version_prints_hex_digest_with_no_subcommand():
    # ratchet's CLI is subcommand-based (dest="cmd", required=True); --scanner-version
    # must still work standalone, with no subcommand and no side effects.
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "--scanner-version"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    assert len(out) == 64  # sha256 hex digest
    int(out, 16)  # valid hex


def test_scanner_version_is_deterministic_and_stable_across_calls():
    assert ratchet._scanner_version() == ratchet._scanner_version()
