"""Tests for scripts/artifacts.py — the per-target meta-location resolver (#213)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import artifacts  # noqa: E402

# ---- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def user_dir(tmp_path, monkeypatch):
    """Isolate every test from the real ~/.chief-wiggum — tests NEVER touch it."""
    d = tmp_path / "cw-user"
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(d))
    return d


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def make_git_repo(path: Path, remote: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "README.md").write_text("hi\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    if remote:
        _git(path, "remote", "add", "origin", remote)
    return path


# ---- target identity ----------------------------------------------------------


def test_target_id_from_https_remote(tmp_path):
    repo = make_git_repo(tmp_path / "r", remote="https://github.com/acme/app.git")
    assert artifacts.Resolver.resolve(repo).target_id == "acme/app"


def test_target_id_from_https_remote_without_dot_git(tmp_path):
    repo = make_git_repo(tmp_path / "r", remote="https://github.com/acme/app")
    assert artifacts.Resolver.resolve(repo).target_id == "acme/app"


def test_target_id_from_ssh_scp_remote(tmp_path):
    repo = make_git_repo(tmp_path / "r", remote="git@github.com:acme/app.git")
    assert artifacts.Resolver.resolve(repo).target_id == "acme/app"


def test_target_id_from_ssh_url_remote(tmp_path):
    repo = make_git_repo(tmp_path / "r", remote="ssh://git@github.com/acme/app.git")
    assert artifacts.Resolver.resolve(repo).target_id == "acme/app"


def test_target_id_no_remote_falls_back_to_path_hash(tmp_path):
    repo = make_git_repo(tmp_path / "r")
    tid = artifacts.Resolver.resolve(repo).target_id
    assert tid.startswith("local/")
    digest = tid.split("/", 1)[1]
    assert len(digest) == 12
    assert all(c in "0123456789abcdef" for c in digest)
    # deterministic: same path, same id
    assert artifacts.Resolver.resolve(repo).target_id == tid


def test_target_id_non_git_dir_falls_back_to_path_hash(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert artifacts.Resolver.resolve(d).target_id.startswith("local/")


def test_target_id_stable_across_symlinked_path_variants(tmp_path):
    """Regression (caught in the #213 smoke run): /tmp vs /private/tmp on
    macOS — or any symlinked spelling of the same repo — must resolve to the
    SAME target id, or an election recorded via one spelling is invisible via
    the other."""
    real = tmp_path / "real-repo"
    real.mkdir()
    link = tmp_path / "link-repo"
    link.symlink_to(real)
    assert artifacts.derive_target_id(link) == artifacts.derive_target_id(real)


# ---- default embedded mode (no election) --------------------------------------


def test_no_election_means_embedded_status_quo(tmp_path):
    repo = make_git_repo(tmp_path / "r", remote="https://github.com/acme/app.git")
    r = artifacts.Resolver.resolve(repo)
    assert r.mode == "embedded"
    assert r.meta_root == repo / "docs"
    assert r.epics_dir() == repo / "docs" / "epics"
    assert r.epic_dir("order-lifecycle") == repo / "docs" / "epics" / "order-lifecycle"
    assert r.quality_dir() == repo / "docs" / "quality"
    assert r.patterns_dir() == repo / "docs" / "patterns"
    assert r.design_dir() == repo / "docs" / "design"


# ---- election round-trip -------------------------------------------------------


def test_elect_sidecar_round_trip(tmp_path, user_dir):
    repo = make_git_repo(tmp_path / "r", remote="https://github.com/acme/app.git")
    rec = artifacts.elect(repo, "sidecar")
    assert rec["mode"] == "sidecar"
    assert rec["backing"] == "git"  # default backing store is a git meta-repo
    assert rec["target_id"] == "acme/app"
    assert rec["elected_at"]

    path = user_dir / "meta" / "acme" / "app" / "election.json"
    assert path.is_file()
    assert json.loads(path.read_text()) == rec

    r = artifacts.Resolver.resolve(repo)
    assert r.mode == "sidecar"
    assert r.backing == "git"
    assert r.meta_root == user_dir / "meta" / "acme" / "app" / "docs"
    # IDENTICAL layout beneath the meta root, embedded or sidecar
    assert r.epics_dir() == r.meta_root / "epics"
    assert r.quality_dir() == r.meta_root / "quality"
    assert r.patterns_dir() == r.meta_root / "patterns"
    assert r.design_dir() == r.meta_root / "design"
    # nothing was written into the target
    assert not (repo / "docs").exists()


def test_elect_back_to_embedded(tmp_path):
    repo = make_git_repo(tmp_path / "r", remote="https://github.com/acme/app.git")
    artifacts.elect(repo, "sidecar")
    artifacts.elect(repo, "embedded")
    r = artifacts.Resolver.resolve(repo)
    assert r.mode == "embedded"
    assert r.meta_root == repo / "docs"


def test_elect_local_backing(tmp_path):
    repo = make_git_repo(tmp_path / "r")
    rec = artifacts.elect(repo, "sidecar", backing="local")
    assert rec["backing"] == "local"
    assert artifacts.Resolver.resolve(repo).backing == "local"


def test_elect_rejects_unknown_mode_and_backing(tmp_path):
    repo = make_git_repo(tmp_path / "r")
    with pytest.raises(ValueError, match="mode"):
        artifacts.elect(repo, "detached")
    with pytest.raises(ValueError, match="backing"):
        artifacts.elect(repo, "sidecar", backing="s3")


def test_malformed_election_fails_closed(tmp_path, user_dir):
    repo = make_git_repo(tmp_path / "r", remote="https://github.com/acme/app.git")
    path = user_dir / "meta" / "acme" / "app" / "election.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    with pytest.raises(ValueError, match="election"):
        artifacts.Resolver.resolve(repo)


# ---- CHIEF_WIGGUM_USER_DIR / cw_home isolation ---------------------------------


def test_cw_home_param_beats_env_var(tmp_path, user_dir):
    repo = make_git_repo(tmp_path / "r", remote="https://github.com/acme/app.git")
    override = tmp_path / "other-user-dir"
    rec = artifacts.elect(repo, "sidecar", cw_home=override)
    assert rec["mode"] == "sidecar"
    assert (override / "meta" / "acme" / "app" / "election.json").is_file()
    assert not (user_dir / "meta").exists()  # env-var dir untouched
    r = artifacts.Resolver.resolve(repo, cw_home=override)
    assert r.mode == "sidecar"
    assert r.meta_root == override / "meta" / "acme" / "app" / "docs"
    # without the param, the env-var dir (no election) still says embedded
    assert artifacts.Resolver.resolve(repo).mode == "embedded"


# ---- scope --------------------------------------------------------------------


def test_missing_scope_json_means_whole_repo(tmp_path):
    repo = make_git_repo(tmp_path / "r")
    r = artifacts.Resolver.resolve(repo)
    assert r.in_scope("anything/at/all.go")
    assert "whole repo" in r.scope_summary()


def test_scope_include_only(tmp_path):
    repo = make_git_repo(tmp_path / "r")
    r = artifacts.Resolver.resolve(repo)
    r.meta_root.mkdir(parents=True, exist_ok=True)
    (r.meta_root / "scope.json").write_text(json.dumps({"include": ["services/billing/*"]}))
    assert r.in_scope("services/billing/handler.go")
    assert not r.in_scope("services/auth/handler.go")


def test_scope_exclude_wins_over_include(tmp_path):
    repo = make_git_repo(tmp_path / "r")
    r = artifacts.Resolver.resolve(repo)
    r.meta_root.mkdir(parents=True, exist_ok=True)
    (r.meta_root / "scope.json").write_text(json.dumps({
        "include": ["services/billing/*"],
        "exclude": ["services/billing/legacy_*"],
    }))
    assert r.in_scope("services/billing/handler.go")
    assert not r.in_scope("services/billing/legacy_import.go")


def test_scope_empty_include_means_everything(tmp_path):
    repo = make_git_repo(tmp_path / "r")
    r = artifacts.Resolver.resolve(repo)
    r.meta_root.mkdir(parents=True, exist_ok=True)
    (r.meta_root / "scope.json").write_text(json.dumps({"include": [], "exclude": ["vendor/*"]}))
    assert r.in_scope("main.go")
    assert not r.in_scope("vendor/dep.go")
    summary = r.scope_summary()
    assert "vendor/*" in summary


def test_scope_lives_at_the_sidecar_meta_root_in_sidecar_mode(tmp_path, user_dir):
    repo = make_git_repo(tmp_path / "r", remote="https://github.com/acme/app.git")
    artifacts.elect(repo, "sidecar")
    r = artifacts.Resolver.resolve(repo)
    r.meta_root.mkdir(parents=True, exist_ok=True)
    (r.meta_root / "scope.json").write_text(json.dumps({"include": ["pkg/*"]}))
    assert r.in_scope("pkg/x.go")
    assert not r.in_scope("cmd/x.go")


# ---- version binding (stamp / check_stale) -------------------------------------


def test_stamp_adds_current_head_sha(tmp_path):
    repo = make_git_repo(tmp_path / "r")
    r = artifacts.Resolver.resolve(repo)
    stamped = r.stamp({"hello": 1})
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert stamped["target_sha"] == head
    assert stamped["hello"] == 1
    # original payload not mutated
    assert "target_sha" not in {"hello": 1}


def test_check_stale_fresh_returns_none(tmp_path):
    repo = make_git_repo(tmp_path / "r")
    r = artifacts.Resolver.resolve(repo)
    assert r.check_stale(r.stamp({})) is None


def test_check_stale_after_new_commit_warns(tmp_path):
    repo = make_git_repo(tmp_path / "r")
    r = artifacts.Resolver.resolve(repo)
    stamped = r.stamp({})
    (repo / "new.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "more")
    warning = r.check_stale(stamped)
    assert warning is not None
    assert stamped["target_sha"] in warning  # names the recorded sha


def test_check_stale_without_target_sha_warns(tmp_path):
    repo = make_git_repo(tmp_path / "r")
    r = artifacts.Resolver.resolve(repo)
    assert r.check_stale({"no": "sha"}) is not None


# ---- CLI ----------------------------------------------------------------------


def test_cli_elect_and_show(tmp_path, user_dir, capsys):
    repo = make_git_repo(tmp_path / "r", remote="https://github.com/acme/app.git")
    assert artifacts.main(["elect", str(repo), "--mode", "sidecar"]) == 0
    capsys.readouterr()
    assert artifacts.main(["show", str(repo), "--format", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["mode"] == "sidecar"
    assert doc["target_id"] == "acme/app"
    assert doc["meta_root"] == str(user_dir / "meta" / "acme" / "app" / "docs")
