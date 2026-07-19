"""Tests for the content-addressed manifest helper (#160), against a real temp
git repo (init, commit, dirty a file, add untracked) — not mocked git plumbing."""

from __future__ import annotations

import subprocess
from pathlib import Path

from chief_wiggum import manifest as m


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _commit(repo: Path, message: str = "commit") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def test_build_manifest_includes_committed_files(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a.go").write_text("package a\n")
    (repo / "b.py").write_text("x = 1\n")
    _commit(repo)

    result = m.build_manifest(repo)
    assert set(result) == {"a.go", "b.py"}
    assert all(len(h) == 40 for h in result.values())  # git blob sha1


def test_build_manifest_reflects_dirty_tracked_file(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a.go").write_text("package a\n")
    _commit(repo)
    before = m.build_manifest(repo)["a.go"]

    (repo / "a.go").write_text("package a\n\nfunc f() {}\n")  # dirty, uncommitted
    after = m.build_manifest(repo)["a.go"]

    assert before != after


def test_build_manifest_includes_untracked_non_ignored_file(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a.go").write_text("package a\n")
    _commit(repo)

    (repo / "new_untracked.go").write_text("package a\n\nfunc g() {}\n")
    result = m.build_manifest(repo)
    assert "new_untracked.go" in result


def test_build_manifest_excludes_gitignored_untracked_file(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / ".gitignore").write_text("ignored.go\n")
    (repo / "a.go").write_text("package a\n")
    _commit(repo)

    (repo / "ignored.go").write_text("package a\n// should never be scanned\n")
    result = m.build_manifest(repo)
    assert "ignored.go" not in result


def test_build_manifest_excludes_deleted_file(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a.go").write_text("package a\n")
    (repo / "b.go").write_text("package a\n")
    _commit(repo)

    (repo / "b.go").unlink()  # deleted, uncommitted
    result = m.build_manifest(repo)
    assert "b.go" not in result
    assert "a.go" in result


def test_build_manifest_applies_predicate(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a.go").write_text("package a\n")
    (repo / "readme.md").write_text("# hi\n")
    _commit(repo)

    result = m.build_manifest(repo, predicate=lambda p: p.endswith(".go"))
    assert set(result) == {"a.go"}


def test_build_manifest_matches_git_hash_object(tmp_path):
    """Manifest hashes are exactly what ``git hash-object`` would produce — so a
    future content-addressed cache can key directly off them."""
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a.go").write_text("package a\n")
    _commit(repo)

    expected = subprocess.run(
        ["git", "hash-object", "a.go"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert m.build_manifest(repo)["a.go"] == expected


# --- tree_manifest / changed_paths ------------------------------------------


def test_tree_manifest_is_pure_committed_state_no_working_tree_overlay(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a.go").write_text("package a\n")
    _commit(repo, "first")
    first_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "a.go").write_text("package a\n\nfunc f() {}\n")  # dirty after the ref

    baseline = m.tree_manifest(repo, first_sha)
    assert baseline["a.go"] == subprocess.run(
        ["git", "rev-parse", "HEAD:a.go"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_changed_paths_detects_dirty_file(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a.go").write_text("package a\n")
    (repo / "b.go").write_text("package a\n")
    _commit(repo, "first")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "a.go").write_text("package a\n\nfunc f() {}\n")  # dirty
    changed = m.changed_paths(repo, base_sha)
    assert changed == {"a.go"}


def test_changed_paths_detects_new_commit_since_ref(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a.go").write_text("package a\n")
    _commit(repo, "first")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "b.go").write_text("package b\n")
    _commit(repo, "second")

    changed = m.changed_paths(repo, base_sha)
    assert changed == {"b.go"}


def test_changed_paths_detects_untracked_file(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a.go").write_text("package a\n")
    _commit(repo, "first")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "c.go").write_text("package c\n")  # untracked
    changed = m.changed_paths(repo, base_sha)
    assert changed == {"c.go"}


def test_changed_paths_empty_when_nothing_changed(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a.go").write_text("package a\n")
    _commit(repo, "first")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    assert m.changed_paths(repo, base_sha) == set()


def test_changed_paths_applies_predicate(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a.go").write_text("package a\n")
    _commit(repo, "first")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "a.go").write_text("package a\n\nfunc f() {}\n")
    (repo / "notes.md").write_text("dirty markdown\n")
    changed = m.changed_paths(repo, base_sha, predicate=lambda p: p.endswith(".go"))
    assert changed == {"a.go"}
