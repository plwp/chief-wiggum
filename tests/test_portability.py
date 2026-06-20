"""Portability conformance tests (#24).

The workflow prompts must keep worker launches harness-neutral: Claude-only
execution params only as adapter notes, and worker-launching commands point at
the worker-contract reference.
"""

from __future__ import annotations

from pathlib import Path

import check_portability as cp

REPO = Path(__file__).resolve().parents[1]


def test_commands_are_harness_neutral():
    """The live command prompts must have zero portability violations."""
    violations = cp.check_repo(REPO)
    assert violations == [], "\n".join(str(v) for v in violations)


# --- checker unit behavior (synthetic inputs) -------------------------------


def test_bare_marker_is_a_violation(tmp_path):
    cmds = tmp_path / ".claude" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "x.md").write_text('Launch a sub-agent (`subagent_type: "general-purpose"`).\n')
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "worker-contracts.md").write_text(
        "role inputs outputs write scope isolation stop"
    )
    violations = cp.check_repo(tmp_path)
    assert any("subagent_type" in v.detail for v in violations)


def test_adapter_tagged_marker_is_allowed(tmp_path):
    cmds = tmp_path / ".claude" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "x.md").write_text(
        'Launch a worker. See worker-contracts.md.\n'
        'Claude Code adapter: `subagent_type: "general-purpose"`, `model: "sonnet"`.\n'
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "worker-contracts.md").write_text(
        "role inputs outputs write scope isolation stop"
    )
    # x.md is not a worker-launching *command name*, so layer-2 doesn't apply.
    assert cp.check_repo(tmp_path) == []


def test_worker_command_must_reference_contract(tmp_path):
    cmds = tmp_path / ".claude" / "commands"
    cmds.mkdir(parents=True)
    # A real worker-launching command name, with a tagged marker but no contract ref.
    (cmds / "implement.md").write_text(
        'Claude Code adapter: `subagent_type: "general-purpose"`.\n'
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "worker-contracts.md").write_text(
        "role inputs outputs write scope isolation stop"
    )
    violations = cp.check_repo(tmp_path)
    assert any("worker-contracts.md" in v.detail for v in violations)


def test_missing_contract_doc_is_a_violation(tmp_path):
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    violations = cp.check_repo(tmp_path)
    assert any("missing" in v.detail for v in violations)


def test_contract_doc_missing_fields_flagged(tmp_path):
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "worker-contracts.md").write_text("role and inputs only")
    violations = cp.check_repo(tmp_path)
    assert any("required contract field" in v.detail for v in violations)


def test_exempt_docs_may_name_markers(tmp_path):
    cmds = tmp_path / ".claude" / "commands"
    cmds.mkdir(parents=True)
    # harness-adapters.md is exempt even under .claude/commands (defensive).
    (cmds / "harness-adapters.md").write_text('subagent_type everywhere\n')
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "worker-contracts.md").write_text(
        "role inputs outputs write scope isolation stop"
    )
    assert cp.check_repo(tmp_path) == []
