"""Tests for review prompt assembly and review run (P1-7)."""

from __future__ import annotations

import subprocess

import pytest
from chief_wiggum import review
from providers import Provider, Role, RolePlan

TEMPLATE = """# Review
Ticket: {{TICKET_TITLE}}
Desc: {{TICKET_DESCRIPTION}}
AC:
{{ACCEPTANCE_CRITERIA}}
Diff:
```diff
{{DIFF}}
```
"""


def _ticket(**kw):
    base = {"number": 42, "title": "Add thing", "body": "Do the thing", "acceptance_criteria": ["AC one", "AC two"]}
    base.update(kw)
    return review.TicketContext(**base)


# --- template substitution --------------------------------------------------


def test_assemble_substitutes_all_placeholders():
    out = review.assemble_review_prompt(TEMPLATE, _ticket(), "the diff body")
    assert "Ticket: Add thing" in out
    assert "Desc: Do the thing" in out
    assert "- AC one" in out and "- AC two" in out
    assert "the diff body" in out
    assert "{{" not in out


def test_missing_ac_renders_placeholder_text():
    out = review.assemble_review_prompt(TEMPLATE, _ticket(acceptance_criteria=[]), "d")
    assert "(none specified)" in out


def test_diff_with_braces_is_not_format_interpreted():
    # A diff containing { } must not break substitution.
    out = review.assemble_review_prompt(TEMPLATE, _ticket(), "func() { return {a: 1}; }")
    assert "{ return {a: 1}; }" in out


def test_checklist_and_epic_sections_appended():
    out = review.assemble_review_prompt(
        TEMPLATE, _ticket(), "d",
        checklist="# Checklist\n- item",
        epic_sections=[("Contracts", "REQUIRES x"), ("Empty", "  ")],
    )
    assert "## Contracts" in out and "REQUIRES x" in out
    assert "# Checklist" in out
    # Empty epic section is skipped.
    assert "## Empty" not in out


# --- diff truncation --------------------------------------------------------


def test_truncate_small_diff_unchanged():
    assert review.truncate_diff("small", max_bytes=100) == "small"


def test_truncate_large_diff():
    big = "x" * 5000
    out = review.truncate_diff(big, max_bytes=1000)
    assert "diff truncated at 1000 bytes of 5000" in out
    assert len(out.encode()) < 5000


# --- ticket context parsing -------------------------------------------------


def test_ticket_from_dict_parses_string_ac():
    t = review.TicketContext.from_dict({"number": 1, "title": "t", "acceptance_criteria": "- one\n- two"})
    assert t.acceptance_criteria == ["one", "two"]


# --- git guards (mocked) ----------------------------------------------------


def _runner(mapping):
    def run(args, **kwargs):
        key = " ".join(args)
        for needle, (rc, out) in mapping.items():
            if needle in key:
                return subprocess.CompletedProcess(args, rc, stdout=out, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return run


def test_assert_git_repo_refuses_non_repo(tmp_path):
    with pytest.raises(review.ReviewError, match="not a git"):
        review.assert_git_repo(tmp_path, runner=_runner({"rev-parse --show-toplevel": (128, "")}))


def test_capture_diff_refuses_unresolvable_base(tmp_path):
    runner = _runner({"rev-parse --verify": (1, "")})
    with pytest.raises(review.ReviewError, match="base ref"):
        review.capture_diff(tmp_path, "nope", runner=runner)


def test_capture_diff_returns_truncated(tmp_path):
    runner = _runner({"rev-parse --verify": (0, "abc"), "diff": (0, "y" * 5000)})
    out = review.capture_diff(tmp_path, "main", runner=runner, max_bytes=1000)
    assert "diff truncated" in out


# --- synthesis prompt -------------------------------------------------------


def test_synthesis_prompt_lists_responses():
    p = review.build_synthesis_prompt(["a/reviewer-codex.md", "a/reviewer-gemini.md"])
    assert "reviewer-codex.md" in p and "reviewer-gemini.md" in p


# --- full run (mocked git + provider) ---------------------------------------


def _plan():
    role = Role(name="reviewer", required=("codex",), optional=("gemini",))
    return RolePlan(
        role=role,
        required=(Provider("codex", "tool", True, tool="codex"),),
        optional=(Provider("gemini", "tool", True, tool="gemini"),),
        missing_required=(),
        skipped_optional=(),
    )


def test_run_review_end_to_end(tmp_path, monkeypatch):
    out = tmp_path / "reviews"
    runner = _runner(
        {
            "rev-parse --show-toplevel": (0, str(tmp_path)),
            "rev-parse --verify": (0, "abc"),
            "diff": (0, "diff --git a b\n+added line"),
        }
    )
    monkeypatch.setattr(review.providers, "plan_role", lambda r, c: _plan())

    captured = {}

    def execute(provider, prompt):
        captured["prompt"] = prompt
        return "A substantive review with findings to report here."

    manifest = review.run_review(
        _ticket(), tmp_path, "main", out,
        template=TEMPLATE, checklist="# Checklist\n- item",
        config={}, execute=execute, runner=runner,
    )

    assert manifest.ok is True
    assert manifest.base == "main"
    # Files written.
    assert (out / "impl-diff.txt").exists()
    assert (out / "review-prompt.md").exists()
    assert (out / "synthesis-prompt.md").exists()
    assert (out / "review-manifest.json").exists()
    # Provider manifest integrated.
    assert manifest.provider_manifest["ok"] is True
    assert any("reviewer-codex.md" in p for p in manifest.response_paths)
    # The assembled prompt (with diff + AC) reached the provider.
    assert "added line" in captured["prompt"]
    assert "- AC one" in captured["prompt"]


def test_run_review_refuses_without_execute(tmp_path, monkeypatch):
    runner = _runner(
        {"rev-parse --show-toplevel": (0, str(tmp_path)), "rev-parse --verify": (0, "abc"), "diff": (0, "d")}
    )
    monkeypatch.setattr(review.providers, "plan_role", lambda r, c: _plan())
    with pytest.raises(review.ReviewError, match="execute callable"):
        review.run_review(_ticket(), tmp_path, "main", tmp_path / "o", template=TEMPLATE, config={}, runner=runner)


def test_run_review_missing_required_provider_raises(tmp_path, monkeypatch):
    runner = _runner({"rev-parse --show-toplevel": (0, str(tmp_path)), "rev-parse --verify": (0, "abc"), "diff": (0, "d")})
    bad_plan = RolePlan(
        role=Role("reviewer", ("codex",), ()),
        required=(), optional=(), missing_required=("codex",), skipped_optional=(),
    )
    monkeypatch.setattr(review.providers, "plan_role", lambda r, c: bad_plan)
    with pytest.raises(review.ReviewError, match="missing required"):
        review.run_review(
            _ticket(), tmp_path, "main", tmp_path / "o",
            template=TEMPLATE, config={}, execute=lambda p, pr: "x", runner=runner,
        )
