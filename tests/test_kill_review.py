"""Tests for the kill-review quorum (chief-wiggum#237, scripts/bet.py).

Seeded-defect coverage per the issue's "done looks like": brief purity (an
impure brief — narrative thesis prose, an unsourced value, a foreign source —
is refused), a bet with no channel/rep evidence marks distribution
`unattempted` in the brief, the distribution-fairness rule downgrades a parsed
`kill` verdict to `recycle` (and only when a demand-shaped criterion fired
with distribution unattempted), malformed provider output is flagged not
crashed, and the journal gains a `kill-review` event carrying the brief hash.

The consult layer is NEVER called for real: `CW_CONSULT_AI` points bet.py's
kill-review at a stub that writes fixture verdict files with the same argv +
output contract as `consult_ai.py --role` (the same seam the transcript uses).
Everything runs against a tmp_path portfolio — never the real ~/.chief-wiggum.
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
import consult_ai  # noqa: E402
import providers as providers_mod  # noqa: E402
import ratchet  # noqa: E402
from chief_wiggum.hashing import stable_hash  # noqa: E402

BET = str(SCRIPTS / "bet.py")

ENVELOPE = {
    "cash_cap_usd": 900,
    "time_cap_hours": 120,
    "tranches": [
        {"amount_usd": 300, "unlock_milestone_id": None},
        {"amount_usd": 600, "unlock_milestone_id": "M1"},
    ],
}

CRITERIA = {
    "criteria": [
        {"id": "KC-1", "metric": "paid_conversions", "comparator": ">=",
         "threshold": 3, "by_date": "2026-07-01", "direction": "has"},
        {"id": "KC-2", "metric": "refund_rate_pct", "comparator": ">",
         "threshold": 30, "by_date": "2026-11-01", "direction": "has_not"},
    ]
}

KILL_JSON = json.dumps({
    "verdict": "kill", "confidence": 0.7,
    "reasons": ["KC-1 fired: 1 paid conversion vs pre-registered >=3"],
})
HOLD_JSON = json.dumps({
    "verdict": "hold", "confidence": 0.6,
    "reasons": ["distribution unattempted"],
    "cheapest_disconfirming_test": "run 3 reps/week for 2 weeks, re-evaluate",
})


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


def _create(portfolio, tmp_path, bet_id="b1", *extra, criteria=None, thesis=""):
    env_p = tmp_path / "envelope.json"
    crit_p = tmp_path / "criteria.json"
    env_p.write_text(json.dumps(ENVELOPE))
    crit_p.write_text(json.dumps(criteria or CRITERIA))
    return _run(portfolio, "create", bet_id, "--title", f"bet {bet_id}",
                "--thesis", thesis, "--envelope", str(env_p),
                "--criteria", str(crit_p), *extra)


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
    """Write a stub consult entrypoint honoring consult_ai --role's argv +
    output contract (provider files + manifest); returns the env override."""
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


# ---- kill-brief: journal-backed values only -------------------------------------


def test_kill_brief_is_pure_and_sources_every_measured_value(portfolio, tmp_path):
    _create(portfolio, tmp_path, thesis="secret narrative thesis about the market")
    _evaluate(portfolio, tmp_path, results={"paid_conversions": 1})
    proc = _run(portfolio, "kill-brief", "b1", "--as-of", "2026-08-02")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # criteria verbatim + hash citation to the create record
    assert '"paid_conversions"' in proc.stdout
    assert "[source: rec-00001]" in proc.stdout
    # measured value cites the journaled kill-proposed evaluation
    assert "KC-1 paid_conversions (demand-shaped): 1" in proc.stdout
    assert "[source: rec-00002]" in proc.stdout
    # envelope status cites artifact files
    assert "[source: bets/b1/ledger.jsonl]" in proc.stdout
    # NO narrative: the thesis never reaches the fresh-context evaluator
    assert "secret narrative thesis" not in proc.stdout
    brief = (portfolio / "bets" / "b1" / "kill-brief.md").read_text()
    assert "secret narrative thesis" not in brief


def test_kill_brief_without_evaluation_is_unresolved_not_prose(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    proc = _run(portfolio, "kill-brief", "b1", "--as-of", "2026-06-01")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "UNRESOLVED: no journaled evaluation covers this criterion" in proc.stdout


def test_kill_brief_marks_distribution_unattempted(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    proc = _run(portfolio, "kill-brief", "b1", "--as-of", "2026-06-01")
    assert proc.returncode == 0
    assert "distribution: unattempted" in proc.stdout
    assert "UNRESOLVED: no channel experiments — no exposure was delivered" in proc.stdout


def test_kill_brief_shows_attempted_distribution_evidence(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _run(portfolio, "spend", "b1", "--rep", "--note", "20 cold emails")
    (portfolio / "bets" / "b1" / "channels.json").write_text(json.dumps({
        "channels": [{"channel": "search-engine-optimization", "status": "testing",
                      "customers_acquired": 2, "visitors": 340}]
    }))
    proc = _run(portfolio, "kill-brief", "b1", "--as-of", "2026-06-01")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "distribution: attempted" in proc.stdout
    assert "search-engine-optimization (testing)" in proc.stdout
    assert "340 visitors" in proc.stdout
    assert "[source: bets/b1/channels.json]" in proc.stdout


def test_kill_brief_includes_open_assumption_evidence(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    (portfolio / "bets" / "b1" / "assumptions.json").write_text(json.dumps({
        "assumptions": [
            {"id": "ASM-001", "statement": "at least 5% of AU dog trainers will pay",
             "status": "testing", "source": "premortem"},
            {"id": "ASM-002", "statement": "at least 10% of vets will book",
             "status": "validated", "source": "canvas"},
        ]
    }))
    proc = _run(portfolio, "kill-brief", "b1", "--as-of", "2026-06-01")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 open, 1 settled" in proc.stdout
    assert "ASM-001 [testing]" in proc.stdout
    assert "ASM-002 [" not in proc.stdout  # settled ASMs are counted, not tabled
    assert "[source: bets/b1/assumptions.json]" in proc.stdout


# ---- seeded defect: brief purity ------------------------------------------------


def test_brief_purity_flags_seeded_impure_brief():
    bet = {"id": "b1", "thesis": "we believe the market is huge"}
    impure_text = (
        "# Kill brief: b1\n\n"
        "History: we believe the market is huge and last month was promising.\n"
        "- paid_conversions: 7\n"
        "- churn_rate_pct: 4 [source: https://dashboard.example.com]\n"
    )
    facts = [
        {"label": "paid_conversions", "value": 7, "source": None},  # unsourced value
        {"label": "churn_rate_pct", "value": 4, "source": "https://dashboard.example.com"},
    ]
    findings = betlib.brief_purity_findings(impure_text, bet, facts)
    assert any("unsourced value" in f for f in findings)
    assert any("thesis prose leaked" in f for f in findings)
    assert any("non-journal, non-artifact source" in f for f in findings)


def test_brief_purity_clean_on_generated_brief(portfolio, tmp_path):
    _create(portfolio, tmp_path, thesis="a thesis that must not leak")
    _evaluate(portfolio, tmp_path, results={"paid_conversions": 1})
    text, findings, meta = betlib.build_kill_brief(
        betlib.portfolio_root(str(portfolio)), "b1", date(2026, 8, 2))
    assert findings == []
    assert meta["fired_demand"] == ["KC-1"]
    assert meta["distribution"]["status"] == "unattempted"


def test_kill_brief_refuses_on_goalpost_tamper(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    crit_path = portfolio / "bets" / "b1" / "kill-criteria.json"
    doc = json.loads(crit_path.read_text())
    doc["criteria"][0]["threshold"] = 1  # hand-lowered goalpost
    crit_path.write_text(json.dumps(doc))
    proc = _run(portfolio, "kill-brief", "b1")
    assert proc.returncode == 1
    assert "REFUSED" in proc.stdout
    assert "does not match the journaled baseline" in proc.stdout


# ---- kill-review: quorum, fairness, journaling ----------------------------------


def test_kill_review_downgrades_kill_when_distribution_unattempted(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _evaluate(portfolio, tmp_path, results={"paid_conversions": 1})
    env = _stub(tmp_path, {
        "codex": f"analysis...\n```json\n{KILL_JSON}\n```\n",
        "opus": f"analysis...\n```json\n{HOLD_JSON}\n```\n",
    })
    proc = _run(portfolio, "kill-review", "b1", "--as-of", "2026-08-02", env_extra=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "codex: RECYCLE" in proc.stdout
    assert "downgraded from `kill` by the distribution-fairness rule" in proc.stdout
    # the finding names the cheapest untried exposure (founder reps at $0)
    assert "cheapest untried exposure: founder reps" in proc.stdout
    assert "opus: HOLD" in proc.stdout
    rec = [r for r in _journal(portfolio) if r["event"] == "kill-review"][-1]
    assert rec["details"]["fairness_downgraded"] == ["codex"]
    codex = next(v for v in rec["details"]["verdicts"] if v["provider"] == "codex")
    assert codex["verdict"] == "recycle" and codex["downgraded_from"] == "kill"


def test_kill_review_shows_verdicts_before_decision_instructions(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _evaluate(portfolio, tmp_path, results={"paid_conversions": 1})
    env = _stub(tmp_path, {
        "codex": f"```json\n{KILL_JSON}\n```",
        "opus": f"```json\n{HOLD_JSON}\n```",
    })
    proc = _run(portfolio, "kill-review", "b1", "--as-of", "2026-08-02", env_extra=env)
    out = proc.stdout
    # Ordering invariant (#237 decision 4): fresh verdicts anchor the decision.
    assert out.index("quorum verdicts") < out.index("your decision")
    assert out.index("opus: HOLD") < out.index("accept the kill")
    assert "transition b1 kill_pending" in out
    assert "--override-kill --reason" in out


def test_kill_review_journals_brief_hash(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _evaluate(portfolio, tmp_path, results={"paid_conversions": 1})
    env = _stub(tmp_path, {"codex": f"```json\n{HOLD_JSON}\n```",
                           "opus": f"```json\n{HOLD_JSON}\n```"})
    proc = _run(portfolio, "kill-review", "b1", "--as-of", "2026-08-02", env_extra=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rec = [r for r in _journal(portfolio) if r["event"] == "kill-review"][-1]
    brief = (portfolio / "bets" / "b1" / "kill-brief.md").read_text()
    assert rec["details"]["brief_hash"] == stable_hash(brief)
    assert rec["details"]["brief_path"] == "bets/b1/kill-brief.md"
    assert rec["details"]["distribution_status"] == "unattempted"


def test_kill_review_tolerates_malformed_verdict_output(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _evaluate(portfolio, tmp_path, results={"paid_conversions": 1})
    env = _stub(tmp_path, {
        "codex": f"```json\n{HOLD_JSON}\n```",
        "opus": f"```json\n{HOLD_JSON}\n```",
        "claude-interactive": "rambling prose, no json block at all",
    })
    proc = _run(portfolio, "kill-review", "b1", "--as-of", "2026-08-02", env_extra=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr  # flagged, not crashed
    assert "claude-interactive: MALFORMED verdict output" in proc.stdout
    assert "malformed verdict output — no parseable fenced JSON" in proc.stdout
    rec = [r for r in _journal(portfolio) if r["event"] == "kill-review"][-1]
    flagged = next(v for v in rec["details"]["verdicts"]
                   if v["provider"] == "claude-interactive")
    assert flagged["malformed"] is True and flagged["verdict"] is None
    # gate discipline: report-only by default, blocking only under --gate
    gated = _run(portfolio, "kill-review", "b1", "--as-of", "2026-08-02",
                 "--gate", env_extra=env)
    assert gated.returncode == 1


def test_kill_review_no_downgrade_when_distribution_attempted(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _run(portfolio, "spend", "b1", "--rep", "--note", "20 cold emails")
    _evaluate(portfolio, tmp_path, results={"paid_conversions": 1})
    env = _stub(tmp_path, {"codex": f"```json\n{KILL_JSON}\n```",
                           "opus": f"```json\n{HOLD_JSON}\n```"})
    proc = _run(portfolio, "kill-review", "b1", "--as-of", "2026-08-02", env_extra=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "codex: KILL" in proc.stdout
    assert "downgraded" not in proc.stdout
    rec = [r for r in _journal(portfolio) if r["event"] == "kill-review"][-1]
    assert rec["details"]["fairness_downgraded"] == []


def test_kill_review_no_downgrade_when_only_non_demand_criterion_fired(portfolio, tmp_path):
    # KC-2 (has_not: refund spike) fires on occurring evidence — no marketing
    # gap explains it, so an unattempted distribution must NOT soften the kill.
    _create(portfolio, tmp_path)
    _evaluate(portfolio, tmp_path,
              results={"refund_rate_pct": 45, "paid_conversions": 5},
              as_of="2026-06-01")
    env = _stub(tmp_path, {"codex": f"```json\n{KILL_JSON}\n```",
                           "opus": f"```json\n{HOLD_JSON}\n```"})
    proc = _run(portfolio, "kill-review", "b1", "--as-of", "2026-06-01", env_extra=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "codex: KILL" in proc.stdout
    assert "downgraded" not in proc.stdout


def test_kill_review_quorum_failure_journals_nothing(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    _evaluate(portfolio, tmp_path, results={"paid_conversions": 1})
    env = _stub(tmp_path, {}, rc=1)
    proc = _run(portfolio, "kill-review", "b1", "--as-of", "2026-08-02", env_extra=env)
    assert proc.returncode == 1
    assert "quorum FAILED" in proc.stdout
    assert not [r for r in _journal(portfolio) if r["event"] == "kill-review"]


def test_kill_review_refuses_on_goalpost_tamper(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    crit_path = portfolio / "bets" / "b1" / "kill-criteria.json"
    doc = json.loads(crit_path.read_text())
    doc["criteria"][0]["threshold"] = 1
    crit_path.write_text(json.dumps(doc))
    env = _stub(tmp_path, {"codex": f"```json\n{HOLD_JSON}\n```",
                           "opus": f"```json\n{HOLD_JSON}\n```"})
    proc = _run(portfolio, "kill-review", "b1", env_extra=env)
    assert proc.returncode == 1
    assert "purity self-check" in proc.stdout
    assert not [r for r in _journal(portfolio) if r["event"] == "kill-review"]


# ---- trigger point ---------------------------------------------------------------


def test_evaluate_recommends_kill_review_on_trigger(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    proc = _evaluate(portfolio, tmp_path, results={"paid_conversions": 1})
    assert "bet.py kill-review b1" in proc.stdout
    # nothing runs the quorum automatically — no kill-review journal event
    assert not [r for r in _journal(portfolio) if r["event"] == "kill-review"]
    # the journaled proposal carries the evaluation rows the brief will cite
    kp = [r for r in _journal(portfolio) if r["event"] == "kill-proposed"][-1]
    rows = {r["id"]: r for r in kp["details"]["rows"]}
    assert rows["KC-1"]["measured"] == 1 and rows["KC-1"]["status"] == "triggered"


def test_evaluate_without_trigger_does_not_recommend_review(portfolio, tmp_path):
    _create(portfolio, tmp_path)
    proc = _evaluate(portfolio, tmp_path,
                     results={"paid_conversions": 5}, as_of="2026-06-01")
    assert "kill-review" not in proc.stdout


# ---- verdict parsing ------------------------------------------------------------


def test_parse_verdict_last_valid_fenced_block_wins():
    text = (
        "thinking...\n```json\n{\"verdict\": \"go\"}\n```\n"
        "on reflection:\n```json\n"
        "{\"verdict\": \"recycle\", \"confidence\": 0.8, \"reasons\": [\"cheaper reframe\"]}\n"
        "```\n"
    )
    parsed = betlib.parse_verdict(text)
    assert parsed["verdict"] == "recycle"
    assert parsed["confidence"] == 0.8
    assert parsed["reasons"] == ["cheaper reframe"]


def test_parse_verdict_tolerates_junk():
    assert betlib.parse_verdict("no json here") is None
    assert betlib.parse_verdict("```json\n{\"verdict\": \"maybe\"}\n```") is None
    assert betlib.parse_verdict("```json\nnot json\n```") is None
    assert betlib.parse_verdict("") is None
    bare = betlib.parse_verdict('{"verdict": "hold", "cheapest_disconfirming_test": "x"}')
    assert bare["verdict"] == "hold" and bare["cheapest_disconfirming_test"] == "x"


# ---- role + provider configuration ----------------------------------------------


def test_kill_review_role_configured_with_charters():
    config = providers_mod.load_config()
    errors = providers_mod.validate_config(
        config, supported_tools=set(consult_ai.TOOLS),
        supported_delegates={"claude-interactive"},
    )
    lenses = providers_mod.load_lenses()
    errors += providers_mod.validate_lenses(config, lenses)
    assert errors == []
    plan = providers_mod.plan_role("kill-review", config)
    assert plan.ok
    assert [p.name for p in plan.required] == ["codex", "opus"]
    assert [p.name for p in plan.optional] == ["claude-interactive"]
    role = plan.role
    assert role.optional_timeout_seconds == 300
    assert role.lenses == {
        "codex": "evidence-sufficiency",
        "opus": "steelman-the-kill",
        "claude-interactive": "is-recycle-better",
    }
    # lenses-not-personas: bounded charters exist for every mapped lens, and the
    # steelman charter carries the amendment's attempt-table-first instruction
    for lens_name in role.lenses.values():
        assert lenses[lens_name]["goal"]
        assert lenses[lens_name]["exclusions"]
    assert "distribution-attempt table" in lenses["steelman-the-kill"]["goal"]


def test_opus_provider_pins_claude_tool_to_opus_model(monkeypatch):
    config = providers_mod.load_config()
    opus = providers_mod.providers_from_config(config)["opus"]
    assert opus.type == "tool" and opus.tool == "claude"
    assert opus.model == "claude-opus-4-6"
    seen = {}

    def fake_claude(prompt, model=None, cwd=None):
        seen["model"] = model
        return "ok", consult_ai.Usage(usage_status="unavailable")

    monkeypatch.setitem(consult_ai.TOOLS, "claude", fake_claude)
    text, usage = consult_ai.consult_provider(opus, "prompt body", None, None)
    assert text == "ok"
    assert usage.usage_status == "unavailable"
    assert seen["model"] == "claude-opus-4-6"
    # an explicit --model override still wins
    consult_ai.consult_provider(opus, "prompt body", "claude-opus-4-7", None)
    assert seen["model"] == "claude-opus-4-7"
