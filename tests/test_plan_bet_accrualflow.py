"""chief-wiggum#238 validation: re-author a REAL grounded bet through /plan-bet.

The ticket was parked until Track A (#235, bet.py) and Track C (#236,
assumption.py) had run against >=1 real bet — that trigger fired, and this
test is the "grounded bet re-authored through the stage" deliverable it asks
for. `tests/fixtures/plan_bet/accrualflow/` holds a structurally-faithful,
redacted copy of the operator's real `accrualflow` bet
(`~/.chief-wiggum/portfolio/bets/accrualflow/`, state `parked`) — see that
directory's README for exactly what is verbatim vs. redacted and why. This
test writes NOTHING into the real portfolio; everything runs against a
tmp_path portfolio built FROM the fixture files.

Two things are asserted, and both are signal, not embarrassment:

1. The real bet's own assumption ledger (ASM-001..004, all still
   untested/testing) joins cleanly against the canvas/vpc field ids this
   ticket's schema fixes — proving the depends_on_element grounding claim in
   the schema's own description, not just asserting it in prose.
2. The real bet's `business-model.json` (faithfully omitting fermi_inputs,
   because the real bet was parked before that arithmetic was ever run)
   reports the Fermi check as `skipped:` — a genuine finding: this bet was
   never put through the cheapest failing test in the whole literature
   before it accumulated spend. The illustrative variant then demonstrates
   what the gate says once numbers (two of them real pre-registered
   thresholds, two operator estimates — see that file's $comment) are
   supplied: an unconditional BLOCK, without --gate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

PLAN_BET = str(SCRIPTS / "plan_bet.py")
BET = str(SCRIPTS / "bet.py")
ASSUME = str(SCRIPTS / "assumption.py")

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan_bet" / "accrualflow"
BET_ID = "accrualflow"


@pytest.fixture
def portfolio(tmp_path):
    return tmp_path / "portfolio"


def _run(script: str, portfolio: Path, *argv: str):
    env = dict(os.environ)
    env["CHIEF_WIGGUM_PORTFOLIO"] = str(portfolio)
    return subprocess.run(
        [sys.executable, script, *argv, "--portfolio-dir", str(portfolio)],
        capture_output=True, text=True, env=env,
    )


def _bet(portfolio, *argv):
    return _run(BET, portfolio, *argv)


def _plan(portfolio, *argv):
    return _run(PLAN_BET, portfolio, *argv)


def _asm(portfolio, *argv):
    return _run(ASSUME, portfolio, *argv)


def _create_accrualflow(portfolio: Path, tmp_path: Path) -> None:
    """Recreate the real bet record from the fixture's envelope/criteria/
    thesis/acquisition — the same shape `bet.py create` requires (envelope +
    criteria as separate files), sourced from the fixture's bet.json/
    kill-criteria.json."""
    real_bet = json.loads((FIXTURE / "bet.json").read_text())
    env_p = tmp_path / "envelope.json"
    crit_p = tmp_path / "criteria.json"
    env_p.write_text(json.dumps(real_bet["envelope"]))
    crit_p.write_text((FIXTURE / "kill-criteria.json").read_text())
    argv = [
        "create", BET_ID,
        "--title", real_bet["title"],
        "--thesis", real_bet["thesis"],
        "--envelope", str(env_p),
        "--criteria", str(crit_p),
    ]
    acq = real_bet.get("acquisition") or {}
    if acq.get("ecosystem_channel"):
        argv += ["--ecosystem-channel", acq["ecosystem_channel"]]
    if acq.get("owned_audience"):
        argv += ["--owned-audience", acq["owned_audience"]]
    proc = _bet(portfolio, *argv)
    assert proc.returncode == 0, proc.stderr


def _recreate_assumptions_via_assumption_py(portfolio: Path) -> None:
    """Re-derive the real assumption ledger through `assumption.py add` (not
    a raw file copy) so the journal chain is real, matching how the ledger
    was actually built."""
    real = json.loads((FIXTURE / "assumptions.json").read_text())["assumptions"]
    for a in sorted(real, key=lambda a: a["id"]):
        proc = _asm(
            portfolio, "add", BET_ID,
            "--statement", a["statement"],
            "--source", a["source"],
            "--element", a["depends_on_element"],
        )
        assert proc.returncode == 0, proc.stderr
    # `assumption.py add` assigns ids in insertion order (ASM-001, ASM-002,
    # ...) matching the real ledger's own ids exactly since we inserted in
    # sorted id order above — assert that rather than assume it.
    ledger = json.loads((portfolio / "bets" / BET_ID / "assumptions.json").read_text())
    assert {a["id"] for a in ledger["assumptions"]} == {a["id"] for a in real}


def test_real_assumption_ledger_joins_cleanly_against_the_schema(portfolio, tmp_path):
    """AC: the real accrualflow ASM ledger's depends_on_element values
    (value-proposition, customer-segment, pricing, competitive-position) —
    mined BEFORE this ticket's schema was written — resolve against the
    canvas field ids this ticket defines, with zero join findings. This is
    the concrete evidence for the schema's own grounding claim."""
    _create_accrualflow(portfolio, tmp_path)
    _recreate_assumptions_via_assumption_py(portfolio)

    bm_file = str(FIXTURE / "business-model.json")
    proc = _plan(portfolio, "author", BET_ID, "--file", bm_file)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "does not match any business-model.json" not in proc.stdout
    assert "no such assumption" not in proc.stdout


def test_real_bet_never_ran_the_fermi_gate(portfolio, tmp_path):
    """AC: re-authoring the REAL (unmodified) accrualflow business model —
    which never declared fermi_inputs, because the bet was parked before
    that arithmetic was run — reports `skipped:`, never a silent pass and
    never a block. This is a genuine finding about the real bet's history,
    not a defect in the check."""
    _create_accrualflow(portfolio, tmp_path)
    _recreate_assumptions_via_assumption_py(portfolio)

    bm_file = str(FIXTURE / "business-model.json")
    proc = _plan(portfolio, "author", BET_ID, "--file", bm_file)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "skipped: fermi_inputs not declared" in proc.stdout
    assert "fermi: ARITHMETIC IMPOSSIBILITY" not in proc.stdout

    check = _plan(portfolio, "check", BET_ID)
    assert check.returncode == 0
    assert "outcome=pass" in check.stdout, (
        "the real bet's ledger has every ASM still untested/testing (never "
        "validated/falsified without a card verdict), premortem is fully "
        "covered (mapped or waived-with-reason), VPC is bipartite-complete, "
        "and the model declares only 2 actors (e3-value inapplicable) — the "
        "faithfully re-authored real record should be clean"
    )


def test_illustrative_fermi_variant_blocks_unconditionally(portfolio, tmp_path):
    """AC: given numbers (two real pre-registered funnel thresholds from
    ASM-002/ASM-003, plus two clearly-flagged operator estimates — see the
    fixture's $comment), the SAME real canvas/premortem/vpc content blocks
    on the Fermi gate WITHOUT --gate. This demonstrates what day-zero
    arithmetic would have said, without claiming the real bet was ever
    evaluated against it."""
    _create_accrualflow(portfolio, tmp_path)
    _recreate_assumptions_via_assumption_py(portfolio)

    bm_file = str(FIXTURE / "business-model-fermi-illustrative.json")
    proc = _plan(portfolio, "author", BET_ID, "--file", bm_file)
    assert proc.returncode == 1, proc.stdout
    assert "fermi: ARITHMETIC IMPOSSIBILITY" in proc.stdout
    assert "BLOCKED" in proc.stdout
    assert not (portfolio / "bets" / BET_ID / "business-model.json").exists()


def test_schema_validates_both_fixture_variants():
    """Belt-and-braces: both committed fixture files are schema-valid on
    their own, independent of the portfolio-journal plumbing above."""
    import jsonschema

    schema = json.loads((SCRIPTS.parent / "templates" / "business-model-schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    for name in ("business-model.json", "business-model-fermi-illustrative.json"):
        bm = json.loads((FIXTURE / name).read_text())
        errors = list(validator.iter_errors(bm))
        assert not errors, f"{name}: {[e.message for e in errors]}"
