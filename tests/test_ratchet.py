"""Tests for scripts/ratchet.py."""

import json
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
