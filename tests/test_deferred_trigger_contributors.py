"""`human_contributors_gte` counts humans, not email addresses (chief-wiggum#171).

Several deferred-rigor triggers key off "a second human contributor". The check
counted distinct author EMAILS, and chief-wiggum's own history has one person
committing under four: a work address, a personal address, a plus-addressed
alias, and a GitHub noreply.

So the trigger read CANDIDATE on this repo permanently — and a trigger that is
always CANDIDATE cannot signal the thing it exists for, because a genuine
second contributor looks exactly like the noise.

Git already solves this. `--use-mailmap` collapses mapped addresses to one
identity, and a repo with no `.mailmap` is unaffected: git falls back to the
raw address, so the change is an improvement where the map exists and a no-op
where it does not.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from check_deferred_triggers import check_human_contributors_gte  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    # --initial-branch pinned: Apple git defaults to main, Linux CI to master,
    # and a fixture that relies on the default passes locally and fails in CI.
    subprocess.run(["git", "init", "--initial-branch", "main", str(root)],
                   capture_output=True, check=True)
    return root


def _commit(repo: Path, name: str, email: str, text: str) -> None:
    (repo / "f.txt").write_text(text)
    _git(repo, "add", "f.txt")
    subprocess.run(
        ["git", "-C", str(repo), "-c", f"user.name={name}", "-c", f"user.email={email}",
         "commit", "-m", text],
        capture_output=True, check=True)


def test_one_human_under_several_emails_counts_once(tmp_path):
    """The defect: four addresses, one person, reported as four contributors."""
    repo = _repo(tmp_path)
    _commit(repo, "Pat", "pat@work.example", "a")
    _commit(repo, "pat", "pat+github@personal.example", "b")
    _commit(repo, "Patrick", "pat@personal.example", "c")
    (repo / ".mailmap").write_text(
        "Pat <pat@personal.example> <pat@work.example>\n"
        "Pat <pat@personal.example> <pat+github@personal.example>\n")
    _commit(repo, "Pat", "pat@personal.example", "d")

    status, detail = check_human_contributors_gte(repo, 2)
    assert status == "QUIET", detail
    assert "1 distinct" in detail


def test_a_genuine_second_contributor_still_registers(tmp_path):
    """The mailmap must not suppress the signal it exists to clarify."""
    repo = _repo(tmp_path)
    _commit(repo, "Pat", "pat@work.example", "a")
    (repo / ".mailmap").write_text("Pat <pat@personal.example> <pat@work.example>\n")
    _commit(repo, "Pat", "pat@personal.example", "b")
    _commit(repo, "Someone Else", "someone@else.example", "c")

    status, detail = check_human_contributors_gte(repo, 2)
    assert status == "CANDIDATE", detail
    assert "2 distinct" in detail


def test_a_repo_with_no_mailmap_is_unaffected(tmp_path):
    """Strictly an improvement where the map exists, a no-op where it does not."""
    repo = _repo(tmp_path)
    _commit(repo, "A", "a@example.com", "a")
    _commit(repo, "B", "b@example.com", "b")
    status, detail = check_human_contributors_gte(repo, 2)
    assert status == "CANDIDATE", detail
    assert "2 distinct" in detail


def test_bots_are_still_excluded(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "Pat", "pat@example.com", "a")
    _commit(repo, "bot", "actions@github.com", "b")
    _commit(repo, "nr", "12345+x@users.noreply.github.com", "c")
    status, detail = check_human_contributors_gte(repo, 2)
    assert status == "QUIET", detail
    assert "1 distinct" in detail


def test_no_repo_is_unevaluated_not_quiet():
    """UNEVALUATED and QUIET are different claims: 'nobody looked' must never
    read as 'checked, and nothing fired'."""
    status, detail = check_human_contributors_gte(None, 2)
    assert status == "UNEVALUATED"


# --- this repo's own map ------------------------------------------------------

def test_chief_wiggum_has_a_mailmap_covering_its_alias_history():
    """Without it, every trigger keyed on a second contributor sits at
    CANDIDATE forever and tells nobody anything."""
    mailmap = REPO / ".mailmap"
    assert mailmap.is_file(), "no .mailmap — the contributor triggers cannot mean anything"
    text = mailmap.read_text()
    assert "noreply" in text, "the GitHub noreply alias is unmapped"


def test_this_repo_now_reports_a_single_human():
    status, detail = check_human_contributors_gte(REPO, 2)
    assert status == "QUIET", (
        f"chief-wiggum reports multiple contributors: {detail}. If that is a real "
        f"second human, this test should be updated — if it is another unmapped "
        f"alias, add it to .mailmap")


def test_the_canonical_address_is_not_itself_filtered_as_a_bot():
    """A mailmap that canonicalises onto the GitHub noreply address would take
    the count to ZERO, since 'noreply' matches BOT_EMAIL_MARKERS — a silent
    inversion of the whole check."""
    _, detail = check_human_contributors_gte(REPO, 2)
    assert "0 distinct" not in detail, detail
