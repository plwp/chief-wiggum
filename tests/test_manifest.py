"""Tests for the content-addressed manifest helper (#160), against a real temp
git repo (init, commit, dirty a file, add untracked) — not mocked git plumbing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
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


# --- error handling (#179 review) --------------------------------------------


def test_build_manifest_non_git_dir_raises_manifest_error(tmp_path):
    with pytest.raises(m.ManifestError):
        m.build_manifest(tmp_path)


def test_changed_paths_bad_ref_raises_manifest_error(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a.go").write_text("package a\n")
    _commit(repo)
    with pytest.raises(m.ManifestError):
        m.changed_paths(repo, "no-such-ref")


def test_build_manifest_unborn_head_raises_manifest_error(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)  # no commit -> HEAD is unborn
    with pytest.raises(m.ManifestError):
        m.build_manifest(repo)


def test_manifest_error_message_is_concise(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    (repo / "a.go").write_text("package a\n")
    _commit(repo)
    with pytest.raises(m.ManifestError) as exc_info:
        m.tree_manifest(repo, "no-such-ref")
    msg = str(exc_info.value)
    assert "git" in msg and "\n" not in msg  # one-line, no dumped traceback


# --- submodules / nested checkouts (#179 review) ------------------------------


def _add_submodule_like(parent: Path, name: str) -> Path:
    """Create a nested git repo at parent/name and gitlink it into the parent's
    index (same 160000 entry a real submodule creates)."""
    sub = parent / name
    _init_repo(sub)
    (sub / "inner.go").write_text("package inner\n")
    _commit(sub, "inner init")
    # `git add <dir-with-.git>` records a gitlink (embedded repo warning is fine).
    _git(parent, "add", name)
    _git(parent, "commit", "-q", "-m", "add gitlink")
    return sub


def test_build_manifest_excludes_submodule_gitlink(tmp_path):
    parent = tmp_path / "p"
    _init_repo(parent)
    (parent / "a.go").write_text("package a\n")
    _commit(parent)
    _add_submodule_like(parent, "sub")

    result = m.build_manifest(parent)
    assert "sub" not in result  # the gitlink itself
    assert not any(p.startswith("sub/") for p in result)  # nor its contents
    assert "a.go" in result


def test_changed_paths_ignores_submodule_pointer_change(tmp_path):
    parent = tmp_path / "p"
    _init_repo(parent)
    (parent / "a.go").write_text("package a\n")
    _commit(parent)
    sub = _add_submodule_like(parent, "sub")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=parent, capture_output=True, text=True, check=True
    ).stdout.strip()

    # Advance the submodule: parent's `git diff` now shows `sub` as modified
    # (a gitlink pointer change) — changed_paths must not surface it or crash.
    (sub / "inner.go").write_text("package inner\n\nfunc g() {}\n")
    _commit(sub, "inner change")

    assert m.changed_paths(parent, base_sha) == set()


def test_walk_source_files_prunes_nested_git_checkouts(tmp_path):
    (tmp_path / "a.go").write_text("package a\n")
    nested = tmp_path / "vendor-repo"
    nested.mkdir()
    (nested / ".git").mkdir()  # nested checkout (.git dir form)
    (nested / "b.go").write_text("package b\n")
    linked = tmp_path / "sub"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: ../.git/modules/sub\n")  # gitlink file form
    (linked / "c.go").write_text("package c\n")

    assert m.walk_source_files(tmp_path) == ["a.go"]
