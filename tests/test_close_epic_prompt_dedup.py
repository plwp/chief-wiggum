"""Pins the mechanical dedups in the /close-epic workflow prompt
(chief-wiggum#323): /close-epic recomputes what close-epic-manifest.json
already proves, and runs the traceability coverage scan twice back-to-back.
Each of these is a prompt an agent follows literally, so the only way to
prove "runs once, not twice" is to count the invocations in the live prompt
text — a dedup with no test proving the second run is gone can silently
regress the next time someone edits the file. Equally, every reuse pins its
FALLBACK (stale/missing manifest still triggers a real recomputation) so the
dedup can never turn into "trust a stale artifact forever."

``.claude/commands/close-epic.md`` is the source of truth; ``skills/
chief-wiggum/references/workflows/close-epic.md`` is a SYMLINK to it (never a
separate copy) — see test_implement_prompt_dedup.py for the symlink pin
shared across all three workflow files.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMMANDS = REPO / ".claude" / "commands"


def _text(name: str) -> str:
    return (COMMANDS / name).read_text()


def _invocation_count(text: str, script: str) -> int:
    """Count ACTUAL invocations of a script (`scripts/<script>"`) — distinct
    from prose that merely NAMES the script while explaining a flag or a
    fallback path. A raw substring count of the filename would false-positive
    on exactly that prose (see close-epic.md Step 2c2, which legitimately
    mentions `check_gate_validation.py` twice in prose around its one real
    invocation)."""
    return text.count(f'scripts/{script}"')


def test_close_epic_manifest_carries_target_sha():
    """The freshness check every reuse below depends on: the manifest records
    the target's HEAD at audit time (close_epic_audit.py's CloseEpicManifest
    .target_sha, #323), and Step 1b's consumer list says so."""
    text = _text("close-epic.md")
    assert "target_sha" in text
    consumers = text.split("Steps 2 (traceability)")[1].split("### Step 2:")[0]
    assert "3 (integration tests" in consumers, (
        "Step 3 must be added to the manifest-consumers list — #323's whole "
        "point is that it was the most expensive duplicate and wasn't even "
        "listed as a consumer")


def test_close_epic_step2b_reuses_manifest_transitions_with_fallback():
    """Step 2b must consume the manifest's `.transitions` when target_sha is
    fresh, and only re-run verify_transitions.py when it isn't (or the
    manifest is missing) — never unconditionally re-scan."""
    text = _text("close-epic.md")
    section = text.split("### Step 2b: Transition-map audit")[1].split("### Step 2c:")[0]
    assert "target_sha" in section
    assert "jq '.transitions'" in section
    assert _invocation_count(section, "verify_transitions.py") == 1, (
        "verify_transitions.py must appear exactly once in 2b — as the "
        "stale/missing-manifest fallback, not the default path")


def test_close_epic_step2c_reuses_manifest_unresolved_with_fallback():
    """Step 2c must consume the manifest's `.unresolved` when fresh, falling
    back to check_unresolved.py only when it isn't."""
    text = _text("close-epic.md")
    section = text.split("### Step 2c: Unresolved-unknowns audit")[1].split("### Step 2c2:")[0]
    assert "target_sha" in section
    assert ".unresolved[]" in section
    assert _invocation_count(section, "check_unresolved.py") == 1, (
        "check_unresolved.py must appear exactly once in 2c — as the "
        "stale/missing-manifest fallback, not the default path")


def test_close_epic_step2c2_checks_all_gates_in_one_invocation():
    """Step 2c2 must invoke check_gate_validation.py ONCE for all three
    gates (check_traceability, check_single_writer, ratchet) — not three
    separate processes each re-walking the shared ratchet journal chain."""
    text = _text("close-epic.md")
    section = text.split("### Step 2c2:")[1].split("### Step 2d:")[0]
    assert _invocation_count(section, "check_gate_validation.py") == 1, (
        f"expected exactly one check_gate_validation.py invocation in 2c2, "
        f"found {_invocation_count(section, 'check_gate_validation.py')}")
    invocation = section.split("```bash")[1].split("```")[0]
    for gate in ("check_traceability", "check_single_writer", "ratchet"):
        assert gate in invocation


def test_close_epic_step2d_collapses_traceability_scan_into_one_call():
    """Step 2d must run check_traceability.py exactly ONCE, with BOTH
    --gate coverage and --write-links on the same invocation — not a plain
    coverage run followed by a second full annotation scan just to add
    --write-links (--write-links is documented as a no-op when the gate
    doesn't pass, so it always safely rides the same run)."""
    text = _text("close-epic.md")
    section = text.split("### Step 2d:")[1].split("### Step 2e:")[0]
    assert _invocation_count(section, "check_traceability.py") == 1, (
        f"expected exactly one check_traceability.py invocation in 2d, "
        f"found {_invocation_count(section, 'check_traceability.py')}")
    invocation = section.split("```bash")[1].split("```")[0]
    assert "--gate coverage" in invocation
    assert "--write-links" in invocation


def test_close_epic_step3_checks_freshness_before_relaunching_integration_tests():
    """Step 3 must check the manifest's target_sha freshness (and whether
    Step 1b's suite run was green) BEFORE deciding to re-run the integration
    tests — not unconditionally launch a fresh verification worker that
    duplicates Step 1b's `ver.verify(repo, ["test"])`."""
    text = _text("close-epic.md")
    section = text.split("### Step 3: Integration test execution")[1].split("### Step 4:")[0]
    assert "target_sha" in section
    assert "SUITE_OK" in section
    assert "FRESH" in section
    # The fallback (genuinely re-running) must still be present and reachable
    # — reuse is conditional, not an unconditional skip.
    assert "Run the full walk below for real" in section
