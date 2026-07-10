"""Tests for check_portability.py — the harness-neutrality conformance gate.

Exercises the pure detection/parsing logic: Claude-only marker detection,
worker-launch binding to contract anchors, contract-section parsing, and the
completeness check for ``*-worker`` sections. Uses synthetic markdown so no
real command files are touched.
"""

from __future__ import annotations

import check_portability as cp

# --- _markers_on ------------------------------------------------------------


def test_markers_on_detects_claude_only_tokens():
    assert "subagent_type" in cp._markers_on("launch with subagent_type: general-purpose")
    assert "run_in_background" in cp._markers_on("set run_in_background true")
    assert cp._markers_on("Opus model") == ["Opus model tier"]


def test_markers_on_clean_line():
    assert cp._markers_on("Delegate to a worker per the contract.") == []


# --- check_command_markers --------------------------------------------------


def _md(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_marker_outside_adapter_note_is_violation(tmp_path):
    p = _md(tmp_path, "cmd.md", "Just run with subagent_type: general-purpose here.\n")
    violations, anchors = cp.check_command_markers(p)
    assert len(violations) == 1
    assert "outside an adapter note" in violations[0].detail
    assert anchors == set()


def test_adapter_note_bound_to_anchor_is_clean(tmp_path):
    text = (
        "See docs/worker-contracts.md#implement-worker for the contract.\n"
        "Claude Code adapter: launch with subagent_type: general-purpose.\n"
    )
    p = _md(tmp_path, "cmd.md", text)
    violations, anchors = cp.check_command_markers(p)
    assert violations == []
    assert anchors == {"implement-worker"}


def test_adapter_note_without_anchor_is_launder_violation(tmp_path):
    text = "Claude Code adapter: launch with subagent_type: general-purpose.\n"
    p = _md(tmp_path, "cmd.md", text)
    violations, _ = cp.check_command_markers(p)
    assert len(violations) == 1
    assert "not bound" in violations[0].detail


def test_unanchored_worker_launch_is_violation(tmp_path):
    # A launch verb + "worker" with no adapter marker still must bind a contract.
    p = _md(tmp_path, "cmd.md", "Launch a review worker to check the diff.\n")
    violations, _ = cp.check_command_markers(p)
    assert len(violations) == 1
    assert "not bound to a worker-contracts.md#<anchor>" in violations[0].detail


def test_anchored_worker_launch_is_clean(tmp_path):
    text = (
        "Per docs/worker-contracts.md#review-worker:\n"
        "Launch a review worker to check the diff.\n"
    )
    p = _md(tmp_path, "cmd.md", text)
    violations, anchors = cp.check_command_markers(p)
    assert violations == []
    assert "review-worker" in anchors


# --- check_contract_reference -----------------------------------------------


def test_contract_reference_required_for_worker_commands(tmp_path):
    p = _md(tmp_path, "implement.md", "Do stuff without any contract link.\n")
    violations = cp.check_contract_reference(p)
    assert len(violations) == 1
    assert "does not reference" in violations[0].detail


def test_contract_reference_satisfied_when_anchor_present(tmp_path):
    p = _md(tmp_path, "implement.md", "See docs/worker-contracts.md#implement-worker.\n")
    assert cp.check_contract_reference(p) == []


def test_contract_reference_ignored_for_non_worker_commands(tmp_path):
    p = _md(tmp_path, "setup.md", "No contract needed here.\n")
    assert cp.check_contract_reference(p) == []


# --- parse_contract_sections ------------------------------------------------


def test_parse_contract_sections_splits_by_heading():
    doc = (
        "# Worker Contracts\n"
        "### implement-worker\n"
        "body A\n"
        "### review-worker\n"
        "body B\n"
    )
    sections = cp.parse_contract_sections(doc)
    assert set(sections.keys()) == {"implement-worker", "review-worker"}
    assert "body A" in sections["implement-worker"]
    assert "body B" in sections["review-worker"]


# --- check_contract_doc -----------------------------------------------------


_COMPLETE_WORKER = (
    "### implement-worker\n"
    "- **role**: builder\n"
    "- **inputs**: ticket\n"
    "- **output**: diff\n"
    "- **write scope**: worktree\n"
    "- **isolation**: worktree\n"
    "- **stop**: on green\n"
)


def test_check_contract_doc_missing_file(tmp_path):
    violations = cp.check_contract_doc(tmp_path / "nope.md", set())
    assert len(violations) == 1
    assert "missing" in violations[0].detail


def test_check_contract_doc_flags_undefined_referenced_anchor(tmp_path):
    doc = _md(tmp_path, "worker-contracts.md", _COMPLETE_WORKER)
    violations = cp.check_contract_doc(doc, {"implement-worker", "ghost-worker"})
    details = " ".join(v.detail for v in violations)
    assert "ghost-worker" in details
    # implement-worker is defined and complete -> no complaint about it.
    assert "#implement-worker not defined" not in details


def test_check_contract_doc_flags_incomplete_worker_section(tmp_path):
    incomplete = (
        "### broken-worker\n"
        "- **role**: builder\n"
        "- **inputs**: ticket\n"
        # missing output / write scope / isolation / stop
    )
    doc = _md(tmp_path, "worker-contracts.md", incomplete)
    violations = cp.check_contract_doc(doc, set())
    assert len(violations) == 1
    detail = violations[0].detail
    assert "broken-worker" in detail
    for missing in ("output", "write scope", "isolation", "stop"):
        assert missing in detail


def test_check_contract_doc_complete_worker_passes(tmp_path):
    doc = _md(tmp_path, "worker-contracts.md", _COMPLETE_WORKER)
    assert cp.check_contract_doc(doc, {"implement-worker"}) == []


# --- Violation str ----------------------------------------------------------


def test_violation_str_format():
    v = cp.Violation("cmd.md", 12, "something wrong")
    assert str(v) == "cmd.md:12: something wrong"
