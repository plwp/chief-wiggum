"""Tests for scripts/diskspace.py: offline, everything under tmp_path."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import time
from pathlib import Path

import diskspace as ds
import pytest


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
                 "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(k, v)


def _write(path: Path, size: int = 4096) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _rows(result: dict) -> dict[str, dict]:
    return {r["path"]: r for r in result["rows"]}


def _scan(root: Path) -> dict:
    return ds.scan([root], min_size=1)


def _repos(tmp_path: Path, root: Path, push: bool) -> tuple[Path, Path]:
    """origin repo, a clone of it (main), and a worktree of main under root/wt."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--initial-branch=main")
    _write(origin / "README", 10)
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "init")
    main = tmp_path / "main"
    subprocess.run(["git", "clone", "-q", str(origin), str(main)], check=True)
    wt = root / "wt"
    root.mkdir(exist_ok=True)
    _git(main, "worktree", "add", "-b", "feat/181-ff", str(wt))
    _write(wt / "change.txt", 10)
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "work")
    if push:
        _git(wt, "push", "-q", "origin", "feat/181-ff")
    return main, wt


def _clean(scan_path: Path, answers: list[str], tmp_path: Path, **kw) -> tuple[int, str]:
    args = argparse.Namespace(from_=scan_path, dry_run=kw.get("dry_run", False),
                              include_ask=kw.get("include_ask", False),
                              trash_dir=str(tmp_path / "Trash" / "diskspace-test"))
    feed = iter(answers)

    def inp(_prompt: str) -> str:
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    out = io.StringIO()
    code = ds.cmd_clean(args, inp=inp, out=out)
    return code, out.getvalue()


def _save(result: dict, tmp_path: Path) -> Path:
    p = tmp_path / "scan.json"
    p.write_text(json.dumps(result))
    return p


def _num(output: str, path: Path) -> str:
    for line in output.splitlines():
        if line.rstrip().endswith(str(path)) or f" {path}   (" in line:
            return line.split()[0]
    raise AssertionError(f"{path} not listed:\n{output}")


# ---------------------------------------------------------------- scan
def test_categorises_a_synthetic_tree(tmp_path):
    root = tmp_path / "root"
    repo = root / "proj"
    (repo / ".git").mkdir(parents=True)
    _write(repo / "package.json", 10)
    _write(repo / "node_modules" / "left-pad" / "index.js")
    _write(root / "nvm" / "lib" / "node_modules" / "npm" / "index.js")
    _write(repo / "src" / "main.py")
    _write(root / "wt" / "file.txt")
    (root / "wt" / ".git").write_text("gitdir: /nonexistent/.git/worktrees/wt\n")
    _write(root / "env" / "pyvenv.cfg", 10)
    _write(root / "env" / "lib" / "site.py")
    _write(root / "stuff" / "go-build" / "ab" / "cache-entry")

    rows = _rows(_scan(root))
    assert rows[str(repo / "node_modules")]["category"] == "build-artefact"
    assert rows[str(repo / "node_modules")]["safety"] == "regenerable"
    assert rows[str(repo)]["category"] == "repo-clone"
    # A global install prefix (no package.json beside it) is not build output.
    assert rows.get(str(root / "nvm" / "lib" / "node_modules"), {}).get("category") != "build-artefact"
    assert rows[str(root / "wt")]["category"] == "git-worktree"
    assert rows[str(root / "wt")]["safety"] == "check"          # broken gitdir is never assumed safe
    assert rows[str(root / "env")]["category"] == "venv"
    assert rows[str(root / "stuff" / "go-build")]["category"] == "cache"
    # Opaque categories are not broken into further rows.
    assert str(repo / "node_modules" / "left-pad") not in rows
    assert str(root / "env" / "lib") not in rows
    # The root is listed for size but marked protected.
    assert rows[str(root)]["protected"] is True


def test_size_is_on_disk_blocks_not_apparent(tmp_path):
    root = tmp_path / "root"
    sparse = root / "vm" / "disk.raw"
    sparse.parent.mkdir(parents=True)
    with open(sparse, "wb") as fh:
        fh.seek(64 * 1024 * 1024)
        fh.write(b"\0")
    st = os.lstat(sparse)
    assert st.st_blocks * 512 < st.st_size  # the filesystem really made it sparse
    row = _rows(_scan(root))[str(root / "vm")]
    assert row["size"] == st.st_blocks * 512 + os.lstat(sparse.parent).st_blocks * 512
    assert row["apparent"] >= st.st_size
    assert row["size"] < 1024 * 1024


def test_category_totals_count_each_byte_once(tmp_path):
    root = tmp_path / "root"
    (root / "proj" / ".git").mkdir(parents=True)
    _write(root / "proj" / "node_modules" / "a.js", 100_000)
    result = _scan(root)
    total_disk = _rows(result)[str(root)]["size"]
    assert sum(result["totals"].values()) == total_disk


# ---------------------------------------------------------------- clean
def test_clean_dry_run_deletes_nothing(tmp_path):
    root = tmp_path / "root"
    nm = root / "proj" / "node_modules"
    (root / "proj" / ".git").mkdir(parents=True)
    _write(root / "proj" / "package.json", 10)
    _write(nm / "a.js")
    code, out = _clean(_save(_scan(root), tmp_path), ["go"], tmp_path, dry_run=True)
    assert code == 0
    assert "Dry run: nothing removed." in out
    assert "delete" in out and str(nm) in out
    assert nm.exists()


def test_regenerable_row_is_removed(tmp_path):
    root = tmp_path / "root"
    nm = root / "proj" / "node_modules"
    (root / "proj" / ".git").mkdir(parents=True)
    _write(root / "proj" / "package.json", 10)
    _write(nm / "a.js")
    code, out = _clean(_save(_scan(root), tmp_path), ["go", "y"], tmp_path)
    assert code == 0, out
    assert not nm.exists()
    assert (root / "proj" / ".git").exists()   # only the selected row went


def test_read_only_tree_is_removed(tmp_path):
    """Go's module cache is chmod 0555 throughout; rmtree must still clear it."""
    root = tmp_path / "root"
    mod = root / "stuff" / "go-build"
    _write(mod / "pkg" / "a.go")
    for d in (mod / "pkg", mod):
        d.chmod(0o555)
    code, out = _clean(_save(_scan(root), tmp_path), ["go", "y"], tmp_path)
    assert code == 0, out
    assert "FAILED" not in out
    assert not mod.exists()


def test_heavy_model_cache_is_check_not_preselected(tmp_path, monkeypatch):
    root = tmp_path / "root"
    whisper = root / ".cache" / "whisper"
    monkeypatch.setattr(ds, "CACHE_PARENTS", (root / ".cache",))
    monkeypatch.setattr(ds, "HEAVY_CACHES", {whisper: "whisper models used by /transcribe"})
    _write(whisper / "large-v3.pt")
    _write(root / ".cache" / "pip" / "wheel")
    rows = _rows(_scan(root))
    assert rows[str(whisper)]["category"] == "cache"
    assert rows[str(whisper)]["safety"] == "check"
    assert rows[str(root / ".cache" / "pip")]["safety"] == "regenerable"


def test_check_row_is_skipped_unless_selected(tmp_path):
    root = tmp_path / "root"
    data = root / "notes"
    _write(data / "thesis.txt")
    scan_path = _save(_scan(root), tmp_path)
    assert _rows(json.loads(scan_path.read_text()))[str(data)]["safety"] == "check"

    _code, out = _clean(scan_path, ["go", "y"], tmp_path)
    assert data.exists()

    num = _num(out, data)
    code, out = _clean(scan_path, [num, "go", "y"], tmp_path)
    assert code == 0, out
    assert not data.exists()
    # A check row is moved to the Trash, never deleted outright.
    trashed = list((tmp_path / "Trash" / "diskspace-test").iterdir())
    assert len(trashed) == 1 and (trashed[0] / "thesis.txt").exists()


def test_ask_rows_need_include_ask(tmp_path, monkeypatch):
    root = tmp_path / "root"
    docs = root / "Documents"
    monkeypatch.setattr(ds, "ASK_PARENTS", (docs,))
    _write(docs / "taxes" / "2026.pdf")
    scan_path = _save(_scan(root), tmp_path)
    taxes = docs / "taxes"
    assert _rows(json.loads(scan_path.read_text()))[str(taxes)]["safety"] == "ask"

    _code, out = _clean(scan_path, ["go"], tmp_path, dry_run=True)
    num = _num(out, taxes)
    _code, out = _clean(scan_path, [num, "go", "y"], tmp_path)
    assert "--include-ask" in out
    assert taxes.exists()

    code, out = _clean(scan_path, [num, "go", "y"], tmp_path, include_ask=True)
    assert code == 0, out
    assert not taxes.exists()


def test_nothing_outside_the_roots_is_touched(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "elsewhere" / "node_modules"
    _write(outside / "a.js")
    _write(root / "keep.txt")
    result = _scan(root)
    result["rows"] = [{"path": str(outside), "size": 1, "apparent": 1, "own": 1, "mtime": 0,
                       "category": "build-artefact", "safety": "regenerable", "reason": "",
                       "root": str(root), "git": {}, "worktrees": [], "protected": False}]
    code, out = _clean(_save(result, tmp_path), ["go", "y"], tmp_path)
    assert "outside the scanned roots" in out
    assert outside.exists()


# ---------------------------------------------------------------- worktrees
def test_worktree_with_unpushed_commits_is_check_with_reason(tmp_path):
    root = tmp_path / "root"
    _main, wt = _repos(tmp_path, root, push=False)
    row = _rows(_scan(root))[str(wt)]
    assert row["category"] == "git-worktree"
    assert row["safety"] == "check"
    assert "feat/181-ff not pushed" in row["reason"]
    assert row["git"]["branch"] == "feat/181-ff"


def test_unpushed_worktree_is_refused_when_not_an_explicit_check_selection(tmp_path):
    root = tmp_path / "root"
    main, wt = _repos(tmp_path, root, push=False)
    result = _scan(root)
    # Simulate a scan that saw it clean: state is re-read at delete time.
    for r in result["rows"]:
        if r["path"] == str(wt):
            r["safety"] = "regenerable"
    code, out = _clean(_save(result, tmp_path), ["go", "y"], tmp_path)
    assert "not pushed" in out and "SKIP" in out
    assert wt.exists()
    assert str(wt) in _git(main, "worktree", "list")


def test_unpushed_worktree_selected_under_check_is_trashed_and_unregistered(tmp_path):
    root = tmp_path / "root"
    main, wt = _repos(tmp_path, root, push=False)
    scan_path = _save(_scan(root), tmp_path)
    _code, out = _clean(scan_path, ["go"], tmp_path, dry_run=True)
    code, out = _clean(scan_path, [_num(out, wt), "go", "y"], tmp_path)
    assert code == 0, out
    assert not wt.exists()
    assert str(wt) not in _git(main, "worktree", "list")
    # The commits survive on the branch in the main repo.
    assert "work" in _git(main, "log", "--oneline", "feat/181-ff")


def _age_checkout(wt: Path, hours: float) -> None:
    gitdir = Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-dir").strip())
    old = time.time() - hours * 3600
    for p in (wt, gitdir / "index", gitdir / "HEAD", gitdir / "logs" / "HEAD"):
        if p.exists():
            os.utime(p, (old, old))


def test_recently_active_clean_worktree_is_check(tmp_path):
    root = tmp_path / "root"
    _main, wt = _repos(tmp_path, root, push=True)
    row = _rows(_scan(root))[str(wt)]
    assert row["safety"] == "check"
    assert "in use" in row["reason"]


def test_pushed_clean_worktree_is_regenerable_and_removed_via_git(tmp_path):
    root = tmp_path / "root"
    main, wt = _repos(tmp_path, root, push=True)
    _age_checkout(wt, 48)
    row = _rows(_scan(root))[str(wt)]
    assert row["safety"] == "regenerable", row["reason"]
    code, out = _clean(_save(_scan(root), tmp_path), ["go", "y"], tmp_path)
    assert code == 0, out
    assert not wt.exists()
    assert str(wt) not in _git(main, "worktree", "list")


def test_tmp_dir_containing_unpushed_worktree_is_downgraded(tmp_path, monkeypatch):
    root = tmp_path / "root"
    monkeypatch.setattr(ds, "SESSION_TMP_PARENTS", (root / "tmp",))
    main, _ = _repos(tmp_path, root, push=False)
    session = root / "tmp" / "old-session"
    _git(main, "worktree", "add", "-b", "scratch", str(session / "wt2"))
    _write(session / "wt2" / "x", 10)
    _git(session / "wt2", "add", ".")
    _git(session / "wt2", "commit", "-m", "scratch work")
    _age_checkout(session / "wt2", 48)
    os.utime(session, (0, 0))
    row = _rows(_scan(root))[str(session)]
    assert row["category"] == "session-tmp"
    assert row["safety"] == "check"
    assert "contains worktree" in row["reason"]


# ---------------------------------------------------------------- docker
def test_docker_hung_engine_is_reported_not_pretended(tmp_path):
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    out = io.StringIO()
    code = ds.cmd_docker(None, inp=lambda _p: "y", out=out, run=run, raw=tmp_path / "Docker.raw")
    assert code == 1
    assert "hung" in out.getvalue() and "restart Docker Desktop" in out.getvalue()
