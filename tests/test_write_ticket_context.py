"""Tests for write_ticket_context.py's default one-line summary (#333).

Before this fix, the CLI unconditionally printed the full ticket.json
(issue body + entire comment thread) to stdout after `/implement` Step 2
already `tee`'d the same raw issue JSON into the orchestrator's own context
— the same text billed twice into the most expensive context. Default
output is now a one-line summary; `--print` opts back into the full dump.
"""

from __future__ import annotations

import json

import write_ticket_context


def _write_issue_json(tmp_path, *, comments=None, body="do the thing"):
    path = tmp_path / "issue-raw.json"
    path.write_text(
        json.dumps(
            {
                "title": "Fix the widget",
                "body": body,
                "author": {"login": "alice"},
                "comments": comments or [],
            }
        )
    )
    return path


def test_default_output_is_one_line_summary_not_full_json(tmp_path, capsys):
    issue_json = _write_issue_json(tmp_path)
    out_path = tmp_path / "ticket.json"
    rc = write_ticket_context.main(
        [
            "--issue-json", str(issue_json),
            "--number", "42",
            "--acceptance-criteria", "AC one",
            "--output", str(out_path),
        ]
    )
    assert rc == 0
    stdout = capsys.readouterr().out
    # Full ticket body must NOT appear in stdout by default.
    assert "do the thing" not in stdout
    assert str(out_path) in stdout
    assert "0 comment(s)" in stdout
    assert "1 AC line(s)" in stdout
    # The file itself still carries the full content.
    written = json.loads(out_path.read_text())
    assert written["body"] == "do the thing"


def test_default_summary_reflects_comment_count(tmp_path, capsys):
    issue_json = _write_issue_json(
        tmp_path,
        comments=[
            {"id": "1", "author": {"login": "bob"}, "body": "looks good"},
            {"id": "2", "author": {"login": "carol"}, "body": "actually wait"},
        ],
    )
    out_path = tmp_path / "ticket.json"
    rc = write_ticket_context.main(
        ["--issue-json", str(issue_json), "--output", str(out_path)]
    )
    assert rc == 0
    stdout = capsys.readouterr().out
    assert "2 comment(s)" in stdout
    assert "looks good" not in stdout


def test_print_flag_restores_full_json_output(tmp_path, capsys):
    issue_json = _write_issue_json(tmp_path, body="the full body text")
    out_path = tmp_path / "ticket.json"
    rc = write_ticket_context.main(
        ["--issue-json", str(issue_json), "--output", str(out_path), "--print"]
    )
    assert rc == 0
    stdout = capsys.readouterr().out
    assert "the full body text" in stdout
    payload = json.loads(stdout)
    assert payload["body"] == "the full body text"


def test_output_file_always_written_regardless_of_print_flag(tmp_path):
    issue_json = _write_issue_json(tmp_path)
    out_path = tmp_path / "ticket.json"
    write_ticket_context.main(["--issue-json", str(issue_json), "--output", str(out_path)])
    assert out_path.exists()
    assert json.loads(out_path.read_text())["comments"] == []
