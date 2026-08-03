"""Tests for the `finding` record type + kill-brief convening triggers (chief-wiggum#252).

Found while dogfooding the kill-review quorum: a material external fact (a competitor
twin discovered post-creation) changed a bet's premise and triggered an ad-hoc review,
but the brief generator correctly refused to carry it — an externally-researched finding
had no journal channel, so the brief omitted the entire reason for the review. This adds
a journaled `finding` record type, a "Material findings" brief section, and an explicit
convening-trigger statement (`criterion` vs `premise-change`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bet as betlib  # noqa: E402
import ratchet  # noqa: E402

BET = str(SCRIPTS / "bet.py")

ENVELOPE = {
    "cash_cap_usd": 900,
    "liability_exposure": {"type": "capped_at", "amount_usd": 900},
    "tranches": [{"amount_usd": 900, "unlock_milestone_id": None}],
}
CRITERIA = {
    "criteria": [
        {"id": "KC-1", "metric": "paid_conversions", "comparator": ">=",
         "threshold": 3, "by_date": "2026-07-01", "direction": "has"},
    ]
}


@pytest.fixture
def portfolio(tmp_path):
    return tmp_path / "portfolio"


def _run(portfolio: Path, *argv: str, env_extra: dict | None = None):
    import os
    env = dict(os.environ)
    env["CHIEF_WIGGUM_PORTFOLIO"] = str(portfolio)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, BET, *argv, "--portfolio-dir", str(portfolio)],
        capture_output=True, text=True, env=env,
    )


def _create(portfolio, tmp_path, bet_id="b1", *extra, criteria=None):
    env_p, crit_p = tmp_path / "envelope.json", tmp_path / "criteria.json"
    env_p.write_text(json.dumps(ENVELOPE))
    crit_p.write_text(json.dumps(criteria or CRITERIA))
    return _run(portfolio, "create", bet_id, "--title", f"bet {bet_id}",
                "--envelope", str(env_p), "--criteria", str(crit_p), *extra)


def _evaluate(portfolio, tmp_path, bet_id="b1", results=None, as_of="2026-08-02"):
    args = ["evaluate", bet_id, "--as-of", as_of]
    if results is not None:
        rp = tmp_path / "results.json"
        rp.write_text(json.dumps(results))
        args += ["--results", str(rp)]
    return _run(portfolio, *args)


def _journal(portfolio: Path) -> list[dict]:
    return ratchet.load_journal(SimpleNamespace(journal=portfolio / "journal.jsonl"))


def _stub(tmp_path, verdicts: dict[str, str], rc: int = 0) -> dict:
    stub = tmp_path / "stub_consult.py"
    stub.write_text(
        "import argparse, json, sys\n"
        "from pathlib import Path\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('prompt'); p.add_argument('--role', required=True)\n"
        "p.add_argument('--output-dir', required=True)\n"
        "a = p.parse_args()\n"
        f"verdicts = {verdicts!r}\n"
        f"rc = {rc!r}\n"
        "out = Path(a.output_dir); out.mkdir(parents=True, exist_ok=True)\n"
        "results = []\n"
        "for name, text in verdicts.items():\n"
        "    path = out / f'{a.role}-{name}.md'\n"
        "    path.write_text(text)\n"
        "    results.append({'name': name, 'required': name != 'claude-interactive',\n"
        "                    'status': 'ok', 'path': str(path), 'attempts': 1, 'error': None})\n"
        "(out / f'{a.role}-manifest.json').write_text(json.dumps(\n"
        "    {'role': a.role, 'ok': not rc, 'failed_required': [], 'results': results}))\n"
        "sys.exit(rc)\n"
    )
    return {"CW_CONSULT_AI": str(stub)}


# ---- `finding` command: malformed refusal + successful record ------------------

def test_finding_without_source_url_is_refused(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    r = _run(portfolio, "finding", "b1", "--statement", "a competitor twin exists",
              "--source-url", "", "--bearing-on", "premise")
    assert r.returncode == 2
    assert "source-url" in r.stdout + r.stderr


def test_finding_missing_source_url_flag_is_a_usage_error(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    r = _run(portfolio, "finding", "b1", "--statement", "x", "--bearing-on", "premise")
    assert r.returncode != 0  # argparse required-arg error


def test_finding_without_statement_is_refused(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    r = _run(portfolio, "finding", "b1", "--statement", "",
              "--source-url", "https://example.com/thread", "--bearing-on", "premise")
    assert r.returncode == 2
    assert "statement" in r.stdout + r.stderr


def test_finding_with_bad_bearing_on_is_refused(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    r = _run(portfolio, "finding", "b1", "--statement", "x",
              "--source-url", "https://example.com", "--bearing-on", "not-a-real-target")
    assert r.returncode == 2
    assert "bearing-on" in r.stdout + r.stderr


def test_finding_accepts_premise_asm_and_kc_bearing_on(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    for i, target in enumerate(("premise", "ASM-001", "KC-1")):
        r = _run(portfolio, "finding", "b1", "--statement", f"fact {i}",
                  "--source-url", f"https://example.com/{i}", "--bearing-on", target)
        assert r.returncode == 0, r.stdout + r.stderr


def test_finding_is_journaled_with_evidence_grade_default(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    r = _run(portfolio, "finding", "b1", "--statement", "a direct competitor twin exists",
              "--source-url", "https://example.com/thread", "--bearing-on", "premise")
    assert r.returncode == 0, r.stdout + r.stderr
    recs = [rec for rec in _journal(portfolio) if rec["event"] == "finding"]
    assert len(recs) == 1
    d = recs[0]["details"]
    assert d["statement"] == "a direct competitor twin exists"
    assert d["source_url"] == "https://example.com/thread"
    assert d["bearing_on"] == "premise"
    assert d["evidence_grade"] == "reported"


def test_finding_respects_explicit_evidence_grade(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    r = _run(portfolio, "finding", "b1", "--statement", "x",
              "--source-url", "https://example.com", "--bearing-on", "premise",
              "--evidence-grade", "verified")
    assert r.returncode == 0
    rec = [rec for rec in _journal(portfolio) if rec["event"] == "finding"][-1]
    assert rec["details"]["evidence_grade"] == "verified"


def test_finding_bad_evidence_grade_is_argparse_error(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    r = _run(portfolio, "finding", "b1", "--statement", "x",
              "--source-url", "https://example.com", "--bearing-on", "premise",
              "--evidence-grade", "gut-feeling")
    assert r.returncode != 0


def test_finding_on_unknown_bet_fails(portfolio, tmp_path):
    r = _run(portfolio, "finding", "does-not-exist", "--statement", "x",
              "--source-url", "https://example.com", "--bearing-on", "premise")
    assert r.returncode != 0


# ---- kill-brief: Material findings section --------------------------------------

def test_kill_brief_shows_no_findings_recorded_by_default(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    proc = _run(portfolio, "kill-brief", "b1", "--as-of", "2026-06-01")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "## Material findings" in proc.stdout
    assert "none recorded" in proc.stdout


def test_kill_brief_cites_a_recorded_finding(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _run(portfolio, "finding", "b1", "--statement", "a direct competitor twin exists",
         "--source-url", "https://example.com/thread", "--bearing-on", "premise",
         "--evidence-grade", "verified")
    proc = _run(portfolio, "kill-brief", "b1", "--as-of", "2026-06-01")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "a direct competitor twin exists" in proc.stdout
    assert "https://example.com/thread" in proc.stdout
    assert "verified" in proc.stdout
    assert "bearing on premise" in proc.stdout
    # journal-record citation, not a bare URL fact with no source
    assert "[source: rec-" in proc.stdout


def test_kill_brief_purity_holds_with_a_finding_present(portfolio, tmp_path):
    """A recorded finding must not itself trip the brief-purity self-check — it is
    journal-backed by construction (refused at record time without a source-url)."""
    _create(portfolio, tmp_path, criteria={"criteria": []})
    _run(portfolio, "finding", "b1", "--statement", "a direct competitor twin exists",
         "--source-url", "https://example.com/thread", "--bearing-on", "premise")
    text, findings, meta = betlib.build_kill_brief(
        betlib.portfolio_root(str(portfolio)), "b1", date(2026, 6, 1))
    assert findings == []


# ---- convening trigger: auto-detect + explicit override ------------------------

def test_trigger_defaults_to_criterion_when_none_fired_and_no_findings(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    text, findings, meta = betlib.build_kill_brief(
        betlib.portfolio_root(str(portfolio)), "b1", date(2026, 6, 1))
    assert "Convening trigger: **criterion**" in text


def test_trigger_is_criterion_when_a_criterion_fired(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _evaluate(portfolio, tmp_path, results={"paid_conversions": 1})
    text, findings, meta = betlib.build_kill_brief(
        betlib.portfolio_root(str(portfolio)), "b1", date(2026, 8, 2))
    assert "Convening trigger: **criterion**" in text


def test_trigger_auto_detects_premise_change_from_a_premise_finding(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _run(portfolio, "finding", "b1", "--statement", "a direct competitor twin exists",
         "--source-url", "https://example.com/thread", "--bearing-on", "premise")
    text, findings, meta = betlib.build_kill_brief(
        betlib.portfolio_root(str(portfolio)), "b1", date(2026, 6, 1))
    assert "Convening trigger: **premise-change**" in text
    assert "NOT by itself grounds for `hold`" in text


def test_trigger_is_criterion_not_premise_change_when_finding_bears_on_an_asm(portfolio, tmp_path):
    """A finding bearing on an ASM (not 'premise') must not auto-flip the trigger."""
    _create(portfolio, tmp_path)
    _run(portfolio, "finding", "b1", "--statement", "x",
         "--source-url", "https://example.com", "--bearing-on", "ASM-001")
    text, findings, meta = betlib.build_kill_brief(
        betlib.portfolio_root(str(portfolio)), "b1", date(2026, 6, 1))
    assert "Convening trigger: **criterion**" in text


def test_explicit_trigger_overrides_auto_detection(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    text, findings, meta = betlib.build_kill_brief(
        betlib.portfolio_root(str(portfolio)), "b1", date(2026, 6, 1), trigger="premise-change")
    assert "Convening trigger: **premise-change**" in text


def test_kill_brief_cli_accepts_trigger_flag(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    proc = _run(portfolio, "kill-brief", "b1", "--as-of", "2026-06-01",
                 "--trigger", "premise-change")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Convening trigger: **premise-change**" in proc.stdout


# ---- fairness rule holds under the new trigger ----------------------------------

def test_fairness_downgrade_still_applies_under_premise_change_trigger(portfolio, tmp_path):
    """The distribution-fairness rule (#241) must not gain a back door via the new
    trigger: a demand-shaped criterion fired with distribution unattempted still
    downgrades a parsed `kill` to `recycle`, even when the review is explicitly
    convened as `premise-change` (e.g. a human overriding auto-detection after a
    material finding, while a criterion also happens to have fired)."""
    _create(portfolio, tmp_path)
    _evaluate(portfolio, tmp_path, results={"paid_conversions": 1})
    _run(portfolio, "finding", "b1", "--statement", "a direct competitor twin exists",
         "--source-url", "https://example.com/thread", "--bearing-on", "premise")
    env = _stub(tmp_path, {
        "codex": 'analysis...\n```json\n{"verdict": "kill", "confidence": 0.7, '
                 '"reasons": ["KC-1 fired"]}\n```\n',
        "opus": 'analysis...\n```json\n{"verdict": "hold", "confidence": 0.6, '
                '"reasons": ["x"], "cheapest_disconfirming_test": "y"}\n```\n',
    })
    proc = _run(portfolio, "kill-review", "b1", "--as-of", "2026-08-02",
                "--trigger", "premise-change", env_extra=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "downgraded from `kill` by the distribution-fairness rule" in proc.stdout
    rec = [r for r in _journal(portfolio) if r["event"] == "kill-review"][-1]
    assert rec["details"]["fairness_downgraded"] == ["codex"]
