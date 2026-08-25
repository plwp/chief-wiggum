"""`statement_for` returns the prose, not the label (code_query).

`orient scripts/ratchet.py` rendered agent-facing output as

    CTR-fh-040**: Verify --scanner-version prints ...

The leading `**` strip only fired at line start, but contract-assertions.md
writes its declarations as `- [ ] **CTR-x-001**: prose`. With the marker
un-stripped, `raw.split("**", 1)[-1]` returned everything after the FIRST `**`
— which is the label itself — instead of the prose after the closing one.

It went unnoticed because the shape it handles correctly is the one used in
invariants.md (`**INV-x-001** — prose`, bold at line start), so most facts
rendered fine and only assertion-sourced ones were garbled.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from code_query import Epic  # noqa: E402


def statement(tmp_path: Path, line: str, node_id: str = "CTR-x-001") -> str:
    """Drive statement_for against a one-line artifact."""
    doc = tmp_path / "contract-assertions.md"
    doc.write_text(line + "\n")
    epic = Epic(slug="t", dir=tmp_path, defined={node_id: (doc.name, 1)})
    return epic.statement_for(node_id)


# --- the shapes that were broken ---------------------------------------------

def test_checkbox_list_item(tmp_path):
    """contract-assertions.md's shape — the one that was garbled."""
    got = statement(tmp_path, "- [ ] **CTR-x-001**: Verify the thing holds.")
    assert got == "Verify the thing holds."


def test_ticked_checkbox(tmp_path):
    got = statement(tmp_path, "- [x] **CTR-x-001**: Verify the thing holds.")
    assert got == "Verify the thing holds."


@pytest.mark.parametrize("bullet", ["-", "*", "+"])
def test_plain_bullet(tmp_path, bullet):
    got = statement(tmp_path, f"{bullet} **CTR-x-001**: Verify the thing holds.")
    assert got == "Verify the thing holds."


def test_the_label_never_survives_into_the_statement(tmp_path):
    """The specific regression: the ID and its closing markers leaking in."""
    got = statement(tmp_path, "- [ ] **CTR-x-001**: Verify the thing holds.")
    assert "CTR-x-001" not in got
    assert "**" not in got


# --- the shapes that already worked, which must not regress -------------------

def test_bold_at_line_start_with_em_dash(tmp_path):
    """invariants.md's shape — correct before the fix, and still correct."""
    got = statement(tmp_path, "**INV-x-001** — Scanner version is hash-derived.",
                    node_id="INV-x-001")
    assert got == "— Scanner version is hash-derived."


def test_heading(tmp_path):
    got = statement(tmp_path, "### **CTR-x-001**: Verify the thing holds.")
    assert got == "Verify the thing holds."


def test_a_line_with_no_bold_is_returned_as_prose(tmp_path):
    got = statement(tmp_path, "CTR-x-001 plain prose with no markers")
    assert got == "CTR-x-001 plain prose with no markers"


def test_bold_inside_the_prose_is_not_treated_as_the_label(tmp_path):
    """A statement that emphasises a word mid-sentence must keep it, not get
    truncated at the first marker."""
    got = statement(tmp_path, "- [ ] **CTR-x-001**: Verify it is **never** silent.")
    assert got.startswith("Verify it is")
    assert "never" in got


# --- the real repo ------------------------------------------------------------

def test_cws_own_facts_render_without_leaked_markers():
    import subprocess

    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "code_query.py"),
         "--repo", str(REPO), "--format", "json", "orient", "scripts/ratchet.py"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[:300]

    import json
    facts = json.loads(result.stdout).get("facts") or []
    assert facts, "no governing facts for scripts/ratchet.py — the query found nothing"
    for fact in facts:
        s = fact.get("statement") or ""
        assert "**" not in s, f"markdown leaked into a statement: {s[:80]}"
        assert not s.startswith(fact["id"]), (
            f"statement begins with its own label rather than the prose: {s[:80]}")
