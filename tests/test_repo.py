import os
import subprocess
import sys
import time

import pytest
import repo


def test_parse_owner_repo_accepts_issue_suffix():
    assert repo._parse_owner_repo("acme/widget-api#42") == ("acme", "widget-api")


@pytest.mark.parametrize(
    "value",
    [
        "acme",
        "acme/../repo",
        "acme/repo/extra",
        "acme/repo name",
        "/acme/repo",
    ],
)
def test_parse_owner_repo_rejects_unsafe_values(value):
    with pytest.raises(SystemExit):
        repo._parse_owner_repo(value)


# --- clone timeout: inactivity, not a fixed wall-clock ceiling (chief-wiggum#268) --
#
# A 776MB/12,500-file repo legitimately took several minutes against the OLD
# fixed 120s `subprocess.run(timeout=120)` ceiling. The fix (AC1) must judge a
# clone by PROGRESS, not elapsed wall-clock time: killed only after
# `inactivity_timeout` seconds with NO output at all. Waiting a literal 120+
# real seconds in a unit test is impractical, so these tests exercise the
# mechanism directly with a small `inactivity_timeout`, proving TOTAL elapsed
# time is allowed to exceed it as long as there is periodic activity — the
# same shape as the real bug, scaled down for test speed.


def test_clone_survives_total_duration_longer_than_inactivity_timeout():
    # 5 ticks x 0.2s = ~1.0s total, each tick well inside inactivity_timeout=0.5s.
    # Total elapsed (~1.0s) EXCEEDS inactivity_timeout (0.5s) — proving there is
    # no fixed ceiling equal to (or derived from) inactivity_timeout: only a
    # gap with NO output at all would kill it.
    cmd = ["sh", "-c", "for i in 1 2 3 4 5; do echo tick; sleep 0.2; done"]
    repo._clone_with_inactivity_timeout(cmd, inactivity_timeout=0.5)


def test_clone_raises_on_genuine_inactivity():
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        repo._clone_with_inactivity_timeout(
            ["sh", "-c", "sleep 5"], inactivity_timeout=0.3,
        )
    elapsed = time.monotonic() - start
    assert elapsed < 3, f"did not fire promptly on stall ({elapsed:.1f}s)"


def test_clone_raises_calledprocesserror_on_nonzero_exit():
    with pytest.raises(subprocess.CalledProcessError):
        repo._clone_with_inactivity_timeout(
            ["sh", "-c", "echo hi; exit 3"], inactivity_timeout=5,
        )


def test_clone_kills_whole_process_group_not_just_direct_child(tmp_path):
    # A grandchild inherited from a forked clone process must ALSO die — not
    # just the direct child — or the `git` process (and anything it spawned)
    # is orphaned and keeps writing to the cache path after the caller has
    # been told the clone failed (#268 AC3). Mirrors the process-group fix
    # already proven in consult_ai.py's _kill_group / _run_capture.
    script = tmp_path / "hang.py"
    pidfile = tmp_path / "pids.txt"
    script.write_text(f"""\
import os, sys, time
child = os.fork()
if child == 0:
    time.sleep(30)
    sys.exit(0)
with open({str(pidfile)!r}, "w") as f:
    f.write(f"{{os.getpid()}} {{child}}\\n")
time.sleep(30)
""")
    with pytest.raises(subprocess.TimeoutExpired):
        repo._clone_with_inactivity_timeout(
            [sys.executable, str(script)], inactivity_timeout=0.3,
        )
    # give SIGKILL a beat to land
    deadline = time.monotonic() + 5
    pids = None
    while time.monotonic() < deadline:
        if pidfile.exists() and pidfile.read_text().strip():
            pids = [int(p) for p in pidfile.read_text().split()]
            break
        time.sleep(0.1)
    assert pids, "child never wrote its pidfile"
    time.sleep(0.5)
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_clone_never_hangs_on_surviving_grandchild_holding_pipe_open():
    # subprocess.run(timeout=) only kills the DIRECT child; if a grandchild
    # inherits the stdout pipe, a naive communicate()-based drain blocks for
    # the grandchild's full lifetime. The whole-group kill must return
    # promptly regardless.
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        repo._clone_with_inactivity_timeout(
            ["sh", "-c", "sleep 30 & sleep 30"],
            inactivity_timeout=0.3,
        )
    elapsed = time.monotonic() - start
    assert elapsed < 10, f"timeout did not return promptly ({elapsed:.1f}s)"


# --- resolve_repo: no partial/interrupted clone reads as a valid cache hit -------


def test_interrupted_clone_leaves_no_directory_at_final_cache_path(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "CACHE_DIR", tmp_path / "cache")

    def fake_run(cmd, **kwargs):
        # the "already inside repo" / "cached, pull" probes — force straight
        # to the clone path.
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)

    def fake_clone(cmd, **kwargs):
        # cmd = ["gh", "repo", "clone", owner_repo, str(tmp_target)] — write
        # SOMETHING into the in-flight temp target, then fail partway through,
        # simulating a killed/interrupted clone.
        tmp_target = cmd[-1]
        with open(os.path.join(tmp_target, "partial-file"), "w") as f:
            f.write("junk")
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(repo, "_clone_with_inactivity_timeout", fake_clone)

    with pytest.raises(subprocess.CalledProcessError):
        repo.resolve_repo("acme/widget")

    cached = tmp_path / "cache" / "acme" / "widget"
    # AC2: no directory at the FINAL cache path — a later resolve_repo call's
    # `cached.exists() and (cached / ".git").exists()` cache-hit check must
    # see nothing here.
    assert not cached.exists()
    owner_dir = tmp_path / "cache" / "acme"
    if owner_dir.exists():
        assert list(owner_dir.iterdir()) == [], "a partial temp clone dir was left behind"


def test_successful_clone_lands_at_final_cache_path(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "CACHE_DIR", tmp_path / "cache")

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)

    def fake_clone(cmd, **kwargs):
        tmp_target = cmd[-1]
        os.makedirs(os.path.join(tmp_target, ".git"), exist_ok=True)

    monkeypatch.setattr(repo, "_clone_with_inactivity_timeout", fake_clone)

    result = repo.resolve_repo("acme/widget")

    cached = tmp_path / "cache" / "acme" / "widget"
    assert result == cached
    assert (cached / ".git").exists()
    owner_dir = tmp_path / "cache" / "acme"
    # only the final directory remains — no leftover temp sibling
    assert list(owner_dir.iterdir()) == [cached]


def test_resolve_repo_clone_has_no_fixed_timeout_kwarg(tmp_path, monkeypatch):
    """The OLD bug: `subprocess.run(["gh", "repo", "clone", ...], timeout=120)`.
    `resolve_repo` must route the clone through `_clone_with_inactivity_timeout`
    instead of a bare `subprocess.run(timeout=...)` call, so a large clone is
    judged on inactivity, not a fixed ceiling."""
    monkeypatch.setattr(repo, "CACHE_DIR", tmp_path / "cache")

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)

    captured = {}

    def fake_clone(cmd, **kwargs):
        captured["cmd"] = cmd
        tmp_target = cmd[-1]
        os.makedirs(os.path.join(tmp_target, ".git"), exist_ok=True)

    monkeypatch.setattr(repo, "_clone_with_inactivity_timeout", fake_clone)

    repo.resolve_repo("acme/widget")
    assert captured["cmd"][:3] == ["gh", "repo", "clone"]
    assert captured["cmd"][3] == "acme/widget"
