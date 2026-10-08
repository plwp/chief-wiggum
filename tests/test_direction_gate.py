"""Tests for scripts/direction_gate.py — stop verdicts and open rulings bind."""

from __future__ import annotations

import argparse
import json
import subprocess

import direction_gate as dg
import pytest


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def _ledger(repo):
    return repo / "docs" / dg.LEDGER_RELPATH


def _check(repo, epic="map-surface", action="plan-epic", probe=None):
    return dg.main(["check", "--repo", str(repo), "--epic", epic, "--action", action]
                   + (["--probe", str(probe)] if probe else []))


def _rule(repo, item_id, answers, tty=True):
    it = iter(answers)
    ns = argparse.Namespace(repo=str(repo), id=item_id)
    return dg.cmd_rule(ns, is_tty=lambda: tty, ask=lambda _prompt: next(it))


GOOD_PROBE = """# Reality probe: map surface

## Success question
Does any cell serve a config that its tier map omits?

## Real-data hand check
Source: `gs://estate-snapshots/2026-09-18/saga.json` and https://github.com/acme/sap/blob/main/app/core/config.py
Method: diffed served cells against the tier map by hand.
Answer: one cell (HabA) is served but missing from the tier map.

## Baseline
Method: grep the two files for cell labels and diff the sets.
Result: the same one discrepancy; the product must beat this.
"""


# ---- stop verdicts bind -------------------------------------------------------


def test_clear_when_nothing_is_recorded(repo):
    assert _check(repo) == 0


def test_stop_verdict_blocks_plan_epic_wave_and_ticket_filing(repo, capsys):
    assert dg.main(["stop", "--repo", str(repo), "--epic", "map-surface",
                    "--verdict", "stop_expanding: no incremental value over baseline",
                    "--source", "evaluation", "--evidence", "docs/evaluation/x.md"]) == 0
    for action in ("plan-epic", "file-ticket", "wave", "architect"):
        assert _check(repo, action=action) == 1
    err = capsys.readouterr().err
    assert "STOP-1" in err and "stop_expanding" in err and "rule --repo" in err


def test_a_stop_on_one_epic_does_not_block_another(repo):
    dg.main(["stop", "--repo", str(repo), "--epic", "other", "--verdict", "v",
             "--source", "council"])
    assert _check(repo, epic="map-surface") == 0


def test_product_stop_blocks_every_epic(repo):
    dg.main(["stop", "--repo", str(repo), "--product", "--verdict", "narrow it",
             "--source", "council"])
    assert _check(repo, epic="anything") == 1


# ---- silence is not a yes -----------------------------------------------------


def test_open_question_blocks_and_is_named(repo, capsys):
    dg.main(["ask", "--repo", str(repo), "--epic", "map-surface",
             "--question", "Freeze Map Surface and make #117 the only milestone?"])
    assert _check(repo) == 1
    err = capsys.readouterr().err
    assert "OPEN QUESTION" in err and "Freeze Map Surface" in err and "Q-1" in err


# ---- only the operator can rule -----------------------------------------------


def test_rule_refuses_without_a_terminal_and_records_nothing(repo, capsys):
    dg.main(["ask", "--repo", str(repo), "--epic", "map-surface", "--question", "q?"])
    before = _ledger(repo).read_text()
    assert _rule(repo, "Q-1", ["Q-1", "continue", "go"], tty=False) == 1
    assert "operator-only" in capsys.readouterr().err
    assert _ledger(repo).read_text() == before
    assert _check(repo) == 1


def test_rule_via_cli_without_a_terminal_is_refused(repo):
    """The real entrypoint, as an agent's Bash tool would run it: no TTY."""
    dg.main(["ask", "--repo", str(repo), "--epic", "map-surface", "--question", "q?"])
    proc = subprocess.run(
        ["python3", str(dg.Path(dg.__file__)), "rule", "--repo", str(repo), "--id", "Q-1"],
        input="Q-1\ncontinue\nbecause\n", capture_output=True, text=True)
    assert proc.returncode == 1 and "REFUSED" in proc.stderr
    assert _check(repo) == 1


def test_operator_continue_clears_the_gate(repo):
    dg.main(["stop", "--repo", str(repo), "--epic", "map-surface", "--verdict", "v",
             "--source", "evaluation"])
    assert _rule(repo, "STOP-1", ["STOP-1", "continue", "new data says expand"]) == 0
    assert _check(repo) == 0
    last = json.loads(_ledger(repo).read_text().splitlines()[-1])
    assert last["kind"] == "ruling" and last["via"] == "tty" and last["decision"] == "continue"


def test_operator_stop_ruling_keeps_it_blocked(repo, capsys):
    dg.main(["ask", "--repo", str(repo), "--epic", "map-surface", "--question", "q?"])
    assert _rule(repo, "Q-1", ["Q-1", "stop", "shelve it"]) == 0
    assert _check(repo) == 1
    assert "STOP CONFIRMED" in capsys.readouterr().err


@pytest.mark.parametrize("answers", [
    ["Q-2", "continue", "x"],        # mistyped id
    ["Q-1", "keep going", "x"],      # a generic "go" is not a decision
    ["Q-1", "continue", ""],         # no rationale
])
def test_rule_rejects_anything_but_an_explicit_ruling(repo, answers):
    dg.main(["ask", "--repo", str(repo), "--epic", "map-surface", "--question", "q?"])
    assert _rule(repo, "Q-1", answers) == 1
    assert _check(repo) == 1


def test_hand_appended_ruling_breaks_the_chain_and_fails_closed(repo, capsys):
    """Filling in a ruling record by hand is exactly what must not work."""
    dg.main(["ask", "--repo", str(repo), "--epic", "map-surface", "--question", "q?"])
    with _ledger(repo).open("a") as fh:
        fh.write(json.dumps({"id": "R-1", "kind": "ruling", "ref": "Q-1",
                             "decision": "continue", "via": "tty", "prev": "0" * 64}) + "\n")
    assert _check(repo) == 1
    assert "hash chain broken" in capsys.readouterr().err


def test_deleting_a_stop_line_breaks_the_chain(repo):
    dg.main(["stop", "--repo", str(repo), "--epic", "map-surface", "--verdict", "v",
             "--source", "evaluation"])
    dg.main(["ask", "--repo", str(repo), "--epic", "other", "--question", "q?"])
    lines = _ledger(repo).read_text().splitlines()
    _ledger(repo).write_text(lines[1] + "\n")
    assert _check(repo) == 1


# ---- reality probe ------------------------------------------------------------


def test_wave_and_architect_need_a_reality_probe(repo, capsys):
    assert _check(repo, action="wave") == 1
    assert "no reality probe" in capsys.readouterr().err
    assert _check(repo, action="architect") == 1
    assert _check(repo, action="plan-epic") == 0


def test_a_filled_probe_clears_wave(repo, tmp_path):
    probe = repo / "docs" / "epics" / "map-surface" / dg.PROBE_NAME
    probe.parent.mkdir(parents=True)
    probe.write_text(GOOD_PROBE)
    assert _check(repo, action="wave") == 0
    assert dg.main(["probe-check", str(probe)]) == 0


def test_the_unfilled_template_is_rejected():
    template = dg.Path(dg.__file__).resolve().parent.parent / "templates" / "reality-probe.md"
    problems = dg.probe_problems(template)
    assert any("placeholder" in p for p in problems)


@pytest.mark.parametrize("mutate, why", [
    (lambda t: t.replace("`gs://estate-snapshots/2026-09-18/saga.json` and "
                         "https://github.com/acme/sap/blob/main/app/core/config.py",
                         "`tests/fixtures/estate.json`"), "REAL data"),
    (lambda t: t.replace("Answer: one cell (HabA) is served but missing from the tier map.",
                         ""), "Answer"),
    (lambda t: t.replace("Result: the same one discrepancy; the product must beat this.",
                         ""), "Result"),
    (lambda t: t.replace("## Baseline", "## Notes"), "Baseline"),
    (lambda t: t.replace("Does any cell", "{question} Does any cell"), "placeholder"),
])
def test_probe_content_check_bites(tmp_path, mutate, why):
    probe = tmp_path / "probe.md"
    probe.write_text(mutate(GOOD_PROBE))
    problems = dg.probe_problems(probe)
    assert problems and any(why.lower() in p.lower() for p in problems), problems


# ---- the workflows actually call the gate --------------------------------------

COMMANDS = dg.Path(dg.__file__).resolve().parent.parent / ".claude" / "commands"


@pytest.mark.parametrize("command, action", [
    ("implement-wave", "--action wave"),
    ("plan-epic", "--action plan-epic"),
    ("architect", "--action architect"),
    ("seed", "--action file-ticket"),
    ("create-issue", "--action file-ticket"),
])
def test_workflow_runs_the_direction_gate(command, action):
    text = (COMMANDS / f"{command}.md").read_text()
    assert "scripts/direction_gate.py\" check" in text and action in text


def test_implement_wave_says_a_generic_go_is_not_a_ruling():
    text = (COMMANDS / "implement-wave.md").read_text()
    assert "Silence is not a yes, and neither is a generic go" in text
    assert "Before launching a wave**, re-run the direction gate" in text


def test_seed_and_architect_write_the_reality_probe_before_contracts():
    seed = (COMMANDS / "seed.md").read_text()
    assert seed.index("probe-check") < seed.index("### Step 3: Interactive architecture")
    arch = (COMMANDS / "architect.md").read_text()
    assert arch.index("--action architect") < arch.index("### Step 4: Synthesise")


def test_workers_implement_and_hand_back_early_the_orchestrator_reviews():
    text = (COMMANDS / "implement-wave.md").read_text()
    assert "Hand back by ~70% of budget" in text
    assert "Removal probes" in text
    assert "Reviewer quorum (orchestrator-run)" in text
