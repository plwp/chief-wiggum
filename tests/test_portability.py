"""Portability conformance tests (#24).

The workflow prompts must keep worker launches harness-neutral: Claude-only
execution params only as adapter notes *bound to a worker contract*, referenced
anchors exist, and worker contracts are complete.
"""

from __future__ import annotations

from pathlib import Path

import check_portability as cp

REPO = Path(__file__).resolve().parents[1]

# A complete contract doc body for synthetic repos (labeled-field format).
GOOD_DOC = (
    "### implementation-worker\n"
    "- **Role**: x\n- **Inputs**: x\n- **Output artifact paths**: x\n"
    "- **Write scope**: x\n- **Isolation**: x\n- **Stop condition**: x\n"
)


def _repo(tmp_path, command_text, doc=GOOD_DOC, name="x.md"):
    cmds = tmp_path / ".claude" / "commands"
    cmds.mkdir(parents=True)
    (cmds / name).write_text(command_text)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "worker-contracts.md").write_text(doc)
    return tmp_path


# --- live prompts ------------------------------------------------------------


def test_commands_are_harness_neutral():
    violations = cp.check_repo(REPO)
    assert violations == [], "\n".join(str(v) for v in violations)


# --- marker detection (broadened) -------------------------------------------


def test_bare_marker_is_a_violation(tmp_path):
    repo = _repo(tmp_path, 'Launch a worker (`subagent_type: "general-purpose"`).\n')
    assert any("outside an adapter note" in v.detail for v in cp.check_repo(repo))


def test_generic_sub_agent_is_a_marker(tmp_path):
    repo = _repo(tmp_path, "The sub-agent should do the thing.\n")
    assert any("sub-agent" in v.detail for v in cp.check_repo(repo))


def test_model_tier_without_quotes_is_a_marker(tmp_path):
    repo = _repo(tmp_path, "Run with model: opus please.\n")
    assert any("model tier" in v.detail for v in cp.check_repo(repo))


# --- launder guard: adapter tag alone is NOT enough -------------------------


def test_adapter_tag_without_contract_anchor_is_rejected(tmp_path):
    # Has the adapter tag but no worker-contracts.md#anchor nearby -> launder guard.
    repo = _repo(
        tmp_path,
        'Claude Code adapter: `subagent_type: "general-purpose"`, `model: "sonnet"`.\n',
    )
    assert any("launder guard" in v.detail for v in cp.check_repo(repo))


def test_bound_adapter_note_passes(tmp_path):
    repo = _repo(
        tmp_path,
        "Launch an implementation worker (contract: `docs/worker-contracts.md#implementation-worker`). "
        '*Claude Code adapter:* `subagent_type: "general-purpose"`, `model: "sonnet"`.\n',
        name="implement.md",
    )
    assert cp.check_repo(repo) == []


# --- anchor existence + contract completeness -------------------------------


def test_referenced_anchor_must_exist(tmp_path):
    repo = _repo(
        tmp_path,
        "Launch a worker (contract: `docs/worker-contracts.md#ghost-worker`). "
        "*Claude Code adapter:* `subagent_type: x`.\n",
        name="implement.md",
    )
    assert any("anchor #ghost-worker not defined" in v.detail for v in cp.check_repo(repo))


def test_incomplete_contract_section_flagged(tmp_path):
    bad_doc = "### implementation-worker\n- **Role**: x\n- **Inputs**: x\n"  # missing fields
    repo = _repo(
        tmp_path,
        "Launch a worker (contract: `docs/worker-contracts.md#implementation-worker`). "
        "*Claude Code adapter:* `subagent_type: x`.\n",
        doc=bad_doc, name="implement.md",
    )
    assert any("missing labeled fields" in v.detail for v in cp.check_repo(repo))


def test_worker_command_must_reference_a_contract_anchor(tmp_path):
    # A real command name with a marker but no anchor at all.
    repo = _repo(
        tmp_path,
        "Claude Code adapter: `subagent_type: x`.\n",
        name="architect.md",
    )
    violations = cp.check_repo(repo)
    assert any("does not reference" in v.detail for v in violations)


def test_missing_contract_doc_is_a_violation(tmp_path):
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    assert any("missing" in v.detail for v in cp.check_repo(tmp_path))


def test_exempt_docs_may_name_markers(tmp_path):
    repo = _repo(tmp_path, "irrelevant\n")
    cmds = repo / ".claude" / "commands"
    (cmds / "harness-adapters.md").write_text("subagent_type sub-agent model: opus everywhere\n")
    (cmds / "keep-going.md").write_text("CronCreate and sub-agent semantics\n")
    assert cp.check_repo(repo) == []
