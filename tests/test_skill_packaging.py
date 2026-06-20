"""Portable skill packaging conformance tests (#25).

The umbrella `skills/chief-wiggum/` skill must be a valid, portable skill:
short SKILL.md with clean frontmatter, per-workflow references that resolve to
the canonical `.claude/commands/*.md` bodies (single source of truth, no drift),
isolated Codex metadata, and `.claude/commands/` left intact.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO / "skills" / "chief-wiggum"
SKILL_MD = SKILL_DIR / "SKILL.md"
WORKFLOWS = SKILL_DIR / "references" / "workflows"

CORE_WORKFLOWS = ("design", "plan-epic", "architect", "implement", "implement-wave", "close-epic")


def _frontmatter(text: str) -> dict[str, str]:
    """Parse the leading ``---`` frontmatter block into key/value pairs."""
    lines = text.splitlines()
    assert lines and lines[0].strip() == "---", "SKILL.md must start with a --- frontmatter block"
    fm: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fm
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip()
    raise AssertionError("unterminated frontmatter block")


def test_skill_md_exists_with_clean_frontmatter():
    assert SKILL_MD.is_file(), "skills/chief-wiggum/SKILL.md must exist"
    fm = _frontmatter(SKILL_MD.read_text())
    assert set(fm) == {"name", "description"}, f"frontmatter keys must be name+description, got {set(fm)}"
    assert fm["name"] == "chief-wiggum"
    assert len(fm["description"]) > 20


def test_skill_md_is_short_progressive_disclosure():
    # The umbrella must route to references, not inline every procedure.
    body = SKILL_MD.read_text()
    assert len(body.splitlines()) < 120, "SKILL.md should be short (<120 lines)"
    assert len(body.encode()) < 6000, "SKILL.md should be small (<6 KB)"


def test_core_workflow_references_are_symlinks_to_command_bodies():
    for wf in CORE_WORKFLOWS:
        ref = WORKFLOWS / f"{wf}.md"
        # Single source of truth: it must be a *symlink* to the canonical command
        # body (a copy would drift and is rejected), with the exact relative target.
        assert ref.is_symlink(), f"{ref} must be a symlink to the command body, not a copy"
        assert ref.readlink() == Path(f"../../../../.claude/commands/{wf}.md")
        assert ref.resolve().is_file()
        assert ref.read_text().strip(), f"workflow reference {wf} resolves to empty content"


def test_all_workflow_references_are_relative_symlinks():
    for ref in WORKFLOWS.glob("*.md"):
        assert ref.is_symlink(), f"{ref.name} must be a symlink"
        assert not ref.readlink().is_absolute(), f"{ref.name} symlink target must be relative"


def test_command_bodies_still_intact():
    for wf in CORE_WORKFLOWS:
        cmd = REPO / ".claude" / "commands" / f"{wf}.md"
        assert cmd.is_file() and cmd.read_text().strip(), f".claude/commands/{wf}.md must remain usable"


def test_codex_metadata_isolated():
    openai = SKILL_DIR / "agents" / "openai.yaml"
    assert openai.is_file(), "Codex metadata must live in agents/openai.yaml"
    assert openai.read_text().strip()
    # The umbrella SKILL.md must not carry harness-specific frontmatter.
    fm = _frontmatter(SKILL_MD.read_text())
    assert "openai" not in fm and "agents" not in fm


def test_readme_documents_install_paths():
    readme = (REPO / "README.md").read_text().lower()
    assert "commanddirs" in readme, "README must document the Claude Code command-dir install"
    assert "skills/chief-wiggum" in readme, "README must document the portable skill install"
