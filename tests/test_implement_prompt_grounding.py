"""Pins the /implement prompt's move from hand-rolled consultation to the
configured role quorum, and the removal of prose descriptions of the code
that a worker with repo access reads live instead.

Step 4 used to background two literal `consult_ai.py codex` / `gemini` calls
and hand-check their output — a roster that drifted the moment
`config/providers.json` changed (gemini was disabled while the prompt still
said "ALL THREE (codex, gemini, ...)"). `consult_ai.py --role explorer` is the
one place the roster lives, and its non-zero exit is the quorum gate.

The same pass dropped the intermediate artifacts that only described the
repo (`codebase-context.md`, `approach-opus.md`, a walkthrough-style
implementation plan): the synthesis and implementation workers have the
checkout and `code_query.py`, so a snapshot of it is at best redundant and
at worst stale. These are prompts an agent follows literally, so the only
way to keep the regression out is to read the live prompt text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMMANDS = REPO / ".claude" / "commands"
PROVIDERS = json.loads((REPO / "config" / "providers.json").read_text())


def _text(name: str) -> str:
    return (COMMANDS / name).read_text()


def _step4(text: str) -> str:
    return text.split("### Step 4: Consult AIs on approach")[1].split("### Step 5:")[0]


def test_implement_step4_runs_the_explorer_role_not_named_providers():
    step4 = _step4(_text("implement.md"))
    assert 'consult_ai.py" --role explorer' in step4
    # No provider is named as a literal consult_ai.py tool argument anywhere in
    # Step 4 — the roster is config/providers.json's, not the prompt's.
    for provider in PROVIDERS["providers"]:
        assert not re.search(rf'consult_ai\.py"\s+{re.escape(provider)}\b', step4), (
            f"Step 4 hardcodes provider {provider!r}; the explorer role owns the roster")


def test_implement_step4_treats_the_role_exit_as_the_quorum_gate():
    """The old prompt hand-validated output files ('> 100 bytes', 'must not
    start with Timeout:') and shouted 'ALL THREE'. The role quorum already
    validates output and names the absentee in its manifest."""
    step4 = _step4(_text("implement.md"))
    assert "explorer-manifest.json" in step4
    assert "ALL THREE" not in step4
    assert "100 bytes" not in step4


def test_implement_no_longer_generates_prose_descriptions_of_the_code():
    text = _text("implement.md")
    for artifact in ("codebase-context.md", "approach-opus.md", "approach-codex.md", "approach-gemini.md"):
        assert artifact not in text, f"{artifact} is a description of the code a worker reads live"
    # The plan is decisions with handles, not a walkthrough that replaces exploration.
    assert "no further codebase exploration is needed" not in text
    assert "decisions with handles" in _step4(text)


def test_implement_review_passes_governing_slices_not_prose_when_models_exist():
    """Step 7 feeds reviewers `code_query.py orient` output for the changed
    files when formal models exist; the prose twins are the prose-only
    fallback, never the default."""
    step7 = _text("implement.md").split("### Step 7:")[1].split("### Step 8:")[0]
    assert "orient" in step7
    assert 'Governing contracts=$TICKET_TMP/reviews/governing.md' in step7
    assert "PROSE_ARTIFACTS" in step7


def test_wave_workers_explore_live_instead_of_reading_a_shared_snapshot():
    text = _text("implement-wave.md")
    assert "codebase-context.md" not in text
    assert "wave-$wave_number-codebase-context" not in text


def test_no_command_hardcodes_the_reviewer_or_critic_roster_in_prose():
    """'codex + gemini in parallel' was true once; the role config is the
    only place a roster is allowed to live."""
    for name in ("implement.md", "implement-wave.md", "close-epic.md", "architect.md"):
        text = _text(name)
        assert "codex + gemini" not in text.lower(), f"{name} names a fixed roster"
        assert "Codex + Gemini" not in text, f"{name} names a fixed roster"


def test_wave_preflight_uses_provider_preflight_not_shell_probes():
    text = _text("implement-wave.md")
    assert 'provider_preflight.py" --human --usage' in text
    assert "codex exec --sandbox read-only" not in text
    assert "gemini --yolo" not in text
