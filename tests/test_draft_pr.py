"""Tests for scripts/draft_pr.py --require-cost (chief-wiggum#345 item 4 / AC4).

PRs went out with "see ledger" hand-edits because the costing pipeline
produced nothing usable at PR time. --require-cost fails loudly on a
missing/empty Implementation Cost section -- opt-in, so /ship (which has no
ticket and no cost data) is unaffected and `shipping.REQUIRED_SECTIONS`
stays untouched (tests/test_shipping.py's existing
test_implementation_cost_omitted_when_absent / test_cli_writes_body_and_validates
must keep passing unmodified).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import draft_pr  # noqa: E402


def test_require_cost_fails_on_missing_section(capsys, tmp_path):
    out = tmp_path / "pr.md"
    rc = draft_pr.main([
        "--issue", "9", "--summary", "Do X", "--change", "Add module",
        "--out", str(out), "--require-cost",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "cost" in err.lower()
    assert not out.exists()


def test_require_cost_fails_on_whitespace_only_section(tmp_path):
    out = tmp_path / "pr.md"
    rc = draft_pr.main([
        "--issue", "9", "--summary", "Do X", "--change", "Add module",
        "--implementation-cost", "   \n  ",
        "--out", str(out), "--require-cost",
    ])
    assert rc == 1
    assert not out.exists()


def test_require_cost_passes_when_section_present(tmp_path):
    out = tmp_path / "pr.md"
    rc = draft_pr.main([
        "--issue", "9", "--summary", "Do X", "--change", "Add module",
        "--implementation-cost", "| Layer |\n**$4.06**",
        "--out", str(out), "--require-cost",
    ])
    assert rc == 0
    assert "**$4.06**" in out.read_text()


def test_require_cost_fails_on_a_hand_typed_stub(capsys, tmp_path):
    """A non-empty string that isn't genuine ticket_cost.py output (e.g. a
    hand-typed 'see ledger' placeholder) must still be rejected — accepting
    any non-empty string was the loophole this flag existed to close
    (chief-wiggum#345 reviewer finding)."""
    out = tmp_path / "pr.md"
    rc = draft_pr.main([
        "--issue", "9", "--summary", "Do X", "--change", "Add module",
        "--implementation-cost", "see ledger",
        "--out", str(out), "--require-cost",
    ])
    assert rc == 1
    assert "stub" in capsys.readouterr().err.lower()
    assert not out.exists()


def test_require_cost_passes_for_genuine_unmetered_block(tmp_path):
    """render_actual_markdown's unmetered branch is genuine ticket_cost.py
    output with no cost table at all — must not be mistaken for a stub."""
    out = tmp_path / "pr.md"
    rc = draft_pr.main([
        "--issue", "9", "--summary", "Do X", "--change", "Add module",
        "--implementation-cost",
        "**Unmetered** — no cost records for acme/app#42 in the factory "
        "ledger. That is absence of telemetry, not a $0 build.",
        "--out", str(out), "--require-cost",
    ])
    assert rc == 0


def test_require_cost_passes_for_coverage_line_without_a_cost_table(tmp_path):
    """A bare coverage block (no priced layer table alongside it) is still
    genuine — the marker set is an OR, not a requirement that all three
    shapes appear together."""
    out = tmp_path / "pr.md"
    rc = draft_pr.main([
        "--issue", "9", "--summary", "Do X", "--change", "Add module",
        "--implementation-cost", "**Coverage — 0 of 3 layers captured.**",
        "--out", str(out), "--require-cost",
    ])
    assert rc == 0
