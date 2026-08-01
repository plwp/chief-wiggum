"""Tests for scripts/prevention_signals.py (#216) — diff-scoped slop signals
(new duplication / dead code introduced / assertion-free tests added), all
report-only: the CLI exits 0 no matter what it finds."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import prevention_signals
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _commit_all(repo: Path, msg: str = "c") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg, "--no-verify")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-home"))
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Ada")
    _git(repo, "config", "user.email", "ada@example.com")
    (repo / "lib.py").write_text(
        "def used():\n    return 1\n\nprint(used())\n"
    )
    _commit_all(repo, "seed")
    _git(repo, "checkout", "-q", "-b", "feature")
    return repo


# --- diff parsing (pure) ------------------------------------------------------


def test_parse_added_ranges_reads_unified_zero_hunks():
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -0,0 +1,3 @@\n+a\n+b\n+c\n"
        "@@ -9 +12 @@\n-old\n+new\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
        "@@ -1,4 +0,0 @@\n-a\n-b\n-c\n-d\n"
    )
    added = prevention_signals.parse_added_ranges(diff)
    assert added == {"x.py": [(1, 3), (12, 12)]}


def test_parse_added_ranges_unquotes_c_style_paths():
    """F7: git's default core.quotepath emits C-style quoted paths for
    unicode/space filenames — they must decode to the real path."""
    diff = (
        'diff --git "a/p\\303\\244 th.py" "b/p\\303\\244 th.py"\n'
        '--- "a/p\\303\\244 th.py"\n+++ "b/p\\303\\244 th.py"\n'
        "@@ -0,0 +1,2 @@\n+a\n+b\n"
    )
    added = prevention_signals.parse_added_ranges(diff)
    assert added == {"pä th.py": [(1, 2)]}


def test_unquote_git_path_passthrough_and_escapes():
    assert prevention_signals.unquote_git_path("b/plain.py") == "b/plain.py"
    assert prevention_signals.unquote_git_path('"b/a\\tb.py"') == "b/a\tb.py"
    assert prevention_signals.unquote_git_path('"b/q\\"uote.py"') == 'b/q"uote.py'


# --- (a) new duplication (pure join, no jscpd needed) -------------------------


def test_duplication_flags_class_spanning_added_and_existing_spans():
    classes = [{
        "content_hash": "cafe", "lines": 8,
        "members": [
            {"file": "old.py", "start_line": 10, "end_line": 17},
            {"file": "new.py", "start_line": 3, "end_line": 10},
        ],
    }]
    added = {"new.py": [(1, 40)]}
    findings = prevention_signals.duplication_findings(classes, added)
    assert len(findings) == 1
    assert findings[0]["added_spans"] == ["new.py:3-10"]
    assert findings[0]["existing_spans"] == ["old.py:10-17"]


def test_duplication_ignores_wholly_preexisting_and_wholly_new_classes():
    classes = [
        {"content_hash": "aa", "lines": 5, "members": [
            {"file": "a.py", "start_line": 1, "end_line": 5},
            {"file": "b.py", "start_line": 1, "end_line": 5}]},
        {"content_hash": "bb", "lines": 5, "members": [
            {"file": "n.py", "start_line": 1, "end_line": 5},
            {"file": "n.py", "start_line": 10, "end_line": 14}]},
    ]
    added = {"n.py": [(1, 20)]}  # both bb members are new; aa untouched
    assert prevention_signals.duplication_findings(classes, added) == []


# --- (b) dead code introduced -------------------------------------------------


def test_added_unused_export_is_flagged(repo):
    (repo / "lib.py").write_text(
        "def used():\n    return 1\n\n"
        "def freshly_dead():\n    return 2\n\nprint(used())\n"
    )
    _commit_all(repo, "add dead export")
    env = prevention_signals.build_signals(str(repo), "main", str(repo.parent / "wd"))
    dc = env["signals"]["dead_code_introduced"]
    assert [f["symbol"] for f in dc["findings"]] == ["freshly_dead"]
    assert dc["tier"] == "builtin-ast"


def test_preexisting_dead_symbol_is_not_flagged_for_this_diff(repo):
    # dead symbol already on main
    _git(repo, "checkout", "-q", "main")
    (repo / "lib.py").write_text(
        "def used():\n    return 1\n\n"
        "def old_dead():\n    return 2\n\nprint(used())\n"
    )
    _commit_all(repo, "pre-existing dead")
    _git(repo, "checkout", "-q", "-b", "feature2")
    (repo / "other.py").write_text("VALUE = 1\n")
    _commit_all(repo, "unrelated change")
    env = prevention_signals.build_signals(str(repo), "main", str(repo.parent / "wd"))
    assert env["signals"]["dead_code_introduced"]["findings"] == []


# --- (c) assertion-free tests added -------------------------------------------


def test_added_assertion_free_test_is_flagged(repo):
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_new.py").write_text(
        "def test_hollow():\n    x = 1\n\n"
        "def test_real():\n    assert 1 == 1\n"
    )
    _commit_all(repo, "add tests")
    env = prevention_signals.build_signals(str(repo), "main", str(repo.parent / "wd"))
    af = env["signals"]["assertion_free_tests_added"]
    assert [f["symbol"] for f in af["findings"]] == ["test_hollow"]


def test_preexisting_assertion_free_test_is_not_flagged(repo):
    _git(repo, "checkout", "-q", "main")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_old.py").write_text("def test_hollow():\n    x = 1\n")
    _commit_all(repo, "pre-existing hollow test")
    _git(repo, "checkout", "-q", "-b", "feature3")
    (repo / "other.py").write_text("VALUE = 1\n")
    _commit_all(repo, "unrelated")
    env = prevention_signals.build_signals(str(repo), "main", str(repo.parent / "wd"))
    assert env["signals"]["assertion_free_tests_added"]["findings"] == []


# --- report-only posture ------------------------------------------------------


def test_cli_exits_zero_even_with_findings_and_states_authority(repo):
    (repo / "lib.py").write_text(
        "def used():\n    return 1\n\ndef freshly_dead():\n    return 2\n\nprint(used())\n"
    )
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_new.py").write_text("def test_hollow():\n    x = 1\n")
    _commit_all(repo, "slop")
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "prevention_signals.py"),
         "--repo", str(repo), "--base", "main", "--workdir", str(repo.parent / "wd"),
         "--format", "json"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    env = json.loads(proc.stdout)
    assert env["schema"] == "prevention-signals/1"
    assert "NEVER blocking" in env["authority"]
    assert env["counts"]["dead_code_introduced"] == 1
    assert env["counts"]["assertion_free_tests_added"] == 1
    # duplication either ran (count int) or is honestly skipped (None)
    dup = env["signals"]["new_duplication"]
    assert ("skipped" in dup) == (env["counts"]["new_duplication"] is None)


def test_cli_exits_zero_on_broken_base_ref(repo):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "prevention_signals.py"),
         "--repo", str(repo), "--base", "no-such-ref",
         "--workdir", str(repo.parent / "wd")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert "prevention_signals" in proc.stderr


def test_unicode_space_filename_is_scanned_not_dropped(repo):
    """F7 end-to-end: a changed file whose name needs C-style quoting still
    gets its added dead export flagged (the path resolved), and nothing lands
    in the unresolved-files bucket."""
    (repo / "pä th.py").write_text(
        "def quoted_dead():\n    return 1\n"
    )
    _commit_all(repo, "add unicode/space filename")
    env = prevention_signals.build_signals(str(repo), "main", str(repo.parent / "wd"))
    dc = env["signals"]["dead_code_introduced"]
    assert [f["symbol"] for f in dc["findings"]] == ["quoted_dead"]
    assert [f["file"] for f in dc["findings"]] == ["pä th.py"]
    assert env["unscanned_files"]["count"] == 0


def test_unresolvable_changed_path_is_counted_with_reason(repo, monkeypatch):
    """F7: a changed path that still fails to resolve in the working tree is
    counted under unscanned_files with a reason — never silently clean."""
    diff = ('--- "a/no\\303\\244 such.py"\n+++ "b/no\\303\\244 such.py"\n'
            "@@ -0,0 +1,2 @@\n+a\n+b\n")
    monkeypatch.setattr(prevention_signals, "_git_diff", lambda r, b: diff)
    env = prevention_signals.build_signals(str(repo), "main", str(repo.parent / "wd"))
    assert env["unscanned_files"]["count"] == 1
    assert env["unscanned_files"]["files"] == ["noä such.py"]
    assert "not scanned, not clean" in env["unscanned_files"]["reason"]
    report = prevention_signals.format_report(env)
    assert "unscanned changed path(s): 1" in report
    assert "noä such.py" in report


def test_cli_any_exception_prints_honest_error_block(repo, monkeypatch, capsys):
    """C4: NO prevention-signal failure may vanish — any exception yields an
    error block on stdout (the review context) and exit 0."""
    monkeypatch.setattr(
        prevention_signals, "build_signals",
        lambda *a, **k: (_ for _ in ()).throw(TypeError("engine exploded")))
    monkeypatch.setattr(sys, "argv", [
        "prevention_signals.py", "--repo", str(repo), "--base", "main",
        "--workdir", str(repo.parent / "wd")])
    rc = prevention_signals.main()
    out = capsys.readouterr()
    assert rc == 0
    assert "prevention signals unavailable: engine exploded" in out.out
    assert "treat as not-run, not clean" in out.out

    # json format keeps the contract too
    monkeypatch.setattr(sys, "argv", [
        "prevention_signals.py", "--repo", str(repo), "--base", "main",
        "--workdir", str(repo.parent / "wd"), "--format", "json"])
    rc = prevention_signals.main()
    out = capsys.readouterr()
    assert rc == 0
    doc = json.loads(out.out)
    assert doc["error"] == "engine exploded"
    assert "not-run, not clean" in doc["note"]


def test_text_report_names_each_signal(repo):
    (repo / "other.py").write_text("VALUE = 1\nprint(VALUE)\n")
    _commit_all(repo, "benign")
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "prevention_signals.py"),
         "--repo", str(repo), "--base", "main", "--workdir", str(repo.parent / "wd")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    for token in ("new duplication", "dead code introduced", "assertion-free tests added",
                  "report-only"):
        assert token in proc.stdout
