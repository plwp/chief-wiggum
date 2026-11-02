"""No skill or script tracks work through ``gh issue`` outside the GitHub adapter.

chief-wiggum#498: skills call the tracker interface (scripts/tracker.py), so a
target can swap GitHub for another tracker by config. One stray ``gh issue``
call in a skill silently bypasses that switch. ``gh`` for PRs, repos, labels
and milestones is unaffected.

The scanner is checked against planted calls first, so a regex that matches
nothing cannot pass as a clean tree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CW_ROOT = Path(__file__).resolve().parents[1]

# The GitHub adapter: the only file allowed to shell out to `gh issue`.
ADAPTER = "scripts/tracker.py"

# Everything a skill or script is made of. docs/ and tests/ describe or fake
# the calls; they never run them for a workflow.
SCANNED = [
    (".claude/commands", "*.md"),
    ("skills", "*"),
    ("scripts", "*.py"),
    ("templates", "*"),
    ("config", "*"),
    ("start", "*"),
]

_VERBS = (
    "create|view|list|edit|close|reopen|comment|delete|transfer|develop|"
    "pin|unpin|lock|unlock|status"
)

PATTERNS = {
    # Shell form, including flags before the subcommand and line continuations:
    #   gh issue view 1   |   gh --repo a/b \\\n  issue create
    "shell": re.compile(
        r"\bgh\b(?:[ \t]+(?:-{1,2}[\w-]+(?:[ =](?:\"[^\"\n]*\"|'[^'\n]*'|\S+))?|\\\n))*"
        r"[ \t]+(?:\\\n[ \t]*)?issue[ \t]+(?:" + _VERBS + r")\b"
    ),
    # Python argv form: ["gh", "issue", ...] or ("issue", "view", ...)
    "argv": re.compile(
        r"""["']gh["']\s*,\s*["']issue["']|["']issue["']\s*,\s*["'](?:""" + _VERBS + r""")["']"""
    ),
    # The REST form of the same thing: gh api repos/<o>/<r>/issues...
    "api": re.compile(r"\bgh\b[^\n]*\bapi\b[^\n]*repos/[^\s'\"]*/issues\b"),
}


def find_gh_issue_calls(text: str) -> list[tuple[str, int, str]]:
    hits = []
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            hits.append((kind, line, match.group(0).strip()))
    return hits


def _scanned_files():
    for rel_dir, glob in SCANNED:
        base = CW_ROOT / rel_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob(glob)):
            if path.is_file() and "__pycache__" not in path.parts:
                yield path


PLANTED = [
    'gh issue view "$n" --repo "$r" --json milestone',
    "gh issue close $t --repo r --comment done",
    'X=$(gh issue list --repo "$r" --milestone "$m")',
    'gh --repo "$(echo a/b)" \\\n  issue create --title t',
    "gh -R a/b issue comment 4 --body hi",
    'subprocess.run(["gh", "issue", "edit", "1"])',
    'args = ["issue", "reopen", str(n), "--repo", repo]',
    "gh api repos/a/b/issues/4/comments -f body=x",
]

ALLOWED = [
    "gh pr create --title t",
    "gh label create bug",
    "gh api repos/a/b/milestones",
    "Fetch tickets via tracker.py instead of calling `gh issue` directly",
    'tracker.py --repo-root "$T" complete "$r#$n" --summary s',
    "gh repo view a/b",
]


class TestScannerBites:
    @pytest.mark.parametrize("planted", PLANTED)
    def test_planted_call_is_found(self, planted):
        assert find_gh_issue_calls(planted), f"scanner missed: {planted!r}"

    @pytest.mark.parametrize("allowed", ALLOWED)
    def test_non_issue_gh_use_is_allowed(self, allowed):
        assert find_gh_issue_calls(allowed) == []

    def test_scanner_reads_the_adapter(self):
        """The allow-list is real: the adapter itself does contain the calls."""
        assert find_gh_issue_calls((CW_ROOT / ADAPTER).read_text())

    def test_scan_covers_skills_and_scripts(self):
        files = {p.relative_to(CW_ROOT).as_posix() for p in _scanned_files()}
        assert ".claude/commands/implement.md" in files
        assert ".claude/commands/implement-wave.md" in files
        assert "scripts/chief_wiggum/github.py" in files


def test_no_gh_issue_outside_the_github_adapter():
    offenders = []
    for path in _scanned_files():
        rel = path.relative_to(CW_ROOT).as_posix()
        if rel == ADAPTER:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        offenders += [f"{rel}:{line} [{kind}] {snippet}" for kind, line, snippet in find_gh_issue_calls(text)]
    assert not offenders, (
        "gh issue called outside the GitHub tracker adapter; use scripts/tracker.py "
        "(docs/tracker.md):\n" + "\n".join(offenders)
    )
