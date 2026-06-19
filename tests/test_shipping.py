"""Tests for PR body and Mermaid diagram scaffolding (P1-11)."""

from __future__ import annotations

import json

import draft_pr
from chief_wiggum import shipping

# --- mermaid ----------------------------------------------------------------


def test_mermaid_theme_directive_uses_palette():
    d = shipping.mermaid_theme_directive()
    assert d.startswith("%%{init:") and d.endswith("}%%")
    assert "'primaryColor': '#003f5c'" in d
    assert '"' not in d  # single-quoted for mermaid


def test_mermaid_sequence_adds_actor_vars():
    d = shipping.mermaid_theme_directive(sequence=True)
    assert "actorBkg" in d


def test_mermaid_block_wraps_with_theme_and_fences():
    block = shipping.mermaid_block("graph TD\n  A --> B")
    assert block.startswith("```mermaid")
    assert block.rstrip().endswith("```")
    assert "%%{init:" in block
    assert "A --> B" in block


# --- required sections ------------------------------------------------------


def test_validate_sections_flags_missing():
    assert shipping.validate_sections("## Summary\n## Changes") == ["Test Evidence"]


def test_build_pr_body_has_all_required_sections():
    body = shipping.build_pr_body(issue=1, summary="x", changes=["a"])
    assert shipping.validate_sections(body) == []


# --- issue linking ----------------------------------------------------------


def test_issue_linking():
    body = shipping.build_pr_body(issue=42, summary="x", changes=["a"])
    assert "Closes #42" in body


def test_no_issue_no_closes_line():
    body = shipping.build_pr_body(summary="x", changes=["a"])
    assert "Closes #" not in body


# --- conditional sections ---------------------------------------------------


def test_model_conformance_included_when_provided():
    body = shipping.build_pr_body(issue=1, summary="x", changes=["a"], model_conformance="all guards present")
    assert "## Model Conformance" in body and "all guards present" in body


def test_model_conformance_omitted_when_absent():
    body = shipping.build_pr_body(issue=1, summary="x", changes=["a"])
    assert "## Model Conformance" not in body


def test_ux_section_included_when_provided():
    body = shipping.build_pr_body(
        issue=1, summary="x", changes=["a"],
        ux={"status": "matches contract", "screenshots": ["a.png", "b.png"]},
    )
    assert "## UX / Design Fidelity" in body
    assert "matches contract" in body
    assert "`a.png`" in body


def test_ux_section_omitted_when_absent():
    body = shipping.build_pr_body(issue=1, summary="x", changes=["a"])
    assert "## UX / Design Fidelity" not in body


# --- evidence + review integration ------------------------------------------


def test_verification_evidence_rendered():
    verification = {
        "ok": False,
        "steps": [
            {"command": ["make", "test"], "ok": False, "exit_code": 1},
            {"command": ["make", "lint"], "ok": True, "exit_code": 0},
        ],
    }
    body = shipping.build_pr_body(issue=1, summary="x", changes=["a"], verification=verification)
    assert "FAILURES" in body
    assert "`make test` — exit 1" in body


def test_review_summary_rendered():
    review = {"provider_manifest": {"ok": True, "results": [{"name": "codex", "status": "ok"}]}}
    body = shipping.build_pr_body(issue=1, summary="x", changes=["a"], review=review)
    assert "Multi-AI review passed" in body
    assert "codex (ok)" in body


# --- title + command --------------------------------------------------------


def test_suggest_title_adds_issue_suffix():
    assert shipping.suggest_title("Add widget", issue=7) == "feat: Add widget (#7)"


def test_gh_pr_create_command():
    cmd = shipping.gh_pr_create_command("t", "/tmp/body.md", base="main", draft=True)
    assert cmd == ["gh", "pr", "create", "--title", "t", "--body-file", "/tmp/body.md", "--base", "main", "--draft"]


# --- CLI --------------------------------------------------------------------


def test_cli_writes_body_and_validates(tmp_path, capsys):
    verification = tmp_path / "v.json"
    verification.write_text(json.dumps({"ok": True, "steps": [{"command": ["make", "test"], "ok": True, "exit_code": 0}]}))
    out = tmp_path / "pr.md"
    rc = draft_pr.main([
        "--issue", "9", "--summary", "Do X", "--change", "Add module",
        "--verification", str(verification), "--out", str(out), "--print-command",
    ])
    assert rc == 0
    body = out.read_text()
    assert "Closes #9" in body and "## Test Evidence" in body
    assert "gh pr create" in capsys.readouterr().out
