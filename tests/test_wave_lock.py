"""Tests for the #245 advisory per-repo wave lock (staging/promote phase).

Concurrent `/implement-wave` orchestrators against the SAME target repo share
one cached checkout (`~/.chief-wiggum/repos/<owner>/<repo>`) for the
merge/staging/promote phase — worker isolation (each ticket in its own
worktree) does not cover this phase, which `cd`s into the shared checkout
directly. Confirmed live (2026-08-02, dogeared-coach): a second wave's
staging merge hit `assert-main-pristine` mid-conflict from a FIRST wave's
in-flight staging merge on the same checkout.

The lock is advisory (a sibling `<repo>.wave-lock` file, never inside the
git repo itself) and fails LOUDLY on contention — never silently shares
state — per the #289 doctrine every one of this batch's fixes follows.
"""

from __future__ import annotations

import json
import os

import pytest
import wave_lock
from chief_wiggum import repo_lock

# --- domain module: chief_wiggum.repo_lock ----------------------------------


def test_acquire_creates_lock_file_sibling_to_repo_not_inside_it(tmp_path):
    repo = tmp_path / "repos" / "acme" / "app"
    repo.mkdir(parents=True)
    lock = repo_lock.acquire(str(repo), "session-1")
    assert lock.path == tmp_path / "repos" / "acme" / "app.wave-lock"
    assert lock.path.exists()
    # Never inside the repo tree itself — must never ride in a worker's diff.
    assert not str(lock.path).startswith(str(repo) + os.sep)


def test_acquire_records_session_pid_and_wave(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    lock = repo_lock.acquire(str(repo), "session-1", wave="2", pid=4242)
    data = json.loads(lock.path.read_text())
    assert data["session_id"] == "session-1"
    assert data["pid"] == 4242
    assert data["wave"] == "2"
    assert "acquired_at" in data


def test_acquire_same_session_is_idempotent(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    first = repo_lock.acquire(str(repo), "session-1", pid=100)
    second = repo_lock.acquire(str(repo), "session-1", pid=100)
    assert second.session_id == first.session_id


def test_acquire_fails_loudly_when_held_by_a_different_live_session(tmp_path, monkeypatch):
    repo = tmp_path / "app"
    repo.mkdir()
    monkeypatch.setattr(repo_lock, "_pid_alive", lambda pid: True)
    repo_lock.acquire(str(repo), "session-A", pid=111)
    with pytest.raises(repo_lock.WaveLockError) as excinfo:
        repo_lock.acquire(str(repo), "session-B", pid=222)
    # The second run must be able to tell it is contending — who, not just that.
    assert "session-A" in str(excinfo.value)
    assert "111" in str(excinfo.value)


def test_acquire_reclaims_a_lock_whose_holder_pid_is_dead(tmp_path, monkeypatch):
    repo = tmp_path / "app"
    repo.mkdir()
    monkeypatch.setattr(repo_lock, "_pid_alive", lambda pid: False)
    repo_lock.acquire(str(repo), "session-A", pid=111)
    # A crashed orchestrator must not wedge the repo forever.
    lock = repo_lock.acquire(str(repo), "session-B", pid=222)
    assert lock.session_id == "session-B"
    data = json.loads(lock.path.read_text())
    assert data["session_id"] == "session-B"


def test_release_removes_the_lock_file(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    lock = repo_lock.acquire(str(repo), "session-1")
    assert repo_lock.release(str(repo), "session-1") is True
    assert not lock.path.exists()


def test_release_when_not_locked_returns_false(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    assert repo_lock.release(str(repo), "session-1") is False


def test_release_refuses_to_drop_another_live_sessions_lock(tmp_path, monkeypatch):
    repo = tmp_path / "app"
    repo.mkdir()
    monkeypatch.setattr(repo_lock, "_pid_alive", lambda pid: True)
    lock = repo_lock.acquire(str(repo), "session-A", pid=111)
    with pytest.raises(repo_lock.WaveLockError):
        repo_lock.release(str(repo), "session-B")
    assert lock.path.exists()


def test_release_force_drops_another_sessions_lock(tmp_path, monkeypatch):
    repo = tmp_path / "app"
    repo.mkdir()
    monkeypatch.setattr(repo_lock, "_pid_alive", lambda pid: True)
    lock = repo_lock.acquire(str(repo), "session-A", pid=111)
    assert repo_lock.release(str(repo), "session-B", force=True) is True
    assert not lock.path.exists()


def test_status_none_when_unlocked(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    assert repo_lock.status(str(repo)) is None


def test_status_reports_live_holder(tmp_path, monkeypatch):
    repo = tmp_path / "app"
    repo.mkdir()
    monkeypatch.setattr(repo_lock, "_pid_alive", lambda pid: True)
    repo_lock.acquire(str(repo), "session-A", pid=111, wave="3")
    info = repo_lock.status(str(repo))
    assert info["session_id"] == "session-A"
    assert info["wave"] == "3"
    assert info["live"] is True


def test_status_reports_dead_holder_as_not_live(tmp_path, monkeypatch):
    repo = tmp_path / "app"
    repo.mkdir()
    monkeypatch.setattr(repo_lock, "_pid_alive", lambda pid: True)
    repo_lock.acquire(str(repo), "session-A", pid=111)
    monkeypatch.setattr(repo_lock, "_pid_alive", lambda pid: False)
    info = repo_lock.status(str(repo))
    assert info["live"] is False


# --- CLI: scripts/wave_lock.py -----------------------------------------------


def test_cli_acquire_ok(tmp_path, capsys):
    repo = tmp_path / "app"
    repo.mkdir()
    rc = wave_lock.main(["acquire", "--repo", str(repo), "--session", "s1"])
    assert rc == 0
    assert "OK" in capsys.readouterr().out


def test_cli_acquire_contention_exits_nonzero_and_names_the_holder(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "app"
    repo.mkdir()
    monkeypatch.setattr(repo_lock, "_pid_alive", lambda pid: True)
    assert wave_lock.main(["acquire", "--repo", str(repo), "--session", "s1", "--pid", "111"]) == 0
    rc = wave_lock.main(["acquire", "--repo", str(repo), "--session", "s2", "--pid", "222"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "s1" in err


def test_cli_release_ok(tmp_path, capsys):
    repo = tmp_path / "app"
    repo.mkdir()
    wave_lock.main(["acquire", "--repo", str(repo), "--session", "s1"])
    rc = wave_lock.main(["release", "--repo", str(repo), "--session", "s1"])
    assert rc == 0
    assert "OK" in capsys.readouterr().out


def test_cli_status_reports_locked_false_when_unlocked(tmp_path, capsys):
    repo = tmp_path / "app"
    repo.mkdir()
    rc = wave_lock.main(["status", "--repo", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["locked"] is False


def test_cli_status_reports_locked_true_with_holder_info(tmp_path, capsys):
    repo = tmp_path / "app"
    repo.mkdir()
    wave_lock.main(["acquire", "--repo", str(repo), "--session", "s1"])
    capsys.readouterr()  # discard the "acquire" command's own stdout
    rc = wave_lock.main(["status", "--repo", str(repo)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["locked"] is True
    assert out["session_id"] == "s1"
