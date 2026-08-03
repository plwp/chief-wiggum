"""chief-wiggum#250: external source of taste — a living current-craft design brief.

Docs/pattern-text deliverable: no gate script is planned or wanted (taste is a human
checkpoint, not a lintable property — the anti-theater rule cuts both ways, same as
#249). These tests verify the artifacts exist, are well-formed, and are wired into the
consuming workflows, not that any aesthetic judgment is "correct."
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESIGN_TASTE = ROOT / "docs" / "design-taste.md"
TASTE_SCHEMA = ROOT / "templates" / "taste-choice-schema.json"
UPDATE_CMD = ROOT / ".claude" / "commands" / "update.md"
DESIGN_CMD = ROOT / ".claude" / "commands" / "design.md"


def test_design_taste_brief_exists_with_as_of():
    text = DESIGN_TASTE.read_text()
    m = re.search(r"as_of:\s*(\d{4}-\d{2}-\d{2})", text)
    assert m, "docs/design-taste.md must carry a parseable `as_of: YYYY-MM-DD` date"


def test_design_taste_brief_has_required_sections():
    text = DESIGN_TASTE.read_text()
    for heading in (
        "design moves that currently read high-craft",
        "instantly-dating anti-patterns",
        "typography / palette / layout currency notes",
        "direction briefs per genre",
        "refresh mechanism",
        "pinned source roster",
        "explicit non-goals",
        "operator taste profile",
    ):
        assert heading in text.lower(), f"missing section: {heading}"


def test_design_taste_states_the_staleness_rule():
    text = DESIGN_TASTE.read_text()
    assert "90 day" in text.lower() or "90-day" in text.lower()


def test_design_taste_explicit_non_goals_present():
    text = DESIGN_TASTE.read_text().lower()
    assert "no aesthetic gate script" in text
    assert "no automated scraping" in text
    assert "no training" in text or "no...fine-tuning" in text or "fine-tuning" in text


def test_design_taste_names_no_private_products():
    """The public repo may never name a private CW-adopted product (CLAUDE.md hard rule)."""
    text = DESIGN_TASTE.read_text().lower()
    for banned in ("dogeared", "safetrail", "booking-forms", "duplicat-rex", "ratably"):
        assert banned not in text, f"private/bet product name leaked into docs/design-taste.md: {banned}"


def test_taste_choice_schema_is_valid_json_and_documents_the_spread_invariant():
    schema = json.loads(TASTE_SCHEMA.read_text())
    assert schema["required"] == ["ts", "context", "chosen", "rejected"]
    props = schema["properties"]
    assert "chosen" in props and "rejected" in props
    # the never-collapse-the-spread invariant must be stated, not just implied
    assert "collapse" in schema["description"].lower() or "never" in schema["description"].lower()


def test_update_workflow_wires_in_design_taste_refresh():
    text = UPDATE_CMD.read_text()
    assert "design-taste.md" in text
    assert "3.7" in text


def test_design_workflow_reads_design_taste_before_generating_directions():
    text = DESIGN_CMD.read_text()
    assert "design-taste.md" in text
    assert "90 days stale" in text or "as_of" in text
