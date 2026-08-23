"""Tests for scripts/ratchet.py."""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import date
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


def append_record(cfg, sc, merged=True, amended=None, retired=None, retired_cases=None):
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
        "retired_cases": retired_cases or [],
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


# ---- #295: vacuous contract-hash gate for an epic the grammar can't parse ------
#
# hash_epic_definitions returns {} for an epic whose ids are two-segment
# (INV-001, the /architect skill's own #281 worked example) — "contracts
# cannot be weakened" then holds vacuously (over an empty set), and no
# weakened_contracts/removed_contracts finding is ever raised. Direct instance
# of chief-wiggum#289 ("something that failed to run renders as a pass"), one
# layer up from #281 (which fixed the SAME shape for check_traceability.py).
# near_miss_ids (landed by #281 in chief_wiggum.trace_ids) is reused here —
# not a second detector.


def _score_ns(tmp_path, **overrides):
    base = dict(repo=str(tmp_path), no_tests=True, no_quality=True, venv=None, gobin=None)
    base.update(overrides)
    return argparse.Namespace(**base)


def _check_ns(tmp_path, **overrides):
    base = dict(repo=str(tmp_path), format="text", gate_verifier_tests=False, gate_quality=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def _two_segment_epic_repo(tmp_path):
    """The #281/#295 case itself: invariants.md is present, has content, and
    declares ONLY the two-segment INV-001 shape the grammar cannot see."""
    epic = tmp_path / "docs" / "epics" / "order-lifecycle"
    epic.mkdir(parents=True)
    (epic / "invariants.md").write_text("## Epic Invariants\n\n1. **INV-001 — X**: y\n")
    state = tmp_path / "docs" / "quality"
    state.mkdir(parents=True)
    (state / "ratchet.json").write_text(json.dumps({
        "suites": [], "epic_docs": "docs/epics", "protected_paths": ratchet.DEFAULT_PROTECTED,
    }))
    return ratchet.load_config(tmp_path)


def test_two_segment_epic_hashes_zero_contracts(tmp_path):
    """Pins the bug's premise: hash_epic_definitions sees nothing."""
    cfg = _two_segment_epic_repo(tmp_path)
    assert ratchet.load_contract_hashes(cfg) == {}


def test_two_segment_epic_scores_as_error_not_clean(tmp_path):
    """AC1/AC2/AC3: artifacts present + zero hashed => 'error' status, the
    malformed id is NAMED (not just counted), and the scorecard shows the
    tracked/scanned denominator."""
    _two_segment_epic_repo(tmp_path)
    assert ratchet.cmd_score(_score_ns(tmp_path)) == 0
    sc = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())
    cm = sc["contract_measurement"]
    assert cm["status"] == "error"
    assert cm["defined_ids"] == 0
    assert cm["id_bearing_artifacts"] == 1
    assert cm["malformed_ids"], "the malformed id must be NAMED, not just counted"
    assert cm["malformed_ids"][0]["token"] == "INV-001"
    assert cm["malformed_ids"][0]["file"] == "order-lifecycle/invariants.md"


def test_two_segment_epic_is_never_a_clean_ratchet_check(tmp_path):
    """AC4/AC5: the regression itself — an epic the grammar can't parse must
    NOT produce a clean ratchet scorecard; `check` fails closed."""
    _two_segment_epic_repo(tmp_path)
    assert ratchet.cmd_score(_score_ns(tmp_path)) == 0
    assert ratchet.cmd_check(_check_ns(tmp_path)) == 1


def test_violations_reports_contract_measurement_error(tmp_path):
    _two_segment_epic_repo(tmp_path)
    ratchet.cmd_score(_score_ns(tmp_path))
    cfg = ratchet.load_config(tmp_path)
    sc = json.loads(cfg.scorecard.read_text())
    v = ratchet.violations(sc, ratchet.derive_highwater(ratchet.load_journal(cfg)))
    assert v["contract_measurement_error"]


def test_healthy_epic_stays_applicable_and_passes_check(tmp_path):
    """Regression: a normal, well-formed epic (three-segment ids) must NOT
    trip the new error state."""
    make_repo(tmp_path)
    assert ratchet.cmd_score(_score_ns(tmp_path)) == 0
    sc = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())
    assert sc["contract_measurement"]["status"] == "applicable"
    assert sc["contract_measurement"]["malformed_ids"] == []
    assert ratchet.cmd_check(_check_ns(tmp_path)) == 0


def test_no_epic_at_all_stays_inapplicable_not_error(tmp_path):
    """Nothing to measure (no docs/epics tree at all) must stay
    'inapplicable', never 'error' — only artifacts WITH content that yield
    zero ids are a broken instrument."""
    state = tmp_path / "docs" / "quality"
    state.mkdir(parents=True)
    (state / "ratchet.json").write_text(json.dumps({
        "suites": [], "epic_docs": "docs/epics", "protected_paths": ratchet.DEFAULT_PROTECTED,
    }))
    assert ratchet.cmd_score(_score_ns(tmp_path)) == 0
    sc = json.loads((state / ratchet.SCORECARD_NAME).read_text())
    assert sc["contract_measurement"]["status"] == "inapplicable"
    v = ratchet.violations(sc, ratchet.derive_highwater([]))
    assert v["contract_measurement_error"] == []
    assert ratchet.cmd_check(_check_ns(tmp_path)) == 0


# ---- broken instrument: the SUITE measured nothing (chief-wiggum#289) ----------
#
# #295 gave the contract-hash dimension a three-state measurement. The pass-set
# dimension — the ratchet's other half — had none: a suite command that died
# (OOM, kill, missing interpreter) or collected zero tests produced an empty
# pass-set, and `check` compared empty-against-empty and printed
# "ratchet: OK (pass-set and contract definitions hold the high-water mark)".


def _junit(cases: str) -> str:
    return f'<testsuites><testsuite name="s" tests="1">{cases}</testsuite></testsuites>'


_PASSING_JUNIT = _junit('<testcase classname="a.b" name="t1"/>')


def _suite_repo(tmp_path, cmd):
    return make_repo(tmp_path, suites=[{
        "name": "py", "cmd": cmd, "cwd": ".", "parser": "junit-xml",
        "report": "report.xml",
    }])


def test_dead_command_with_a_stale_report_is_not_a_pass_set(tmp_path):
    """The worst shape: a leftover report from an earlier run plus a command
    that dies writing nothing fabricates a NON-ZERO pass count out of stale
    bytes. TRX already pre-cleared for exactly this reason; junit did not."""
    cfg = _suite_repo(tmp_path, "exit 137")
    (tmp_path / "report.xml").write_text(_PASSING_JUNIT)
    with pytest.raises(ratchet.RatchetError):
        ratchet.run_suite(cfg, cfg.suites[0])


def test_unparseable_report_is_a_clean_error_not_a_traceback(tmp_path):
    """A truncated/zero-byte report reached ET.fromstring unguarded and exited
    with an ElementTree traceback — outside the documented 0/1/2/3/4 taxonomy,
    so no wrapper could classify it."""
    cfg = _suite_repo(tmp_path, "printf '' > report.xml")
    with pytest.raises(ratchet.RatchetError) as exc:
        ratchet.run_suite(cfg, cfg.suites[0])
    assert "report.xml" in str(exc.value)


def test_dead_command_scores_as_a_suite_measurement_error(tmp_path):
    """A reportless parser (go-test-json / pass-fail-lines) has no
    missing-report guard at all: a command that dies just yields an empty
    pass-set, which `check` compares against an empty high-water and calls
    OK."""
    make_repo(tmp_path, suites=[{
        "name": "smoke", "cmd": "exit 137", "cwd": ".", "parser": "pass-fail-lines",
    }])
    ratchet.cmd_score(_score_ns(tmp_path, no_tests=False))
    sc = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())
    assert sc["suite_measurement"]["status"] == "error"
    assert sc["suite_measurement"]["suites"][0]["exit_code"] == 137
    assert ratchet.cmd_check(_check_ns(tmp_path)) == 1


def test_zero_collected_tests_is_an_error_not_a_clean_run(tmp_path):
    """A wrong -k, an empty testpath, or a rootdir slip: the runner exits 0,
    the report parses, and ZERO cases come back. Before #289 that was a
    silent, warning-free green."""
    empty = '<testsuites><testsuite name="s" tests="0"></testsuite></testsuites>'
    _suite_repo(tmp_path, f"printf '{empty}' > report.xml")
    ratchet.cmd_score(_score_ns(tmp_path, no_tests=False))
    sc = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())
    sm = sc["suite_measurement"]
    assert sm["status"] == "error"
    assert sm["suites"][0]["passing_cases"] == 0
    assert sm["suites"][0]["suite"] == "py"


def test_suite_measurement_error_is_never_a_clean_ratchet_check(tmp_path):
    empty = '<testsuites><testsuite name="s" tests="0"></testsuite></testsuites>'
    _suite_repo(tmp_path, f"printf '{empty}' > report.xml")
    ratchet.cmd_score(_score_ns(tmp_path, no_tests=False))
    assert ratchet.cmd_check(_check_ns(tmp_path)) == 1


def test_violations_reports_suite_measurement_error(tmp_path):
    empty = '<testsuites><testsuite name="s" tests="0"></testsuite></testsuites>'
    _suite_repo(tmp_path, f"printf '{empty}' > report.xml")
    ratchet.cmd_score(_score_ns(tmp_path, no_tests=False))
    cfg = ratchet.load_config(tmp_path)
    sc = json.loads(cfg.scorecard.read_text())
    v = ratchet.violations(sc, ratchet.derive_highwater(ratchet.load_journal(cfg)))
    assert v["suite_measurement_error"]


def test_healthy_suite_stays_applicable_and_passes_check(tmp_path):
    """Regression: a suite that really ran and really passed must NOT trip the
    new error state."""
    _suite_repo(tmp_path, f"printf '{_PASSING_JUNIT}' > report.xml")
    ratchet.cmd_score(_score_ns(tmp_path, no_tests=False))
    sc = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())
    assert sc["suite_measurement"]["status"] == "applicable"
    assert sc["suite_measurement"]["suites"][0]["passing_cases"] == 1
    assert ratchet.cmd_check(_check_ns(tmp_path)) == 0


def test_failing_tests_are_findings_not_a_measurement_error(tmp_path):
    """Tests that RAN and FAILED are a real measurement — the pass-set ratchet
    handles them. Only a suite contributing zero passing cases is broken."""
    mixed = _junit('<testcase classname="a.b" name="t1"/>'
                   '<testcase classname="a.b" name="t2"><failure/></testcase>')
    _suite_repo(tmp_path, f"printf '{mixed}' > report.xml; exit 1")
    ratchet.cmd_score(_score_ns(tmp_path, no_tests=False))
    sc = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())
    assert sc["suite_measurement"]["status"] == "applicable"


def test_no_tests_flag_is_inapplicable_not_error(tmp_path):
    """--no-tests is an explicit operator choice, not a broken instrument — but
    it must not read as a measured green either."""
    _suite_repo(tmp_path, f"printf '{_PASSING_JUNIT}' > report.xml")
    ratchet.cmd_score(_score_ns(tmp_path, no_tests=True))
    sc = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())
    assert sc["suite_measurement"]["status"] == "inapplicable"
    assert ratchet.cmd_check(_check_ns(tmp_path)) == 0


def test_no_suites_configured_is_inapplicable_not_error(tmp_path):
    make_repo(tmp_path)
    ratchet.cmd_score(_score_ns(tmp_path, no_tests=False))
    sc = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())
    assert sc["suite_measurement"]["status"] == "inapplicable"
    assert ratchet.cmd_check(_check_ns(tmp_path)) == 0


def test_scorecard_predating_suite_measurement_is_tolerated(tmp_path):
    """An older scorecard carries no suite_measurement key: tolerated as empty,
    never read as 'error' (same rule #295 set for contract_measurement)."""
    make_repo(tmp_path)
    ratchet.cmd_score(_score_ns(tmp_path))
    cfg = ratchet.load_config(tmp_path)
    sc = json.loads(cfg.scorecard.read_text())
    del sc["suite_measurement"]
    v = ratchet.violations(sc, ratchet.derive_highwater([]))
    assert v["suite_measurement_error"] == []


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
    ids, files = ratchet.run_suite(cfg, cfg.suites[0])
    assert ids == {"smoke::one", "smoke::two"}
    assert files == {}  # pass-fail-lines carries no file info — unresolved, not guessed


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
            # instrument-broken is unconditionally mandatory (#289); without it
            # this synthetic record fails the gate-of-gates for the wrong
            # reason and the test can no longer observe what it is asserting.
            {"seed_id": "ib1", "seed_class": "instrument-broken", "repo": "r",
             "expected": "fire", "result": "fired", "passed": True},
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


def test_append_does_not_fuse_onto_an_unterminated_last_line(tmp_path):
    """chief-wiggum#420: a missing newline must not erase the journal.

    The broken-chain guard does not fire here, and that is the whole problem:
    a record that merely lost its trailing newline is complete, verifies, and
    the raw line count matches the verified prefix. Appending onto it fused
    both records, reported success, and left a line that no longer parses.

    Asserted on what a reader would then believe, not on the file bytes: a
    wired gate reading back as NEVER WIRED is the fail-open this protects.
    """
    journal = tmp_path / "ratchet-journal.jsonl"
    ratchet.append_authority_event(journal, "gate_a", "wire", wired_rid="rec-x")
    journal.write_text(journal.read_text().rstrip("\n"))
    assert not journal.read_text().endswith("\n")

    rid = ratchet.append_authority_event(journal, "gate_a", "unwire")
    assert rid == "rec-00002"
    assert len(ratchet.verified_prefix(journal)) == 2
    assert ratchet.last_authority_action(journal, "gate_a") == "unwire"


def test_a_corrupt_journal_line_reads_as_tamper_not_a_traceback(tmp_path):
    """The read path must hold the invariant the append path already states.

    Records fused onto one line by a bad merge or a hand edit produced an
    unhandled JSONDecodeError out of `load_journal`. It failed closed, so this
    is about the operator being told what is wrong rather than about whether
    the gate blocks: a named TamperError is actionable, a traceback is not.
    """
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=True)
    intact = cfg.journal.read_text().rstrip("\n")
    cfg.journal.write_text(intact + intact + "\n")  # two records, one line

    with pytest.raises(ratchet.TamperError, match="journal unreadable"):
        ratchet.load_journal(cfg)


def test_a_fresh_journal_gets_no_leading_blank_line(tmp_path):
    """The separator is for an unterminated PREVIOUS line; there is none."""
    journal = tmp_path / "ratchet-journal.jsonl"
    ratchet.append_authority_event(journal, "gate_a", "wire")
    assert not journal.read_text().startswith("\n")
    assert len(ratchet.verified_prefix(journal)) == 1


def test_cmd_record_also_separates_from_an_unterminated_last_line(tmp_path):
    """The `record` CLI is the second writer, and must not drift from the first.

    `append_authority_event` and `cmd_record` both append to this journal. The
    fix only holds if BOTH go through the shared append: covering one leaves
    the other free to fuse records again, which is how the two call sites
    diverge in the first place.
    """
    cfg = make_repo(tmp_path)
    scorecard = scorecard_from(cfg, {"s::t1"})
    append_record(cfg, scorecard, merged=True)
    _write_scorecard(cfg, scorecard)
    cfg.journal.write_text(cfg.journal.read_text().rstrip("\n"))
    assert not cfg.journal.read_text().endswith("\n")

    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--ref", "#420", "--gate", "pass"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    records = ratchet.verified_prefix(cfg.journal)
    assert len(records) == 2, [r.get("record_id") for r in records]
    # The earlier merged record still counts: the high-water did not reset.
    assert "s::t1" in ratchet.derive_highwater(records)["pass_set"]


def test_an_unterminated_scorecard_record_does_not_reset_the_high_water(tmp_path):
    """The same fusion on a `merged` record silently empties the pass-set.

    `derive_highwater` folds merged records; if the fused line stops parsing,
    the verified prefix is empty and the high-water mark reads as nothing ever
    having passed, so a suite that shrank no longer violates the ratchet.
    """
    journal = tmp_path / "ratchet-journal.jsonl"
    _chain(journal, [
        {"record_id": "rec-00001", "event": "suite", "ref": "v1", "merged": True,
         "scorecard": {"pass_set": ["tests/test_a.py::test_one"]}},
    ])
    journal.write_text(journal.read_text().rstrip("\n"))

    ratchet.append_journal_line(journal, {
        "record_id": "rec-00002", "event": "gate-authority", "ref": "g",
        "details": "wire", "merged": False, "record_hash": "unchained",
    })
    records = ratchet.verified_prefix(journal)
    assert records, "the merged record must survive the append"
    assert ratchet.derive_highwater(records)["pass_set"] == [
        "tests/test_a.py::test_one"]


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


def test_sidecar_election_routes_ratchet_state_dir(tmp_path, monkeypatch):
    """#213: a sidecar election moves the DEFAULT state dir (config, journal,
    scorecard) to the external quality dir — workers in the target tree
    physically cannot write it; no election keeps <repo>/docs/quality."""
    import artifacts

    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))
    repo = tmp_path / "target"
    repo.mkdir()
    # embedded status quo without an election
    assert ratchet.default_state_dir(repo) == repo / "docs" / "quality"
    artifacts.elect(repo, "sidecar")
    state = ratchet.default_state_dir(repo)
    assert state == artifacts.Resolver.resolve(repo).quality_dir()
    assert str(state).startswith(str(tmp_path / "cw-user"))
    assert ratchet.cmd_init(argparse.Namespace(repo=str(repo), force=False)) == 0
    assert (state / "ratchet.json").is_file()
    assert not (repo / "docs").exists()  # zero CW files in the target tree
    cfg = ratchet.load_config(repo)
    assert cfg.state_dir == state
    assert cfg.journal == state / "ratchet-journal.jsonl"


def test_sidecar_init_then_score_sees_sidecar_contracts(tmp_path, monkeypatch):
    """F1 regression: sidecar elect -> init -> score must hash the SIDECAR
    epic contracts (non-zero contract count) with NO hand-patching of
    epic_docs. Before the fix, cmd_init wrote the target-relative
    'docs/epics', which is empty on a clean sidecar target — the contract
    ratchet was silently vacuous."""
    import artifacts

    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))
    repo = tmp_path / "target"
    repo.mkdir()
    artifacts.elect(repo, "sidecar", backing="local")
    resolver = artifacts.Resolver.resolve(repo)
    epic = resolver.epic_dir("order-app")
    epic.mkdir(parents=True)
    (epic / "contracts.md").write_text(
        "### CTR-app-001 — valid range\nREQUIRES: start <= end\n")

    assert ratchet.cmd_init(argparse.Namespace(repo=str(repo), force=False)) == 0
    cfg_doc = json.loads((resolver.quality_dir() / "ratchet.json").read_text())
    # cmd_init resolved the ABSOLUTE sidecar epics dir itself.
    assert cfg_doc["epic_docs"] == str(resolver.epics_dir())
    assert Path(cfg_doc["epic_docs"]).is_absolute()

    assert ratchet.cmd_score(argparse.Namespace(
        repo=str(repo), no_tests=True, no_quality=True, venv=None, gobin=None)) == 0
    sc = json.loads((resolver.quality_dir() / ratchet.SCORECARD_NAME).read_text())
    assert sc["contract_hashes"], "sidecar contracts were not hashed (vacuous ratchet)"
    assert "CTR-app-001" in sc["contract_hashes"]
    assert not (repo / "docs").exists()  # still zero CW files in the target


def test_embedded_init_keeps_relative_epic_docs(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    assert ratchet.cmd_init(argparse.Namespace(repo=str(repo), force=False)) == 0
    cfg_doc = json.loads((repo / "docs" / "quality" / "ratchet.json").read_text())
    assert cfg_doc["epic_docs"] == "docs/epics"


def test_scope_json_is_default_protected(tmp_path):
    """F13: docs/scope.json is a goalpost — a worker widening its own scope
    must be parked by `protected`."""
    assert "docs/scope.json" in ratchet.DEFAULT_PROTECTED
    cfg = make_repo(tmp_path)
    assert ratchet.protected_hits(cfg, ["docs/scope.json"]) == ["docs/scope.json"]


def test_score_stamps_target_sha(tmp_path):
    """F12: every scorecard names the target HEAD it was computed against
    (None outside a git repo — recorded, not omitted)."""
    make_repo(tmp_path)
    assert ratchet.cmd_score(argparse.Namespace(
        repo=str(tmp_path), no_tests=True, no_quality=True, venv=None, gobin=None)) == 0
    sc = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())
    assert "target_sha" in sc
    assert sc["target_sha"] is None  # tmp_path is not a git repo — recorded as None


# ---- #284: score --reuse-report (skip a second full suite run) -----------------


def _score_args(tmp_path, **overrides):
    base = dict(
        repo=str(tmp_path), no_tests=False, no_quality=True, venv=None, gobin=None,
        reuse_report=None, reuse_report_max_age=1800,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _write_junit(path, cases):
    """Minimal junit-xml with the given (classname, name) PASSING cases."""
    body = "".join(f'<testcase classname="{c}" name="{n}"/>' for c, n in cases)
    Path(path).write_text(f"<testsuite>{body}</testsuite>")


def test_score_reuse_report_skips_running_cmd(tmp_path):
    """AC1/proof: a suite whose `cmd` would DESTROY the report if it ran is
    configured; --reuse-report must leave the pre-written report untouched
    and parse IT, never invoking cmd at all."""
    report = tmp_path / ".ratchet-junit.xml"
    cfg = make_repo(tmp_path, suites=[
        {"name": "pytest", "cmd": f"rm -f {report}", "cwd": ".", "parser": "junit-xml",
         "report": ".ratchet-junit.xml"},
    ])
    _write_junit(report, [("a.b", "t1"), ("a.b", "t2")])
    rc = ratchet.cmd_score(_score_args(tmp_path, reuse_report=[f"pytest={report}"]))
    assert rc == 0
    sc = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())
    assert set(sc["pass_set"]) == {"pytest::a.b::t1", "pytest::a.b::t2"}
    assert report.is_file()  # cmd (which would have deleted it) never ran


def _idempotent_junit_suite(tmp_path, name="pytest", report_name=".ratchet-junit.xml"):
    """A suite whose `cmd`, if actually run, SUCCEEDS and writes a valid
    report — so a test asserting --reuse-report validation fails for the
    RIGHT reason pre-implementation (rc==0, cmd silently ran) rather than
    coincidentally via run_suite's own missing-report check."""
    script = tmp_path / f"write_{name}.py"
    script.write_text(
        f"open('{report_name}', 'w').write("
        "'<testsuite><testcase classname=\"a.b\" name=\"t1\"/></testsuite>')\n"
    )
    return {"name": name, "cmd": f"python3 {script}", "cwd": ".",
            "parser": "junit-xml", "report": report_name}


def test_score_reuse_report_missing_file_errors(tmp_path):
    cfg = make_repo(tmp_path, suites=[_idempotent_junit_suite(tmp_path)])
    with pytest.raises(ratchet.RatchetError):
        ratchet.cmd_score(_score_args(
            tmp_path, reuse_report=[f"pytest={tmp_path / 'nope.xml'}"]))


def test_score_reuse_report_stale_mtime_errors(tmp_path):
    """A report older than --reuse-report-max-age must fail loudly, never
    silently score against leftover state from an earlier ticket (the exact
    failure mode the trx pre-run clearing already guards against)."""
    import os
    import time

    report = tmp_path / ".ratchet-junit.xml"
    cfg = make_repo(tmp_path, suites=[
        {"name": "pytest", "cmd": "true", "cwd": ".", "parser": "junit-xml",
         "report": ".ratchet-junit.xml"},
    ])
    _write_junit(report, [("a.b", "t1")])
    old = time.time() - 10_000
    os.utime(report, (old, old))
    with pytest.raises(ratchet.RatchetError, match="stale|old"):
        ratchet.cmd_score(_score_args(
            tmp_path, reuse_report=[f"pytest={report}"], reuse_report_max_age=60))


def test_score_reuse_report_unknown_suite_name_errors(tmp_path):
    cfg = make_repo(tmp_path, suites=[_idempotent_junit_suite(tmp_path)])
    with pytest.raises(ratchet.RatchetError):
        ratchet.cmd_score(_score_args(
            tmp_path, reuse_report=[f"nosuchsuite={tmp_path / '.ratchet-junit.xml'}"]))


def test_score_reuse_report_matches_a_fresh_run(tmp_path):
    """AC3: reuse and a fresh run must produce the SAME pass-set."""
    write_script = tmp_path / "write_report.py"
    write_script.write_text(
        "open('.ratchet-junit.xml', 'w').write("
        "'<testsuite><testcase classname=\"a.b\" name=\"t1\"/>"
        "<testcase classname=\"a.b\" name=\"t2\"/></testsuite>')\n"
    )
    cfg = make_repo(tmp_path, suites=[
        {"name": "pytest", "cmd": f"python3 {write_script}", "cwd": ".",
         "parser": "junit-xml", "report": ".ratchet-junit.xml"},
    ])
    # Fresh run: cmd actually executes and writes the report.
    assert ratchet.cmd_score(_score_args(tmp_path)) == 0
    sc_fresh = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())

    # Reuse run: same report file, cmd is NOT re-executed (cmd here is
    # idempotent, so this only proves parity of the parsed result, not
    # non-execution — that's covered by test_score_reuse_report_skips_running_cmd).
    report = tmp_path / ".ratchet-junit.xml"
    assert ratchet.cmd_score(_score_args(
        tmp_path, reuse_report=[f"pytest={report}"])) == 0
    sc_reused = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())

    assert sc_fresh["pass_set"] == sc_reused["pass_set"]


def test_score_reuse_report_absent_namespace_attrs_degrade_gracefully(tmp_path):
    """A hand-built argparse.Namespace predating #284 (no reuse_report attrs)
    must not crash — house precedent (see _resolve_retire_cases)."""
    make_repo(tmp_path, suites=[])
    ns = argparse.Namespace(repo=str(tmp_path), no_tests=True, no_quality=True,
                             venv=None, gobin=None)
    assert ratchet.cmd_score(ns) == 0


# ---- #322: --reuse-report extended to go-test-json / pass-fail-lines --------


def test_score_reuse_report_go_test_json_skips_running_cmd(tmp_path):
    """AC (#322): a go-test-json suite whose `cmd` would DESTROY the report if
    it ran is configured; --reuse-report must parse the pre-written report
    and never invoke cmd."""
    report = tmp_path / "go-test.jsonl"
    report.write_text(
        '{"Action":"run","Package":"pkg/a","Test":"TestFoo"}\n'
        '{"Action":"pass","Package":"pkg/a","Test":"TestFoo"}\n'
        '{"Action":"run","Package":"pkg/a","Test":"TestBar"}\n'
        '{"Action":"pass","Package":"pkg/a","Test":"TestBar"}\n'
    )
    make_repo(tmp_path, suites=[
        {"name": "go", "cmd": f"rm -f {report}", "cwd": ".", "parser": "go-test-json"},
    ])
    rc = ratchet.cmd_score(_score_args(tmp_path, reuse_report=[f"go={report}"]))
    assert rc == 0
    sc = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())
    assert set(sc["pass_set"]) == {"go::pkg/a::TestFoo", "go::pkg/a::TestBar"}
    assert report.is_file()  # cmd (which would have deleted it) never ran


def test_score_reuse_report_go_test_json_excludes_failed_cases(tmp_path):
    report = tmp_path / "go-test.jsonl"
    report.write_text(
        '{"Action":"pass","Package":"pkg/a","Test":"TestFoo"}\n'
        '{"Action":"fail","Package":"pkg/a","Test":"TestBar"}\n'
    )
    make_repo(tmp_path, suites=[
        {"name": "go", "cmd": "true", "cwd": ".", "parser": "go-test-json"},
    ])
    assert ratchet.cmd_score(_score_args(tmp_path, reuse_report=[f"go={report}"])) == 0
    sc = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())
    assert set(sc["pass_set"]) == {"go::pkg/a::TestFoo"}


def test_score_reuse_report_go_test_json_matches_a_fresh_run(tmp_path):
    """AC3 (#322): reuse and a fresh run must produce the SAME pass-set."""
    write_script = tmp_path / "write_go.py"
    write_script.write_text(
        "import sys\n"
        "sys.stdout.write("
        '\'{"Action":"pass","Package":"pkg/a","Test":"TestFoo"}\\n\'\n'
        ")\n"
    )
    report = tmp_path / "go-test.jsonl"
    cfg = make_repo(tmp_path, suites=[
        {"name": "go", "cmd": f"python3 {write_script}", "cwd": ".", "parser": "go-test-json"},
    ])
    assert ratchet.cmd_score(_score_args(tmp_path)) == 0
    sc_fresh = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())

    # Build the report a wrapper would have captured from that same stdout.
    proc_out = ratchet.subprocess.run(
        cfg.suites[0].cmd, shell=True, cwd=cfg.repo, capture_output=True, text=True
    ).stdout
    report.write_text(proc_out)
    assert ratchet.cmd_score(_score_args(tmp_path, reuse_report=[f"go={report}"])) == 0
    sc_reused = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())

    assert sc_fresh["pass_set"] == sc_reused["pass_set"]


def test_score_reuse_report_pass_fail_lines_skips_running_cmd(tmp_path):
    report = tmp_path / "pf.txt"
    report.write_text("PASS: test_one\nFAIL: test_two\nPASS: test_three\n")
    make_repo(tmp_path, suites=[
        {"name": "generic", "cmd": f"rm -f {report}", "cwd": ".", "parser": "pass-fail-lines"},
    ])
    rc = ratchet.cmd_score(_score_args(tmp_path, reuse_report=[f"generic={report}"]))
    assert rc == 0
    sc = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())
    assert set(sc["pass_set"]) == {"generic::test_one", "generic::test_three"}
    assert report.is_file()


def test_score_reuse_report_pass_fail_lines_matches_a_fresh_run(tmp_path):
    """AC3 (#322): reuse and a fresh run must produce the SAME pass-set."""
    write_script = tmp_path / "write_pf.py"
    write_script.write_text("print('PASS: test_one')\nprint('PASS: test_two')\n")
    report = tmp_path / "pf.txt"
    cfg = make_repo(tmp_path, suites=[
        {"name": "generic", "cmd": f"python3 {write_script}", "cwd": ".", "parser": "pass-fail-lines"},
    ])
    assert ratchet.cmd_score(_score_args(tmp_path)) == 0
    sc_fresh = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())

    proc_out = ratchet.subprocess.run(
        cfg.suites[0].cmd, shell=True, cwd=cfg.repo, capture_output=True, text=True
    ).stdout
    report.write_text(proc_out)
    assert ratchet.cmd_score(_score_args(tmp_path, reuse_report=[f"generic={report}"])) == 0
    sc_reused = json.loads((tmp_path / "docs" / "quality" / ratchet.SCORECARD_NAME).read_text())

    assert sc_fresh["pass_set"] == sc_reused["pass_set"]


def test_score_reuse_report_go_test_json_missing_file_errors(tmp_path):
    make_repo(tmp_path, suites=[
        {"name": "go", "cmd": "true", "cwd": ".", "parser": "go-test-json"},
    ])
    with pytest.raises(ratchet.RatchetError):
        ratchet.cmd_score(_score_args(
            tmp_path, reuse_report=[f"go={tmp_path / 'nope.jsonl'}"]))


def test_score_reuse_report_pass_fail_lines_stale_mtime_errors(tmp_path):
    """A stale go-test-json/pass-fail-lines report must fail loudly, exactly
    like junit-xml/trx already do — never silently score against leftover
    state from an earlier ticket."""
    import os
    import time

    report = tmp_path / "pf.txt"
    report.write_text("PASS: test_one\n")
    make_repo(tmp_path, suites=[
        {"name": "generic", "cmd": "true", "cwd": ".", "parser": "pass-fail-lines"},
    ])
    old = time.time() - 10_000
    os.utime(report, (old, old))
    with pytest.raises(ratchet.RatchetError, match="stale|old"):
        ratchet.cmd_score(_score_args(
            tmp_path, reuse_report=[f"generic={report}"], reuse_report_max_age=60))


def test_score_reuse_report_still_rejects_a_genuinely_unknown_parser(tmp_path):
    """Every KNOWN parser is now supported; an unrecognized one must still
    fail loudly rather than silently produce an empty pass-set."""
    report = tmp_path / "whatever.txt"
    report.write_text("anything")
    cfg = make_repo(tmp_path, suites=[])
    suite = ratchet.Suite(name="mystery", cmd="true", cwd=".", parser="mystery-parser")
    with pytest.raises(ratchet.RatchetError, match="mystery-parser"):
        ratchet.reuse_suite_report(cfg, suite, report, 1800)


# ---- sanctioned pathset (chief-wiggum#213) -----------------------------------------


def test_pathset_outside_with_explicit_paths_spec():
    """Ticket-pathset shape: a changed file is sanctioned iff it matches one of
    the globs (the same _glob_to_re grammar as protected_paths)."""
    spec = {"paths": ["internal/billing/**", "docs/epics/billing/*.md"], "source": "ticket #42"}
    changed = [
        "internal/billing/reconcile.go",       # sanctioned
        "internal/admin/handlers.go",          # OUTSIDE
        "docs/epics/billing/contracts.md",     # sanctioned
        "ui/app.tsx",                          # OUTSIDE
    ]
    assert ratchet.pathset_outside(spec, changed) == [
        "internal/admin/handlers.go",
        "ui/app.tsx",
    ]


def test_pathset_outside_with_scope_spec():
    """Domain scope.json shape: artifacts.py semantics — missing include =
    everything, exclude wins."""
    spec = {"include": ["internal/**"], "exclude": ["internal/legacy/**"]}
    changed = [
        "internal/billing/reconcile.go",   # in scope
        "internal/legacy/old.go",          # excluded -> OUTSIDE
        "ui/app.tsx",                      # not included -> OUTSIDE
    ]
    assert ratchet.pathset_outside(spec, changed) == [
        "internal/legacy/old.go",
        "ui/app.tsx",
    ]


def test_pathset_outside_scope_exclude_only():
    spec = {"exclude": ["vendor/**"]}
    assert ratchet.pathset_outside(spec, ["a.go", "vendor/x.go"]) == ["vendor/x.go"]


def test_load_pathset_rejects_bad_shapes(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(ratchet.RatchetError):
        ratchet.load_pathset(missing)
    both = tmp_path / "both.json"
    both.write_text(json.dumps({"paths": ["a"], "include": ["b"]}))
    with pytest.raises(ratchet.RatchetError):
        ratchet.load_pathset(both)
    neither = tmp_path / "neither.json"
    neither.write_text(json.dumps({"source": "x"}))
    with pytest.raises(ratchet.RatchetError):
        ratchet.load_pathset(neither)
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json")
    with pytest.raises(ratchet.RatchetError):
        ratchet.load_pathset(garbage)


def _pathset_repo(tmp_path):
    """A git repo whose HEAD adds one in-pathset and one out-of-pathset file
    relative to the base commit."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "T")
    (repo / "README.md").write_text("base\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()
    (repo / "internal" / "billing").mkdir(parents=True)
    (repo / "internal" / "billing" / "reconcile.go").write_text("func R() {}\n")
    (repo / "ui").mkdir()
    (repo / "ui" / "app.tsx").write_text("export {}\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "change")
    return repo, base


def test_cmd_pathset_parks_out_of_pathset_diff(tmp_path, capsys):
    """Park-for-human semantics, same shape as `protected`: exit 1 + a labeled
    stderr listing when the branch diff escapes the sanctioned set."""
    repo, base = _pathset_repo(tmp_path)
    spec_file = tmp_path / "pathset.json"
    spec_file.write_text(json.dumps({"paths": ["internal/**"], "source": "ticket #216-demo"}))
    args = argparse.Namespace(repo=str(repo), base=base,
                              pathset_file=str(spec_file), report_only=False)
    rc = ratchet.cmd_pathset(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "OUTSIDE THE SANCTIONED PATHSET" in err
    assert "ui/app.tsx" in err
    assert "internal/billing/reconcile.go" not in err
    assert "ticket #216-demo" in err  # the spec's source label is surfaced


def test_cmd_pathset_report_only_prints_but_exits_zero(tmp_path, capsys):
    """--report-only is how #216 consumes this first (docs/gate-rollout.md)."""
    repo, base = _pathset_repo(tmp_path)
    spec_file = tmp_path / "pathset.json"
    spec_file.write_text(json.dumps({"paths": ["internal/**"]}))
    args = argparse.Namespace(repo=str(repo), base=base,
                              pathset_file=str(spec_file), report_only=True)
    rc = ratchet.cmd_pathset(args)
    assert rc == 0
    err = capsys.readouterr().err
    assert "ui/app.tsx" in err and "report-only" in err


def test_cmd_pathset_scope_source_clean_when_diff_in_scope(tmp_path, capsys):
    """Scope-source pathset: a diff entirely inside the domain scope passes."""
    repo, base = _pathset_repo(tmp_path)
    spec_file = tmp_path / "scope.json"
    spec_file.write_text(json.dumps({"include": ["internal/**", "ui/**"]}))
    args = argparse.Namespace(repo=str(repo), base=base,
                              pathset_file=str(spec_file), report_only=False)
    rc = ratchet.cmd_pathset(args)
    assert rc == 0
    assert "within the sanctioned pathset" in capsys.readouterr().out


def test_cmd_pathset_scope_source_parks_out_of_scope(tmp_path, capsys):
    repo, base = _pathset_repo(tmp_path)
    spec_file = tmp_path / "scope.json"
    spec_file.write_text(json.dumps({"include": ["internal/**"]}))
    args = argparse.Namespace(repo=str(repo), base=base,
                              pathset_file=str(spec_file), report_only=False)
    rc = ratchet.cmd_pathset(args)
    assert rc == 1
    assert "ui/app.tsx" in capsys.readouterr().err


def test_load_pathset_typo_key_is_a_legible_error(tmp_path):
    """F6: {"includes": [...]} (typo) must error NAMING the key — never
    silently sanction everything (or nothing)."""
    p = tmp_path / "pathset.json"
    p.write_text(json.dumps({"includes": ["internal/**"]}))
    with pytest.raises(ratchet.RatchetError, match="includes"):
        ratchet.load_pathset(p)


def test_load_pathset_unknown_key_beside_valid_shape_is_an_error(tmp_path):
    p = tmp_path / "pathset.json"
    p.write_text(json.dumps({"exclude": ["vendor/**"], "includes": ["internal/**"]}))
    with pytest.raises(ratchet.RatchetError, match="includes"):
        ratchet.load_pathset(p)


def test_cmd_pathset_typo_scope_exits_2(tmp_path, capsys):
    repo, base = _pathset_repo(tmp_path)
    spec_file = tmp_path / "scope.json"
    spec_file.write_text(json.dumps({"includes": ["internal/**"]}))
    args = argparse.Namespace(repo=str(repo), base=base,
                              pathset_file=str(spec_file), report_only=False)
    dispatch_rc = None
    try:
        dispatch_rc = ratchet.cmd_pathset(args)
    except ratchet.RatchetError as e:
        assert "includes" in str(e)
    else:
        raise AssertionError(f"expected RatchetError, got rc={dispatch_rc}")


def test_cmd_pathset_needs_no_ratchet_config(tmp_path, capsys):
    """Deliberately config-free: works on a target with no ratchet init (the
    #216 consumption path) — unlike `protected`, which loads the config."""
    repo, base = _pathset_repo(tmp_path)
    assert not (repo / "docs" / "quality" / "ratchet.json").exists()
    spec_file = tmp_path / "pathset.json"
    spec_file.write_text(json.dumps({"paths": ["**"]}))
    args = argparse.Namespace(repo=str(repo), base=base,
                              pathset_file=str(spec_file), report_only=False)
    assert ratchet.cmd_pathset(args) == 0


@pytest.mark.skipif(shutil.which("lizard") is None,
                    reason="lizard required for the quality snapshot")
def test_score_quality_scope_filters_population(tmp_path, monkeypatch):
    """#213 domain scope: quality baselines are computed over the IN-SCOPE
    population only; removing the scope restores the whole-repo snapshot
    exactly (no scope => identical to before)."""
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))
    repo = tmp_path / "scoped"
    (repo / "app").mkdir(parents=True)
    (repo / "vendored").mkdir()
    (repo / "app" / "main.py").write_text(
        "def f(x):\n    if x:\n        return 1\n    return 0\n"
    )
    (repo / "vendored" / "lib.py").write_text(
        "def g(x):\n    if x:\n        return 1\n    return 0\n\n\n"
        "def h(x):\n    if x:\n        return 2\n    return 0\n"
    )
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@e.co",
         "commit", "-q", "-m", "init"],
        check=True, capture_output=True,
    )

    cfg = make_repo(tmp_path)
    cfg.repo = repo
    q_full = ratchet.score_quality(cfg)
    assert "skipped" not in q_full, q_full

    # scope.json at the meta root (embedded => <repo>/docs) excludes vendored/.
    (repo / "docs").mkdir()
    (repo / "docs" / "scope.json").write_text(json.dumps({"exclude": ["vendored/*"]}))
    q_scoped = ratchet.score_quality(cfg)
    assert "skipped" not in q_scoped, q_scoped
    assert q_scoped["functions"] < q_full["functions"]
    assert q_scoped["total_loc"] < q_full["total_loc"]
    assert q_scoped["churned_loc"] < q_full["churned_loc"]

    # No scope file => byte-identical to the pre-scope snapshot.
    (repo / "docs" / "scope.json").unlink()
    assert ratchet.score_quality(cfg) == q_full

    # A malformed scope must never silently widen to whole-repo scope.
    (repo / "docs" / "scope.json").write_text(json.dumps({"includes": ["app/*"]}))
    q_bad = ratchet.score_quality(cfg)
    assert "skipped" in q_bad and "includes" in q_bad["skipped"]


# ---- pass-set retirement (quarantine, #278) ----------------------------------


def _prep_quarantine_repo(tmp_path, flaky_still_passing=False):
    """A repo with 's::flaky' already in the high-water mark (merged), and a
    CURRENT scorecard either containing it (still passing, for the V8
    anti-abuse test) or not (excluded from the suite — the flaky-quarantine
    scenario every other CLI test needs)."""
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::flaky"}), merged=True)
    cur = {"s::t1", "s::flaky"} if flaky_still_passing else {"s::t1"}
    _write_scorecard(cfg, scorecard_from(cfg, cur))
    return cfg


def test_flaky_quarantine_round_trip(tmp_path):
    """chief-wiggum#278 AC round trip, five explicitly-labelled phases:
    (1) a flaky case enters the high-water mark, (2) it is excluded from the
    suite and shows as a permanent-red missing_tests violation, (3) a human
    journals a --retire-case quarantine for it, (4) the ratchet reads clean
    while the quarantine is live, (5) once the expiry passes the case
    re-enters missing_tests and blocks again."""
    cfg = make_repo(tmp_path)

    # 1. in high-water
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::flaky"}), merged=True)
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert hw["pass_set"] == ["s::flaky", "s::t1"]

    # 2. excluded from the suite — the permanent-red bug
    sc = scorecard_from(cfg, {"s::t1"})
    assert ratchet.violations(sc, hw)["missing_tests"] == ["s::flaky"]

    # 3. retired
    append_record(cfg, sc, merged=False, retired_cases=[{
        "id": "s::flaky", "reason": "order-dependent shared state", "owner": "plwp",
        "expiry": "2026-11-01", "created_at": "2026-08-03T00:00:00Z",
    }])
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert hw["pass_set"] == ["s::t1"]
    assert "s::flaky" in hw["quarantined"]

    # 4. ratchet clean
    v = ratchet.violations(sc, hw, today=date(2026, 8, 3))
    assert v["missing_tests"] == []
    assert len(v["quarantined"]) == 1
    assert v["quarantined"][0]["reason"] == "order-dependent shared state"
    assert v["expired_quarantines"] == []

    # 5. expiry passes -> surfaced again
    v = ratchet.violations(sc, hw, today=date(2026, 11, 2))
    assert v["missing_tests"] == ["s::flaky"]
    assert v["expired_quarantines"][0]["id"] == "s::flaky"
    assert v["quarantined"] == []


# --- fold / derivation ------------------------------------------------------


def test_retired_case_is_restored_when_it_passes_again(tmp_path):
    """D7: a case that returns to a later MERGED record's pass_set is
    un-quarantined by the fold — stale quarantine metadata is dropped too,
    with no second human act."""
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::flaky"}), merged=True)
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=False, retired_cases=[{
        "id": "s::flaky", "reason": "flaky", "owner": "plwp",
        "expiry": "2099-01-01", "created_at": "2026-08-03T00:00:00Z",
    }])
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert "s::flaky" in hw["quarantined"]
    assert "s::flaky" not in hw["pass_set"]
    # the flaky case passes again in a later MERGED record
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::flaky"}), merged=True)
    hw2 = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert "s::flaky" in hw2["pass_set"]
    assert "s::flaky" not in hw2["quarantined"]


def test_retire_wins_over_a_repass_within_one_record(tmp_path):
    """A single record that is merged=True with the case in its pass_set AND
    carries a retired_cases entry for it lands on quarantined — the explicit
    human act wins inside its own record."""
    cfg = make_repo(tmp_path)
    sc = scorecard_from(cfg, {"s::t1", "s::flaky"})
    append_record(cfg, sc, merged=True, retired_cases=[{
        "id": "s::flaky", "reason": "flaky", "owner": "plwp",
        "expiry": "2099-01-01", "created_at": "2026-08-03T00:00:00Z",
    }])
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert "s::flaky" not in hw["pass_set"]
    assert "s::flaky" in hw["quarantined"]


def test_last_retirement_wins_renewal(tmp_path):
    cfg = make_repo(tmp_path)
    sc = scorecard_from(cfg, {"s::t1"})
    append_record(cfg, sc, merged=False, retired_cases=[{
        "id": "s::flaky", "reason": "flaky", "owner": "plwp",
        "expiry": "2026-11-01", "created_at": "2026-08-03T00:00:00Z",
    }])
    append_record(cfg, sc, merged=False, retired_cases=[{
        "id": "s::flaky", "reason": "flaky, renewed", "owner": "plwp",
        "expiry": "2027-02-01", "created_at": "2026-11-02T00:00:00Z",
    }])
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert hw["quarantined"]["s::flaky"]["expiry"] == "2027-02-01"


def test_backward_compat_journal_without_retired_cases(tmp_path):
    """A pre-#278 journal record — hand-built with NO retired_cases key at
    all (the exact shape append_record wrote before this ticket) — derives an
    empty quarantine map and raises nothing."""
    cfg = make_repo(tmp_path)
    body = {
        "record_id": "rec-00001", "event": "ticket", "ref": "#1",
        "gate_result": "pass", "merged": True,
        "scorecard": scorecard_from(cfg, {"s::t1"}),
        "amended": {}, "retired": [], "ratchet_status": "held", "notes": "",
    }
    body["record_hash"] = ratchet.stable_hash("genesis", json.dumps(body, sort_keys=True))
    cfg.journal.parent.mkdir(parents=True, exist_ok=True)
    with cfg.journal.open("a") as f:
        f.write(json.dumps(body, sort_keys=True) + "\n")
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert hw["quarantined"] == {}


def test_violations_tolerates_highwater_without_quarantined_key():
    """Old ratchet-highwater.json caches / hand-built high-water dicts lack
    the 'quarantined' key entirely — violations() must read it with .get, not
    KeyError (the tests/test_verifier_hashes.py:361 shape)."""
    hw = {"pass_set": ["s::t1"], "contract_hashes": {}}
    sc = {"pass_set": ["s::t1"], "contract_hashes": {}}
    v = ratchet.violations(sc, hw)
    assert v["quarantined"] == []
    assert v["expired_quarantines"] == []


# --- expiry posture (inherited fail-closed) ----------------------------------


def test_missing_expiry_counts_as_expired(tmp_path):
    cfg = make_repo(tmp_path)
    sc = scorecard_from(cfg, {"s::t1"})
    append_record(cfg, sc, merged=False, retired_cases=[{
        "id": "s::flaky", "reason": "flaky", "owner": "plwp",
        "created_at": "2026-08-03T00:00:00Z",
    }])  # no "expiry" key at all
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    v = ratchet.violations(sc, hw, today=date(2026, 8, 3))
    assert "s::flaky" in v["missing_tests"]
    assert v["expired_quarantines"][0]["id"] == "s::flaky"


def test_unparseable_expiry_counts_as_expired(tmp_path):
    cfg = make_repo(tmp_path)
    sc = scorecard_from(cfg, {"s::t1"})
    append_record(cfg, sc, merged=False, retired_cases=[{
        "id": "s::flaky", "reason": "flaky", "owner": "plwp",
        "expiry": "next tuesday", "created_at": "2026-08-03T00:00:00Z",
    }])
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    v = ratchet.violations(sc, hw, today=date(2026, 8, 3))
    assert "s::flaky" in v["missing_tests"]
    assert v["expired_quarantines"][0]["id"] == "s::flaky"


def test_expiry_day_itself_still_waives(tmp_path):
    """`grandfather.is_expired` is strictly `<` — the expiry day itself still
    waives."""
    cfg = make_repo(tmp_path)
    sc = scorecard_from(cfg, {"s::t1"})
    append_record(cfg, sc, merged=False, retired_cases=[{
        "id": "s::flaky", "reason": "flaky", "owner": "plwp",
        "expiry": "2026-11-01", "created_at": "2026-08-03T00:00:00Z",
    }])
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    v = ratchet.violations(sc, hw, today=date(2026, 11, 1))
    assert "s::flaky" not in v["missing_tests"]
    assert v["expired_quarantines"] == []


# --- cmd_record validation (real CLI, exit 2) --------------------------------


def test_retire_case_requires_a_reason(tmp_path):
    _prep_quarantine_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--ref", "#278", "--retire-case", "s::flaky"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "reason" in proc.stderr.lower()


def test_retire_case_rejects_a_past_expiry(tmp_path):
    _prep_quarantine_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--ref", "#278", "--retire-case", "s::flaky",
         "--retire-case-reason", "flaky", "--retire-case-expiry", "2020-01-01"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "past" in proc.stderr.lower()


def test_retire_case_rejects_an_unparseable_expiry(tmp_path):
    _prep_quarantine_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--ref", "#278", "--retire-case", "s::flaky",
         "--retire-case-reason", "flaky", "--retire-case-expiry", "next tuesday"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "iso" in proc.stderr.lower()


def test_retire_case_refuses_a_case_not_in_highwater(tmp_path):
    """Same doctrine as --retire-verifier: a typo'd case must be SURFACED,
    not a silent no-op — the error names the '<suite>::<case id>' form."""
    _prep_quarantine_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--ref", "#278", "--retire-case", "s::not_a_real_case",
         "--retire-case-reason", "flaky"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "s::not_a_real_case" in proc.stderr
    assert "case id" in proc.stderr.lower() or "no case" in proc.stderr.lower()


def test_retire_case_refuses_a_currently_passing_case(tmp_path):
    """The anti-abuse test: an agent must not be able to pre-emptively
    retire the cases it is about to break."""
    _prep_quarantine_repo(tmp_path, flaky_still_passing=True)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--ref", "#278", "--retire-case", "s::flaky",
         "--retire-case-reason", "flaky"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "s::flaky" in proc.stderr
    assert "passing" in proc.stderr.lower()


def test_retire_case_companion_flags_without_a_case_are_an_error(tmp_path):
    _prep_quarantine_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--ref", "#278", "--retire-case-reason", "flaky"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "--retire-case" in proc.stderr


def test_retire_case_file_that_resolves_nothing_is_an_error(tmp_path):
    _prep_quarantine_repo(tmp_path)
    case_file = tmp_path / "cases.txt"
    case_file.write_text("# just a comment\n\n")
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--ref", "#278", "--retire-case-file", str(case_file),
         "--retire-case-reason", "flaky"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2


# --- glob semantics -----------------------------------------------------------


def test_retire_case_glob_expands_and_materializes(tmp_path):
    cfg = _prep_quarantine_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--ref", "#278", "--retire-case", "s::flaky*",
         "--retire-case-reason", "flaky class"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    recs = ratchet.load_journal(cfg)
    last = recs[-1]
    assert last["retired_cases"][0]["id"] == "s::flaky"
    assert not any("*" in e["id"] for e in last["retired_cases"])
    # a later merged record introducing a NEW case that also matches the glob
    # must NOT be quarantined — globs are materialized, never stored.
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::flaky_new"}), merged=True)
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert "s::flaky_new" in hw["pass_set"]
    assert "s::flaky_new" not in hw["quarantined"]


def test_retire_case_glob_matching_nothing_is_an_error(tmp_path):
    _prep_quarantine_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--ref", "#278", "--retire-case", "s::nope-*",
         "--retire-case-reason", "flaky"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "no case" in proc.stderr.lower()


def test_retire_case_glob_matches_go_ids_with_slashes(tmp_path):
    """The fnmatch vs _glob_to_re trap: this fails if someone 'simplifies' to
    _glob_to_re, whose '*' compiles to '[^/]*' and cannot match a go case ID
    carrying '/' in its package path."""
    cfg = make_repo(tmp_path)
    append_record(
        cfg, scorecard_from(cfg, {"go::github.com/acme/app/internal::TestX"}), merged=True,
    )
    _write_scorecard(cfg, scorecard_from(cfg, set()))
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--ref", "#278",
         "--retire-case", "go::github.com/acme/app/*",
         "--retire-case-reason", "flaky go test"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    recs = ratchet.load_journal(cfg)
    assert recs[-1]["retired_cases"][0]["id"] == "go::github.com/acme/app/internal::TestX"


def test_retire_case_file_accepts_comments_and_blanks(tmp_path):
    cfg = _prep_quarantine_repo(tmp_path)
    case_file = tmp_path / "cases.txt"
    case_file.write_text("# flaky class\n\ns::flaky\n\n  \n")
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--ref", "#278", "--retire-case-file", str(case_file),
         "--retire-case-reason", "flaky"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    recs = ratchet.load_journal(cfg)
    assert recs[-1]["retired_cases"][0]["id"] == "s::flaky"


# --- verdict + chain -----------------------------------------------------------


def _record_args(tmp_path, **overrides):
    base = dict(
        repo=str(tmp_path), event="ticket", ref="#278", gate="pass", merged=False,
        notes="", amend_verifier=None, retire_verifier=None, amend=None, retire=None,
        retire_case=None, retire_case_file=None, retire_case_reason="",
        retire_case_owner="unassigned", retire_case_expiry=None,
        retire_case_expiry_days=ratchet.DEFAULT_QUARANTINE_DAYS,
        retire_case_permanent=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_record_status_is_quarantined_when_cases_retired(tmp_path):
    """Verdict precedence (D4): a record that ALSO advances the pass-set but
    carries retired_cases still reads 'quarantined', not 'advanced'."""
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::flaky"}), merged=True)
    _write_scorecard(cfg, scorecard_from(cfg, {"s::t1", "s::t2"}))  # advances + drops flaky
    args = _record_args(
        tmp_path, merged=False, retire_case=["s::flaky"],
        retire_case_reason="order-dependent shared state", retire_case_owner="plwp",
        retire_case_expiry="2099-01-01",
    )
    assert ratchet.cmd_record(args) == 0
    recs = ratchet.load_journal(cfg)
    assert recs[-1]["ratchet_status"] == "quarantined"


def test_record_status_is_violated_when_an_expired_quarantine_is_still_missing(tmp_path):
    """An expired quarantine plus --merged reads 'violated', proving
    cmd_record uses effective_pass_set and not the bare pass_set."""
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::flaky"}), merged=True)
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=False, retired_cases=[{
        "id": "s::flaky", "reason": "flaky", "owner": "plwp",
        "expiry": "2020-01-01", "created_at": "2019-01-01T00:00:00Z",  # already expired
    }])
    _write_scorecard(cfg, scorecard_from(cfg, {"s::t1"}))  # flaky still missing
    args = _record_args(tmp_path, merged=True)
    assert ratchet.cmd_record(args) == 0
    recs = ratchet.load_journal(cfg)
    assert recs[-1]["ratchet_status"] == "violated"


def test_record_with_retired_cases_still_hash_chains(tmp_path):
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::flaky"}), merged=True)
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=False, retired_cases=[{
        "id": "s::flaky", "reason": "flaky", "owner": "plwp",
        "expiry": "2099-01-01", "created_at": "2026-08-03T00:00:00Z",
    }])
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::t2"}), merged=True)
    recs = ratchet.load_journal(cfg)  # raises TamperError if the chain is broken
    assert len(recs) == 3


def test_recent_prints_the_quarantined_status(tmp_path, capsys):
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::flaky"}), merged=True)
    _write_scorecard(cfg, scorecard_from(cfg, {"s::t1"}))
    args = _record_args(
        tmp_path, merged=False, retire_case=["s::flaky"],
        retire_case_reason="order-dependent shared state", retire_case_owner="plwp",
        retire_case_expiry="2099-01-01",
    )
    assert ratchet.cmd_record(args) == 0
    capsys.readouterr()
    assert ratchet.cmd_recent(argparse.Namespace(repo=str(tmp_path), n=5)) == 0
    assert "[quarantined]" in capsys.readouterr().out


# --- `check` surfacing ---------------------------------------------------------


def test_check_reports_quarantines_report_only_and_exits_zero(tmp_path, capsys):
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::flaky"}), merged=True)
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=False, retired_cases=[{
        "id": "s::flaky", "reason": "order-dependent shared state", "owner": "plwp",
        "expiry": "2099-01-01", "created_at": "2026-08-03T00:00:00Z",
    }])
    _write_scorecard(cfg, scorecard_from(cfg, {"s::t1"}))
    args = argparse.Namespace(repo=str(tmp_path), format="text", gate_quality=False)
    assert ratchet.cmd_check(args) == 0
    out = capsys.readouterr()
    assert "1 case(s) quarantined" in out.out
    assert "[report-only]" in out.err


def test_check_ok_line_is_unchanged_with_no_quarantines(tmp_path, capsys):
    """Pins the existing success string byte-for-byte (guards downstream
    prose/log matching) — must not change when there is nothing quarantined."""
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=True)
    _write_scorecard(cfg, scorecard_from(cfg, {"s::t1"}))
    args = argparse.Namespace(repo=str(tmp_path), format="text", gate_quality=False)
    assert ratchet.cmd_check(args) == 0
    out = capsys.readouterr().out
    assert out.strip() == "ratchet: OK (pass-set and contract definitions hold the high-water mark)"


def test_check_blocks_and_labels_an_expired_quarantine(tmp_path, capsys):
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::flaky"}), merged=True)
    # built via append_record (bypassing V5), so no clock freezing is needed.
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=False, retired_cases=[{
        "id": "s::flaky", "reason": "flaky", "owner": "plwp",
        "expiry": "2020-01-01", "created_at": "2019-01-01T00:00:00Z",
    }])
    _write_scorecard(cfg, scorecard_from(cfg, {"s::t1"}))
    args = argparse.Namespace(repo=str(tmp_path), format="text", gate_quality=False)
    assert ratchet.cmd_check(args) == 1
    err = capsys.readouterr().err
    assert "EXPIRED" in err
    assert "s::flaky" in err

    args_json = argparse.Namespace(repo=str(tmp_path), format="json", gate_quality=False)
    assert ratchet.cmd_check(args_json) == 1
    data = json.loads(capsys.readouterr().out)
    assert "s::flaky" in data["missing_tests"]


def test_check_json_gains_quarantine_keys_additively(tmp_path, capsys):
    """Asserts the five original violations() keys are all still present with
    unchanged names, plus the two new ones."""
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=True)
    _write_scorecard(cfg, scorecard_from(cfg, {"s::t1"}))
    args = argparse.Namespace(repo=str(tmp_path), format="json", gate_quality=False)
    assert ratchet.cmd_check(args) == 0
    data = json.loads(capsys.readouterr().out)
    for key in ("missing_tests", "weakened_contracts", "removed_contracts",
                "weakened_verifier_tests", "removed_verifier_tests"):
        assert key in data
    assert "quarantined" in data
    assert "expired_quarantines" in data


def test_highwater_overlays_live_expiry(tmp_path, capsys):
    cfg = make_repo(tmp_path)
    append_record(
        cfg, scorecard_from(cfg, {"s::t1", "s::future", "s::past"}), merged=True,
    )
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=False, retired_cases=[
        {"id": "s::future", "reason": "flaky", "owner": "plwp",
         "expiry": "2099-01-01", "created_at": "2026-08-03T00:00:00Z"},
        {"id": "s::past", "reason": "flaky", "owner": "plwp",
         "expiry": "2020-01-01", "created_at": "2019-01-01T00:00:00Z"},
    ])
    assert ratchet.cmd_highwater(argparse.Namespace(repo=str(tmp_path))) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["quarantined"]["s::future"]["expired"] is False
    assert data["quarantined"]["s::past"]["expired"] is True


# ---- quarantine: paths only reachable through the real CLI (#278 review) ----
# The three bugs below all shipped with every test green because the original
# suite reached the fold by injecting journal records directly (append_record)
# instead of driving cmd_record. Each of these drives the REAL CLI.


def test_retire_case_renewal_through_the_cli(tmp_path):
    """A quarantined case must stay retirable so the documented renewal path
    (D7: 'renewal is a new record, not an edit') is reachable through the real
    CLI. Regression: hw_cases was built from pass_set alone, but a quarantined
    case has already LEFT pass_set, so V7 rejected every renewal with
    'matches no case in the current high-water mark'."""
    cfg = _prep_quarantine_repo(tmp_path)
    base = [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
            "--event", "ticket", "--retire-case", "s::flaky", "--retire-case-reason", "flaky"]
    first = subprocess.run(base + ["--retire-case-expiry", "2026-11-01"],
                           capture_output=True, text=True)
    assert first.returncode == 0, first.stderr

    renewal = subprocess.run(base + ["--retire-case-expiry", "2027-02-01"],
                             capture_output=True, text=True)
    assert renewal.returncode == 0, renewal.stderr
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    # last-wins: the renewal's expiry is the live one.
    assert hw["quarantined"]["s::flaky"]["expiry"] == "2027-02-01"
    assert "s::flaky" not in hw["pass_set"]


def test_record_merged_with_retired_cases_reads_quarantined(tmp_path):
    """A record that BOTH merges and retires reads 'quarantined', per D4.
    Regression: this record's own retirements were still required by
    prev_hw while V8 guarantees they are absent from the new scorecard, so
    the verdict could only ever be 'violated' and the 'quarantined' branch
    was unreachable in exactly the combination /implement produces."""
    _prep_quarantine_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--ref", "#278", "--merged",
         "--retire-case", "s::flaky", "--retire-case-reason", "order-dependent shared state"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "status=quarantined" in proc.stdout, proc.stdout


def test_retire_case_exact_id_with_fnmatch_metacharacters(tmp_path):
    """A pytest parameterized case ID carries fnmatch metacharacters and does
    NOT fnmatch itself, so an exact ID pasted verbatim must match exactly
    before glob interpretation — otherwise the most obvious possible
    invocation fails with 'matches no case'."""
    cid = "pytest::tests/test_x.py::test_y[param-1]"
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {cid, "s::t1"}), merged=True)
    _write_scorecard(cfg, scorecard_from(cfg, {"s::t1"}))
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--retire-case", cid, "--retire-case-reason", "flaky param"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert cid in hw["quarantined"]


# ==== permanent retirement (#290) ==============================================
#
# #278's --retire-case is a QUARANTINE: mandatory expiry, self-renewing
# pressure, designed for a flaky/order-dependent test that should come back.
# It is the wrong shape for a case that was renamed/re-parametrised/deleted
# and will NEVER pass again — the quarantine just expires and re-blocks,
# needing renewal forever. --retire-case-permanent is a DISTINCT, honestly
# named terminal path: still journaled/reasoned/owned, but no expiry, and
# never re-added by effective_pass_set.
#
# Reuses the ``_record_args`` helper defined above (now with a
# ``retire_case_permanent=False`` default) rather than a second definition.


def test_a_case_vanishing_without_a_journal_entry_still_fires(tmp_path):
    """#290 must not become a silent escape hatch.

    Permanent retirement removes a case from the pass-set for good, so the
    property that actually matters is the NEGATIVE one: a case that simply
    DISAPPEARS from the suite, with no journaled retirement behind it, must
    still fire missing_tests. Otherwise the ratchet could be dodged by
    deleting a test instead of recording that you retired it, and the
    tamper-evidence argument collapses.

    The positive path is covered by the tests below; this pins the negative.
    """
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::flaky"}), merged=True)
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))

    # the case vanishes from the suite. No `record --retire-case-permanent`,
    # no journal entry, no owner, no reason — just gone.
    sc = scorecard_from(cfg, {"s::t1"})

    assert ratchet.violations(sc, hw)["missing_tests"] == ["s::flaky"], (
        "a case that vanished with no journaled retirement must still be a "
        "missing_tests violation — the #290 retirement path is the ONLY way "
        "a case may legitimately leave the pass-set"
    )


def test_retire_case_permanent_requires_explicit_owner(tmp_path):
    """Attribution is not the thing being relaxed for a permanent retirement —
    the quarantine path's lax 'unassigned' default does not carry over."""
    _prep_quarantine_repo(tmp_path)
    args = _record_args(
        tmp_path, retire_case=["s::flaky"], retire_case_reason="renamed",
        retire_case_permanent=True,
    )
    with pytest.raises(ratchet.RatchetError, match="explicit --retire-case-owner"):
        ratchet.cmd_record(args)


def test_retire_case_permanent_rejects_an_expiry(tmp_path):
    _prep_quarantine_repo(tmp_path)
    args = _record_args(
        tmp_path, retire_case=["s::flaky"], retire_case_reason="renamed",
        retire_case_owner="plwp", retire_case_permanent=True,
        retire_case_expiry="2099-01-01",
    )
    with pytest.raises(ratchet.RatchetError, match="rejects an expiry"):
        ratchet.cmd_record(args)


def test_retire_case_permanent_rejects_expiry_days(tmp_path):
    _prep_quarantine_repo(tmp_path)
    args = _record_args(
        tmp_path, retire_case=["s::flaky"], retire_case_reason="renamed",
        retire_case_owner="plwp", retire_case_permanent=True,
        retire_case_expiry_days=30,
    )
    with pytest.raises(ratchet.RatchetError, match="rejects an expiry"):
        ratchet.cmd_record(args)


def test_retire_case_permanent_still_requires_a_reason(tmp_path):
    _prep_quarantine_repo(tmp_path)
    args = _record_args(
        tmp_path, retire_case=["s::flaky"], retire_case_owner="plwp",
        retire_case_permanent=True,
    )
    with pytest.raises(ratchet.RatchetError, match="reason"):
        ratchet.cmd_record(args)


def test_retire_case_permanent_companion_flags_without_a_case_are_an_error(tmp_path):
    _prep_quarantine_repo(tmp_path)
    args = _record_args(tmp_path, retire_case_permanent=True)
    with pytest.raises(ratchet.RatchetError, match="--retire-case"):
        ratchet.cmd_record(args)


def test_retire_case_permanent_round_trip(tmp_path):
    """AC1: a permanently retired case never re-enters missing_tests, no
    matter how far in the future — unlike quarantine, there is no expiry to
    even check."""
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::renamed_away"}), merged=True)
    sc = scorecard_from(cfg, {"s::t1"})
    _write_scorecard(cfg, sc)
    args = _record_args(
        tmp_path, retire_case=["s::renamed_away"],
        retire_case_reason="renamed to s::t1_v2, old id gone for good",
        retire_case_owner="plwp", retire_case_permanent=True,
    )
    assert ratchet.cmd_record(args) == 0
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert "s::renamed_away" not in hw["pass_set"]
    assert "s::renamed_away" in hw["removed_cases"]
    assert "s::renamed_away" not in hw["quarantined"]
    assert hw["removed_cases"]["s::renamed_away"]["expiry"] is None

    # Decades in the future — still never blocks (no expiry logic applies).
    v = ratchet.violations(sc, hw, today=date(2099, 1, 1))
    assert v["missing_tests"] == []
    assert v["expired_quarantines"] == []
    assert len(v["removed_cases"]) == 1
    assert v["removed_cases"][0]["id"] == "s::renamed_away"


def test_removed_case_is_separate_from_quarantined_in_the_fold(tmp_path):
    cfg = make_repo(tmp_path)
    append_record(
        cfg, scorecard_from(cfg, {"s::t1", "s::flaky", "s::gone"}), merged=True,
    )
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=False, retired_cases=[
        {"id": "s::flaky", "reason": "flaky", "owner": "plwp",
         "expiry": "2099-01-01", "created_at": "2026-08-03T00:00:00Z"},
        {"id": "s::gone", "reason": "deleted", "owner": "plwp", "expiry": None,
         "created_at": "2026-08-03T00:00:00Z", "kind": "removed"},
    ])
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert set(hw["quarantined"]) == {"s::flaky"}
    assert set(hw["removed_cases"]) == {"s::gone"}
    assert "s::flaky" not in hw["pass_set"]
    assert "s::gone" not in hw["pass_set"]


def test_removed_case_self_heals_when_it_passes_again(tmp_path):
    """Same self-healing doctrine as quarantine (#278 D7): if the exact case
    ID somehow reappears in a later MERGED pass_set, it is restored — no
    second human act needed."""
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::gone"}), merged=True)
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=False, retired_cases=[
        {"id": "s::gone", "reason": "deleted", "owner": "plwp", "expiry": None,
         "created_at": "2026-08-03T00:00:00Z", "kind": "removed"},
    ])
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert "s::gone" in hw["removed_cases"]
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::gone"}), merged=True)
    hw2 = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert "s::gone" in hw2["pass_set"]
    assert "s::gone" not in hw2["removed_cases"]


def test_a_quarantined_case_can_graduate_to_permanent(tmp_path):
    """An operator gives up renewing a flaky quarantine and permanently
    retires it instead — the case must stay retirable via the CLI (same
    reachability guard as quarantine renewal, #278 review) and the fold must
    land it in removed_cases only, not both."""
    cfg = _prep_quarantine_repo(tmp_path)
    quarantine = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--retire-case", "s::flaky", "--retire-case-reason", "flaky",
         "--retire-case-expiry", "2026-11-01"],
        capture_output=True, text=True,
    )
    assert quarantine.returncode == 0, quarantine.stderr
    permanent = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--retire-case", "s::flaky",
         "--retire-case-reason", "giving up, deleting the test",
         "--retire-case-owner", "plwp", "--retire-case-permanent"],
        capture_output=True, text=True,
    )
    assert permanent.returncode == 0, permanent.stderr
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert "s::flaky" in hw["removed_cases"]
    assert "s::flaky" not in hw["quarantined"]


def test_record_status_is_removed_for_a_permanent_only_retirement(tmp_path):
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::gone"}), merged=True)
    _write_scorecard(cfg, scorecard_from(cfg, {"s::t1"}))
    args = _record_args(
        tmp_path, retire_case=["s::gone"], retire_case_reason="deleted",
        retire_case_owner="plwp", retire_case_permanent=True,
    )
    assert ratchet.cmd_record(args) == 0
    recs = ratchet.load_journal(cfg)
    assert recs[-1]["ratchet_status"] == "removed"


def test_recent_prints_the_removed_status_distinctly_from_quarantined(tmp_path, capsys):
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::gone"}), merged=True)
    _write_scorecard(cfg, scorecard_from(cfg, {"s::t1"}))
    args = _record_args(
        tmp_path, retire_case=["s::gone"], retire_case_reason="deleted",
        retire_case_owner="plwp", retire_case_permanent=True,
    )
    assert ratchet.cmd_record(args) == 0
    capsys.readouterr()
    assert ratchet.cmd_recent(argparse.Namespace(repo=str(tmp_path), n=5)) == 0
    out = capsys.readouterr().out
    assert "[removed]" in out
    assert "[quarantined]" not in out


def test_check_reports_removed_cases_separately_from_quarantined(tmp_path, capsys):
    cfg = make_repo(tmp_path)
    append_record(
        cfg, scorecard_from(cfg, {"s::t1", "s::flaky", "s::gone"}), merged=True,
    )
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=False, retired_cases=[
        {"id": "s::flaky", "reason": "order-dependent", "owner": "plwp",
         "expiry": "2099-01-01", "created_at": "2026-08-03T00:00:00Z"},
        {"id": "s::gone", "reason": "deleted", "owner": "plwp", "expiry": None,
         "created_at": "2026-08-03T00:00:00Z", "kind": "removed"},
    ])
    _write_scorecard(cfg, scorecard_from(cfg, {"s::t1"}))
    args = argparse.Namespace(repo=str(tmp_path), format="text", gate_quality=False)
    assert ratchet.cmd_check(args) == 0
    out = capsys.readouterr()
    assert "1 case(s) quarantined" in out.out
    assert "1 case(s) permanently retired" in out.out
    assert "s::gone" in out.err
    assert "permanently retired" in out.err

    args_json = argparse.Namespace(repo=str(tmp_path), format="json", gate_quality=False)
    assert ratchet.cmd_check(args_json) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["quarantined"][0]["id"] == "s::flaky"
    assert data["removed_cases"][0]["id"] == "s::gone"


def test_check_ok_line_unchanged_with_no_removed_cases(tmp_path, capsys):
    """Regression pin: the pre-#290 OK string must stay byte-identical when
    nothing is quarantined AND nothing is permanently retired."""
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=True)
    _write_scorecard(cfg, scorecard_from(cfg, {"s::t1"}))
    args = argparse.Namespace(repo=str(tmp_path), format="text", gate_quality=False)
    assert ratchet.cmd_check(args) == 0
    out = capsys.readouterr().out
    assert out.strip() == "ratchet: OK (pass-set and contract definitions hold the high-water mark)"


def test_check_json_gains_removed_cases_key_additively(tmp_path, capsys):
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=True)
    _write_scorecard(cfg, scorecard_from(cfg, {"s::t1"}))
    args = argparse.Namespace(repo=str(tmp_path), format="json", gate_quality=False)
    assert ratchet.cmd_check(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["removed_cases"] == []


def test_highwater_includes_removed_cases(tmp_path, capsys):
    cfg = make_repo(tmp_path)
    append_record(cfg, scorecard_from(cfg, {"s::t1", "s::gone"}), merged=True)
    append_record(cfg, scorecard_from(cfg, {"s::t1"}), merged=False, retired_cases=[
        {"id": "s::gone", "reason": "deleted", "owner": "plwp", "expiry": None,
         "created_at": "2026-08-03T00:00:00Z", "kind": "removed"},
    ])
    assert ratchet.cmd_highwater(argparse.Namespace(repo=str(tmp_path))) == 0
    data = json.loads(capsys.readouterr().out)
    assert "s::gone" in data["removed_cases"]


def test_backward_compat_journal_without_removed_cases_key(tmp_path):
    """A journal predating #290 has NO entries carrying a 'kind' key at all —
    derive_highwater must default them all to quarantined, and violations()
    must tolerate a highwater dict from an even-older cache lacking
    'removed_cases' entirely."""
    hw = {"pass_set": ["s::t1"], "contract_hashes": {}, "quarantined": {}}
    sc = {"pass_set": ["s::t1"], "contract_hashes": {}}
    v = ratchet.violations(sc, hw)
    assert v["removed_cases"] == []


def test_retire_case_permanent_via_real_cli(tmp_path):
    cfg = _prep_quarantine_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--retire-case", "s::flaky",
         "--retire-case-reason", "renamed, id gone for good",
         "--retire-case-owner", "plwp", "--retire-case-permanent"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "status=removed" in proc.stdout, proc.stdout
    hw = ratchet.derive_highwater(ratchet.load_journal(cfg))
    assert "s::flaky" in hw["removed_cases"]


def test_retire_case_permanent_via_real_cli_rejects_default_owner(tmp_path):
    _prep_quarantine_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--retire-case", "s::flaky",
         "--retire-case-reason", "renamed", "--retire-case-permanent"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "owner" in proc.stderr.lower()


def test_retire_case_permanent_via_real_cli_rejects_expiry(tmp_path):
    _prep_quarantine_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "ratchet.py"), "record", "--repo", str(tmp_path),
         "--event", "ticket", "--retire-case", "s::flaky",
         "--retire-case-reason", "renamed", "--retire-case-owner", "plwp",
         "--retire-case-permanent", "--retire-case-expiry", "2099-01-01"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "expiry" in proc.stderr.lower()


# ---- state classification (#356) ------------------------------------------------
# A greenfield repo whose only ratchet.json is the apply_pattern.py stub must
# not read as "real quality history" to /architect's new-product check — and
# an error state must stay distinct from every classification.


def _quality_dir(tmp_path):
    d = tmp_path / "docs" / "quality"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_state_absent(tmp_path):
    state, _ = ratchet.classify_state(_quality_dir(tmp_path))
    assert state == "absent"


def test_state_stub_marker(tmp_path):
    (_quality_dir(tmp_path) / "ratchet.json").write_text(json.dumps(
        {"$comment": ratchet.STUB_COMMENT, "protected_paths": []}))
    state, _ = ratchet.classify_state(_quality_dir(tmp_path))
    assert state == "stub"


def test_state_unbaselined_real_config_no_journal(tmp_path):
    """An init-style config that was never journaled is still not history."""
    make_repo(tmp_path)
    state, _ = ratchet.classify_state(_quality_dir(tmp_path))
    assert state == "unbaselined"


def test_state_real_when_journal_has_records(tmp_path):
    make_repo(tmp_path)
    (_quality_dir(tmp_path) / "ratchet-journal.jsonl").write_text(
        json.dumps({"event": "baseline"}) + "\n")
    state, _ = ratchet.classify_state(_quality_dir(tmp_path))
    assert state == "real"


def test_state_blank_journal_is_not_history(tmp_path):
    make_repo(tmp_path)
    (_quality_dir(tmp_path) / "ratchet-journal.jsonl").write_text("\n\n")
    state, _ = ratchet.classify_state(_quality_dir(tmp_path))
    assert state == "unbaselined"


def test_state_corrupt_config_is_invalid_not_absent_or_real(tmp_path):
    (_quality_dir(tmp_path) / "ratchet.json").write_text("{not json")
    state, _ = ratchet.classify_state(_quality_dir(tmp_path))
    assert state == "invalid"


def test_state_non_object_config_is_invalid(tmp_path):
    (_quality_dir(tmp_path) / "ratchet.json").write_text("[]")
    state, _ = ratchet.classify_state(_quality_dir(tmp_path))
    assert state == "invalid"


def test_apply_pattern_stub_classifies_as_stub(tmp_path):
    """#356 AC: the stub apply_pattern.py actually writes and the classifier
    can never drift into being indistinguishable — this exercises the real
    writer (_merge_ratchet), not a copy of its output."""
    import apply_pattern
    content, added = apply_pattern._merge_ratchet(tmp_path, ["docs/patterns/**"])
    assert content is not None and added
    (_quality_dir(tmp_path) / "ratchet.json").write_text(content)
    state, reason = ratchet.classify_state(_quality_dir(tmp_path))
    assert state == "stub"
    assert "apply_pattern" in reason


def test_cmd_state_cli_word_on_stdout(tmp_path, capsys):
    """/architect consumes exactly one word on stdout; reason goes to stderr."""
    make_repo(tmp_path)
    rc = ratchet.cmd_state(argparse.Namespace(repo=str(tmp_path)))
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "unbaselined"
    assert "ratchet: state=" in out.err
