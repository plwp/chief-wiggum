"""Tests for quality/cache.py (#328): the SHA-keyed on-disk result cache for
immutable quality-engine inputs.

``tests/conftest.py``'s ``isolate_quality_cache`` autouse fixture points
``CW_QUALITY_CACHE_DIR`` at a per-test tmp dir, so these tests never touch the
operator's real ``~/.chief-wiggum/cache/quality``.
"""

from __future__ import annotations

import subprocess

from quality import cache


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _make_repo(tmp_path, files: dict[str, str]):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.name", "Ada")
    _git(repo, "config", "user.email", "ada@example.com")
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


# --- load/store roundtrip ----------------------------------------------------


def test_store_then_load_roundtrips(tmp_path):
    repo = str(tmp_path / "some-repo")
    cache.store(repo, "trend", "abc123", {"src_loc": 42})
    assert cache.load(repo, "trend", "abc123") == {"src_loc": 42}


def test_load_is_a_miss_when_nothing_stored(tmp_path):
    assert cache.load(str(tmp_path), "trend", "nope") is None


def test_load_tolerates_corrupt_cache_file(tmp_path):
    repo = str(tmp_path / "r")
    path = cache._entry_path(repo, "trend", "k")
    path.write_text("{not json")
    assert cache.load(repo, "trend", "k") is None


def test_different_engines_and_keys_do_not_collide(tmp_path):
    repo = str(tmp_path / "r")
    cache.store(repo, "trend", "k1", {"v": 1})
    cache.store(repo, "trend", "k2", {"v": 2})
    cache.store(repo, "jscpd", "k1", {"v": 3})
    assert cache.load(repo, "trend", "k1") == {"v": 1}
    assert cache.load(repo, "trend", "k2") == {"v": 2}
    assert cache.load(repo, "jscpd", "k1") == {"v": 3}


def test_different_repos_do_not_collide(tmp_path):
    r1, r2 = str(tmp_path / "one"), str(tmp_path / "two")
    cache.store(r1, "trend", "k", {"v": "one"})
    cache.store(r2, "trend", "k", {"v": "two"})
    assert cache.load(r1, "trend", "k") == {"v": "one"}
    assert cache.load(r2, "trend", "k") == {"v": "two"}


# --- --no-cache escape hatch --------------------------------------------------


def test_no_cache_env_disables_both_read_and_write(tmp_path, monkeypatch):
    repo = str(tmp_path / "r")
    cache.store(repo, "trend", "k", {"v": 1})  # written before the flag is set
    monkeypatch.setenv(cache.NO_CACHE_ENV, "1")
    assert cache.load(repo, "trend", "k") is None  # read suppressed
    cache.store(repo, "trend", "k2", {"v": 2})
    monkeypatch.delenv(cache.NO_CACHE_ENV)
    assert cache.load(repo, "trend", "k2") is None  # write was suppressed too


# --- manifest_key: content-hash correctness under a dirty worktree ----------


def test_manifest_key_stable_when_content_unchanged(tmp_path):
    repo = _make_repo(tmp_path, {"a.py": "x = 1\n"})
    k1 = cache.manifest_key(str(repo), None)
    k2 = cache.manifest_key(str(repo), None)
    assert k1 is not None
    assert k1 == k2


def test_manifest_key_changes_on_dirty_edit_not_just_commit(tmp_path):
    """The doctrine: a cache keyed on HEAD alone would silently reuse a stale
    jscpd result for an uncommitted edit. manifest_key must bust on dirty
    content, not just a new commit."""
    repo = _make_repo(tmp_path, {"a.py": "x = 1\n"})
    before = cache.manifest_key(str(repo), None)
    (repo / "a.py").write_text("x = 2\n")  # dirty, uncommitted
    after = cache.manifest_key(str(repo), None)
    assert before != after


def test_manifest_key_changes_on_untracked_file_added(tmp_path):
    repo = _make_repo(tmp_path, {"a.py": "x = 1\n"})
    before = cache.manifest_key(str(repo), None)
    (repo / "b.py").write_text("y = 2\n")  # untracked
    after = cache.manifest_key(str(repo), None)
    assert before != after


def test_manifest_key_scoped_to_explicit_file_list(tmp_path):
    """Passing an explicit corpus restricts the hash to those files — a
    change OUTSIDE the corpus must not bust the key."""
    repo = _make_repo(tmp_path, {"in/a.py": "x = 1\n", "out/b.py": "y = 1\n"})
    before = cache.manifest_key(str(repo), ["in/a.py"])
    (repo / "out" / "b.py").write_text("y = 2\n")
    after = cache.manifest_key(str(repo), ["in/a.py"])
    assert before == after


def test_manifest_key_none_outside_git_repo(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert cache.manifest_key(str(not_a_repo), None) is None


# --- head_sha ------------------------------------------------------------------


def test_head_sha_returns_current_commit(tmp_path):
    repo = _make_repo(tmp_path, {"a.py": "x = 1\n"})
    sha = cache.head_sha(str(repo))
    assert sha is not None
    expected = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert sha == expected


def test_head_sha_none_outside_git_repo(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert cache.head_sha(str(not_a_repo)) is None


def test_head_sha_stable_across_a_dirty_edit(tmp_path):
    """A dirty, UNCOMMITTED edit must not change head_sha — it is the correct
    cache key only for engines (git-of-theseus) that never read the working
    tree, so this must reflect the last COMMIT only."""
    repo = _make_repo(tmp_path, {"a.py": "x = 1\n"})
    before = cache.head_sha(str(repo))
    (repo / "a.py").write_text("x = 2\n")
    after = cache.head_sha(str(repo))
    assert before == after
