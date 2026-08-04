"""Tests for chief_wiggum.ai_disclosure and its CLI wrapper (chief-wiggum#317)."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ai_disclosure as ai_disclosure_cli  # noqa: E402
from chief_wiggum import ai_disclosure  # noqa: E402

# --- library: PR/issue body disclosure --------------------------------------


def test_ensure_disclosure_appends_line():
    out = ai_disclosure.ensure_disclosure("## Summary\n\nDid a thing.\n")
    assert ai_disclosure.DISCLOSURE_LINE in out
    assert out.startswith("## Summary")


def test_ensure_disclosure_idempotent():
    once = ai_disclosure.ensure_disclosure("body text\n")
    twice = ai_disclosure.ensure_disclosure(once)
    assert twice == once
    assert twice.count(ai_disclosure.DISCLOSURE_LINE) == 1


def test_ensure_disclosure_handles_no_trailing_newline():
    out = ai_disclosure.ensure_disclosure("no trailing newline")
    assert "no trailing newline" in out
    assert ai_disclosure.DISCLOSURE_LINE in out


# --- library: commit trailer -------------------------------------------------


def test_ensure_commit_trailer_appends():
    out = ai_disclosure.ensure_commit_trailer("fix: do the thing\n\nDetails here.\n")
    assert ai_disclosure.COMMIT_TRAILER in out
    assert out.startswith("fix: do the thing")


def test_ensure_commit_trailer_idempotent():
    once = ai_disclosure.ensure_commit_trailer("chore: bump\n")
    twice = ai_disclosure.ensure_commit_trailer(once)
    assert twice == once
    assert twice.count(ai_disclosure.COMMIT_TRAILER) == 1


def test_disclosure_line_and_trailer_are_distinct():
    # The two artifact shapes must not be interchangeable — a PR body appended
    # with the commit trailer (or vice versa) would silently mismatch style.
    assert ai_disclosure.DISCLOSURE_LINE != ai_disclosure.COMMIT_TRAILER


# --- CLI ----------------------------------------------------------------------


def test_cli_body_via_stdin_stdout(capsys, monkeypatch):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("## Summary\n\nx\n"))
    rc = ai_disclosure_cli.main(["body"])
    assert rc == 0
    out = capsys.readouterr().out
    assert ai_disclosure.DISCLOSURE_LINE in out


def test_cli_commit_trailer_via_file(tmp_path):
    msg_file = tmp_path / "commit-msg.txt"
    msg_file.write_text("feat: add widget\n")
    rc = ai_disclosure_cli.main(["commit-trailer", "--file", str(msg_file)])
    assert rc == 0
    assert ai_disclosure.COMMIT_TRAILER in msg_file.read_text()


def test_cli_body_via_file_is_idempotent(tmp_path):
    body_file = tmp_path / "issue-body.md"
    body_file.write_text("## Summary\n\nDo the thing.\n")
    ai_disclosure_cli.main(["body", "--file", str(body_file)])
    once = body_file.read_text()
    ai_disclosure_cli.main(["body", "--file", str(body_file)])
    twice = body_file.read_text()
    assert once == twice
