"""Tests for scripts/check_ai_act.py (chief-wiggum#316)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_ai_act  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def user_dir(tmp_path, monkeypatch):
    """Isolate every test from the real ~/.chief-wiggum, like test_tracker.py."""
    d = tmp_path / "cw-user"
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(d))
    return d


def _write_artifact(target_repo: Path, data: dict) -> Path:
    path = check_ai_act.artifact_path(target_repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def _rules(findings):
    return {f.rule for f in findings}


MINIMAL_FEATURE = {
    "feature_id": "AIACT-CHATBOT-001",
    "role": "deployer",
    "tier": "minimal",
    "performs_profiling": False,
    "obligations": [],
    "evidence": [],
}


# --- Art. 6(4): absence vs. a recorded "not high risk" ----------------------


def test_missing_artifact_is_findings_not_a_silent_pass(tmp_path):
    report = check_ai_act.load(tmp_path)
    assert report.classification_status == "missing"
    assert report.outcome == "findings"


def test_recorded_empty_features_is_inapplicable(tmp_path):
    _write_artifact(tmp_path, {"eu_scope": "out_of_scope", "eu_scope_reason": "no EU users", "features": []})
    report = check_ai_act.load(tmp_path)
    assert report.classification_status == "recorded"
    assert report.outcome == "inapplicable"
    assert report.findings == []


def test_unparseable_artifact_is_error(tmp_path):
    path = check_ai_act.artifact_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    report = check_ai_act.load(tmp_path)
    assert report.outcome == "error"
    assert report.unparsed_reason is not None


def test_no_features_array_is_error(tmp_path):
    _write_artifact(tmp_path, {"eu_scope": "TBD", "eu_scope_reason": "x"})
    report = check_ai_act.load(tmp_path)
    assert report.outcome == "error"


# --- clean pass ---------------------------------------------------------------


def test_single_minimal_feature_passes(tmp_path):
    _write_artifact(tmp_path, {
        "eu_scope": "in_scope", "eu_scope_reason": "AU factory building for EU market",
        "features": [MINIMAL_FEATURE],
    })
    report = check_ai_act.load(tmp_path)
    assert report.outcome == "pass"
    assert report.n_features == 1


def test_eu_scope_undeclared_is_a_finding(tmp_path):
    _write_artifact(tmp_path, {"features": [MINIMAL_FEATURE]})
    report = check_ai_act.load(tmp_path)
    assert "eu_scope_undeclared" in _rules(report.findings)


# --- Art. 5 screen -------------------------------------------------------------


def test_prohibited_tier_is_a_fail_not_a_warn(tmp_path):
    feat = dict(MINIMAL_FEATURE, tier="prohibited")
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    report = check_ai_act.load(tmp_path)
    hit = next(f for f in report.findings if f.rule == "art5_prohibited_practice")
    assert hit.severity == "fail"
    assert report.n_prohibited == 1


# --- profiling always voids the derogation ------------------------------------


def test_profiling_true_with_minimal_tier_is_misclassified(tmp_path):
    feat = dict(MINIMAL_FEATURE, performs_profiling=True)
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    report = check_ai_act.load(tmp_path)
    assert "profiling_misclassified" in _rules(report.findings)


def test_missing_profiling_flag_is_a_fail(tmp_path):
    feat = {k: v for k, v in MINIMAL_FEATURE.items() if k != "performs_profiling"}
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    report = check_ai_act.load(tmp_path)
    assert "missing_profiling_flag" in _rules(report.findings)


# --- Annex III / Art. 6(4) derogation documentation ---------------------------


def test_annex_iii_without_derogation_assessment_is_undocumented(tmp_path):
    feat = dict(MINIMAL_FEATURE, tier="high_risk_annex_iii", annex_iii_area=4)
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    report = check_ai_act.load(tmp_path)
    assert "annex_iii_undocumented_assessment" in _rules(report.findings)
    assert report.n_high_risk_annex_iii == 1


def test_annex_iii_with_named_condition_documents_the_claim(tmp_path):
    feat = dict(
        MINIMAL_FEATURE, tier="high_risk_annex_iii", annex_iii_area=4,
        derogation_assessment={"condition": "narrow_procedural_task", "reasoning": "..."},
    )
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    report = check_ai_act.load(tmp_path)
    assert "annex_iii_undocumented_assessment" not in _rules(report.findings)


def test_annex_iii_without_area_is_a_fail(tmp_path):
    feat = dict(
        MINIMAL_FEATURE, tier="high_risk_annex_iii",
        derogation_assessment={"condition": "preparatory_task"},
    )
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    report = check_ai_act.load(tmp_path)
    assert "annex_iii_area_missing" in _rules(report.findings)


def test_annex_iii_area_on_wrong_tier_is_a_warn(tmp_path):
    feat = dict(MINIMAL_FEATURE, annex_iii_area=2)
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    report = check_ai_act.load(tmp_path)
    hit = next(f for f in report.findings if f.rule == "annex_iii_area_on_wrong_tier")
    assert hit.severity == "warn"


# --- Art. 50 transparency ------------------------------------------------------


def test_transparency_tier_requires_art50_obligation(tmp_path):
    feat = dict(MINIMAL_FEATURE, tier="transparency_art50", obligations=[], evidence=["x.py:12"])
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    report = check_ai_act.load(tmp_path)
    assert "art50_obligation_undeclared" in _rules(report.findings)


def test_transparency_tier_with_no_evidence_is_a_warn(tmp_path):
    feat = dict(MINIMAL_FEATURE, tier="transparency_art50", obligations=["Art. 50(1)"], evidence=[])
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    report = check_ai_act.load(tmp_path)
    hit = next(f for f in report.findings if f.rule == "art50_no_evidence")
    assert hit.severity == "warn"
    assert report.n_transparency == 1


def test_transparency_tier_fully_declared_passes(tmp_path):
    feat = dict(MINIMAL_FEATURE, tier="transparency_art50", obligations=["Art. 50(1)"],
                evidence=["src/chat_widget.py:42"])
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    report = check_ai_act.load(tmp_path)
    assert report.outcome == "pass"


# --- malformed feature entries --------------------------------------------------


def test_malformed_feature_id_is_a_fail(tmp_path):
    feat = dict(MINIMAL_FEATURE, feature_id="not-an-id")
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    report = check_ai_act.load(tmp_path)
    assert "malformed_feature_id" in _rules(report.findings)


def test_invalid_role_is_a_fail(tmp_path):
    feat = dict(MINIMAL_FEATURE, role="hobbyist")
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    report = check_ai_act.load(tmp_path)
    assert "invalid_role" in _rules(report.findings)


def test_invalid_tier_is_a_fail(tmp_path):
    feat = dict(MINIMAL_FEATURE, tier="somewhat_risky")
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    report = check_ai_act.load(tmp_path)
    assert "invalid_tier" in _rules(report.findings)


def test_non_object_feature_entry_is_a_fail(tmp_path):
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": ["oops"]})
    report = check_ai_act.load(tmp_path)
    assert "malformed_feature" in _rules(report.findings)


# --- CLI ------------------------------------------------------------------------


def test_cli_report_only_exits_zero_even_with_prohibited_finding(tmp_path, capsys):
    feat = dict(MINIMAL_FEATURE, tier="prohibited")
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    rc = check_ai_act.main([str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "art5_prohibited_practice" in out


def test_cli_gate_blocks_on_fail_finding(tmp_path):
    feat = dict(MINIMAL_FEATURE, tier="prohibited")
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [feat]})
    rc = check_ai_act.main([str(tmp_path), "--gate"])
    assert rc == 1


def test_cli_gate_passes_on_clean_artifact(tmp_path):
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [MINIMAL_FEATURE]})
    rc = check_ai_act.main([str(tmp_path), "--gate"])
    assert rc == 0


def test_cli_gate_blocks_on_missing_artifact(tmp_path):
    rc = check_ai_act.main([str(tmp_path), "--gate"])
    assert rc == 1


def test_cli_json_format_round_trips(tmp_path, capsys):
    _write_artifact(tmp_path, {"eu_scope": "in_scope", "eu_scope_reason": "x", "features": [MINIMAL_FEATURE]})
    check_ai_act.main([str(tmp_path), "--format", "json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["classification_status"] == "recorded"
    assert parsed["outcome"] == "pass"
    assert parsed["measured"]["features_declared"] == 1
