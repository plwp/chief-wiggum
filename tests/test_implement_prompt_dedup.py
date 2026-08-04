"""Pins the mechanical dedups in the /implement workflow prompt
(chief-wiggum#324): the prompt is followed literally by an agent, so the only
way to prove "runs once, not twice" is to count the invocations in the live
prompt text — a dedup with no test proving the second run is gone can
silently regress the next time someone edits the file.

``.claude/commands/*.md`` are the source of truth; ``skills/chief-wiggum/
references/workflows/*.md`` are SYMLINKS to them (never a separate copy), so
reading one file exercises both.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMMANDS = REPO / ".claude" / "commands"


def _text(name: str) -> str:
    return (COMMANDS / name).read_text()


# --- symlinks stay symlinks, never copies -----------------------------------


def test_workflow_symlinks_point_at_commands():
    workflows = REPO / "skills" / "chief-wiggum" / "references" / "workflows"
    for name in ("implement.md", "implement-wave.md", "close-epic.md"):
        link = workflows / name
        assert link.is_symlink(), f"{link} must be a symlink, never a copy"
        assert link.resolve() == (COMMANDS / name).resolve()


# --- #324 item 1: verify.json / verification.json filename bug -------------


def test_implement_verification_writer_and_reader_agree_on_filename():
    """Step 8.4 writes `$TICKET_TMP/verify.json`; Step 11's draft_pr.py call
    must read that SAME file — not a `verification.json` nobody writes."""
    text = _text("implement.md")
    assert 'run_verification.py" --repo "$(git rev-parse --show-toplevel)" --profile test,lint,build --json > "$TICKET_TMP/verify.json"' in text
    assert '--verification "$TICKET_TMP/verify.json"' in text
    # The old mismatched filename must not reappear anywhere in the file.
    assert "verification.json" not in text


# --- #324 item 2: the unresolved-marker scan runs once in Step 1 -----------


def test_implement_step1_reuses_inventory_unresolved_scan():
    """`epic_inventory.py` already runs `check_unresolved.scan` internally
    and writes `.unresolved`/`.blocked_tickets` into inventory.json — Step 1
    must read that field, not shell out to check_unresolved.py a second time
    over the same $EPIC_DIR with zero intervening writes. The ONE remaining
    mention is the documented fallback for when inventory.json itself is
    unavailable."""
    text = _text("implement.md")
    assert text.count("check_unresolved.py") == 1, (
        "check_unresolved.py must appear exactly once in implement.md — as "
        "the documented fallback, not a routine second scan")
    assert 'jq \'.unresolved\' "$TICKET_TMP/inventory.json"' in text
    gate_section = text.split("Unresolved-unknowns gate")[1].split("### Step 2")[0]
    assert "Fallback" in gate_section
    assert "check_unresolved.py" in gate_section  # the fallback lives IN this section


# --- #324 item 3: verify_transitions.py runs once in Step 8b ---------------


def test_implement_step8b_runs_verify_transitions_once():
    """The old flow ran verify_transitions.py twice (once --format text for
    the human summary, once --format json --output for the transition-map)
    — the SAME comparison, computed twice. One JSON run must feed both the
    written map and the locally-rendered summary."""
    text = _text("implement.md")
    assert text.count("verify_transitions.py") == 1, (
        f"verify_transitions.py must be invoked exactly once in Step 8b, "
        f"found {text.count('verify_transitions.py')}")
    assert "--format json --output" in text
    # Only one fenced ```bash block in this section may invoke the script —
    # a second code block re-running it (the old --format text pass) is the
    # regression this test guards against. (Prose ABOVE the code block is
    # allowed to reference the old `--format text` run when explaining why
    # it was removed — that is not a second invocation.)
    section = text.split("Transition-map verification")[1].split('9. **Quality check**')[0]
    bash_blocks = section.count("```bash")
    assert bash_blocks == 2, (
        f"expected exactly two ```bash blocks in 8b (the single verify_transitions "
        f"invocation + the local jq render), found {bash_blocks}")


# --- #324 item 4: QUALITY_DIR/meta root resolved once, reused --------------


def test_no_redundant_artifacts_show_invocations():
    """`artifacts.py show` re-resolving quality_dir/meta_root ~10x across the
    three workflow files is exactly the redundancy #324 flags — Step 1's
    `workflow_context.py` now exports QUALITY_DIR/CW_META_ROOT/CW_META_MODE
    once per session (see chief_wiggum/context.py), so no downstream step in
    these three files should re-invoke `artifacts.py show` for the same
    answer against the same unchanged target."""
    for name in ("implement.md", "implement-wave.md", "close-epic.md"):
        text = _text(name)
        assert 'artifacts.py" show' not in text, (
            f"{name} still re-invokes `artifacts.py show` — QUALITY_DIR/"
            "CW_META_ROOT/CW_META_MODE should be reused from Step 1 instead")


def test_implement_and_wave_and_close_epic_reuse_quality_dir_var():
    """Every ratchet-gated step references `$QUALITY_DIR` as a plain shell
    variable (Step 1's export), not a freshly-derived one."""
    for name in ("implement.md", "implement-wave.md", "close-epic.md"):
        text = _text(name)
        assert "$QUALITY_DIR" in text


# --- #324 item 5: service teardown moves past Step 10 -----------------------


def test_implement_service_teardown_happens_after_step10():
    """Step 8 used to tear services down (`docker compose down`) only for
    Step 9's UX gate to immediately need them running again. Teardown must
    now occur exactly once, after Step 10 (browser-use validation) and
    before Step 11 (Ship PR) — never inside Step 8's checklist."""
    text = _text("implement.md")
    assert text.count("docker compose down") == 1
    step8_idx = text.index("### Step 8: Apply review fixes and verify")
    step9_idx = text.index("### Step 9: UX sanity")
    step10_idx = text.index("### Step 10: Browser-use validation")
    step11_idx = text.index("### Step 11: Ship PR")
    teardown_idx = text.index("docker compose down")
    assert not (step8_idx < teardown_idx < step9_idx), (
        "teardown must not happen inside Step 8 anymore")
    assert step10_idx < teardown_idx < step11_idx, (
        "teardown must happen after Step 10 and before Step 11")
