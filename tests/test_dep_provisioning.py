"""Tests for shared dependency-cache provisioning (#329).

`/implement-wave` runs workers concurrently in separate worktrees. Symlinking
a shared node_modules/.venv across them (the /implement single-ticket rule)
is unsafe under concurrency — these tests pin the safe replacement: env vars
pointing each ecosystem's package manager at a shared, concurrency-safe
CACHE (never the installed tree), plus the CLI that emits them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import dep_cache
import pytest
from chief_wiggum import dep_provisioning

# --- ecosystem detection -----------------------------------------------------


def test_detect_npm(tmp_path):
    (tmp_path / "package-lock.json").write_text("{}")
    assert dep_provisioning.detect_ecosystems(tmp_path) == ["npm"]


def test_detect_pnpm(tmp_path):
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert dep_provisioning.detect_ecosystems(tmp_path) == ["pnpm"]


def test_detect_yarn(tmp_path):
    (tmp_path / "yarn.lock").write_text("")
    assert dep_provisioning.detect_ecosystems(tmp_path) == ["yarn"]


def test_detect_pip_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert dep_provisioning.detect_ecosystems(tmp_path) == ["pip"]


def test_detect_uv(tmp_path):
    (tmp_path / "uv.lock").write_text("")
    assert dep_provisioning.detect_ecosystems(tmp_path) == ["uv"]


def test_detect_go(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    assert dep_provisioning.detect_ecosystems(tmp_path) == ["go"]


def test_detect_multiple_ecosystems_in_one_worktree(tmp_path):
    # A monorepo with a Go service and a JS frontend.
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "package-lock.json").write_text("{}")
    assert set(dep_provisioning.detect_ecosystems(tmp_path)) == {"go", "npm"}


def test_detect_none_for_unrecognized_repo(tmp_path):
    (tmp_path / "README.md").write_text("hi")
    assert dep_provisioning.detect_ecosystems(tmp_path) == []


def test_detect_does_not_walk_subdirectories(tmp_path):
    # Lockfiles live at repo root; a nested example project's lockfile must
    # not falsely trigger detection for the whole worktree.
    nested = tmp_path / "examples" / "demo"
    nested.mkdir(parents=True)
    (nested / "package-lock.json").write_text("{}")
    assert dep_provisioning.detect_ecosystems(tmp_path) == []


# --- plan / env -------------------------------------------------------------


def test_plan_points_at_shared_cache_root_not_installed_tree(tmp_path):
    (tmp_path / "package-lock.json").write_text("{}")
    cache_root = tmp_path / "cache"
    result = dep_provisioning.plan(tmp_path, cache_root=cache_root)
    env = result.env
    assert env == {"npm_config_cache": str(cache_root / "npm")}
    # Never points at the installed dependency tree — only the cache dir.
    for value in env.values():
        assert "node_modules" not in value
        assert value.startswith(str(cache_root))


def test_plan_merges_env_across_ecosystems(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    cache_root = tmp_path / "cache"
    result = dep_provisioning.plan(tmp_path, cache_root=cache_root)
    assert result.env == {
        "GOMODCACHE": str(cache_root / "go-mod"),
        "PIP_CACHE_DIR": str(cache_root / "pip"),
    }


def test_plan_default_cache_root_is_under_chief_wiggum_home(tmp_path):
    (tmp_path / "uv.lock").write_text("")
    result = dep_provisioning.plan(tmp_path)
    (value,) = result.env.values()
    assert ".chief-wiggum/cache/deps" in value.replace("\\", "/")


def test_plan_empty_for_unrecognized_repo(tmp_path):
    result = dep_provisioning.plan(tmp_path)
    assert result.ecosystems == []
    assert result.env == {}
    assert result.cache_dirs == []


def test_pip_plan_carries_prefer_binary_hint(tmp_path):
    (tmp_path / "requirements.txt").write_text("")
    result = dep_provisioning.plan(tmp_path, cache_root=tmp_path / "cache")
    assert result.ecosystems[0].install_flags == ["--prefer-binary"]


# --- concurrency-safety documentation (asserted, not just prose) -----------


@pytest.mark.parametrize("ecosystem", ["npm", "pnpm", "yarn", "pip", "uv", "go"])
def test_every_ecosystem_documents_concurrency_safety(ecosystem):
    spec = dep_provisioning._ECOSYSTEMS[ecosystem]
    assert "concurren" in spec["note"].lower() or "safe" in spec["note"].lower()


def test_no_ecosystem_env_var_targets_an_installed_tree_name():
    # The whole point of #329: never recommend sharing node_modules/.venv/vendor
    # itself, only the package manager's own download/build cache.
    banned = {"node_modules", ".venv", "vendor", "GOPATH"}
    for spec in dep_provisioning._ECOSYSTEMS.values():
        for subdir in spec["env"].values():
            assert subdir not in banned


# --- render_shell -------------------------------------------------------------


def test_render_shell_emits_mkdir_and_export(tmp_path):
    (tmp_path / "package-lock.json").write_text("{}")
    cache_root = tmp_path / "cache"
    result = dep_provisioning.plan(tmp_path, cache_root=cache_root)
    shell = dep_provisioning.render_shell(result)
    assert f'mkdir -p "{cache_root / "npm"}"' in shell
    assert f'export npm_config_cache="{cache_root / "npm"}"' in shell


def test_render_shell_empty_for_no_ecosystems(tmp_path):
    result = dep_provisioning.plan(tmp_path)
    assert dep_provisioning.render_shell(result) == ""


# --- CLI ----------------------------------------------------------------------


def test_cli_plan_json(tmp_path, capsys):
    (tmp_path / "package-lock.json").write_text("{}")
    rc = dep_cache.main(["plan", "--worktree", str(tmp_path), "--cache-root", str(tmp_path / "cache")])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ecosystems"][0]["ecosystem"] == "npm"
    assert payload["env"]["npm_config_cache"] == str(tmp_path / "cache" / "npm")


def test_cli_plan_shell(tmp_path, capsys):
    (tmp_path / "go.mod").write_text("module x\n")
    rc = dep_cache.main(
        ["plan", "--worktree", str(tmp_path), "--cache-root", str(tmp_path / "cache"), "--shell"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "export GOMODCACHE=" in out


def test_cli_plan_empty_repo_exits_zero_with_empty_output(tmp_path, capsys):
    rc = dep_cache.main(["plan", "--worktree", str(tmp_path), "--shell"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cli_subprocess_smoke(tmp_path):
    (tmp_path / "package-lock.json").write_text("{}")
    script = Path(__file__).resolve().parent.parent / "scripts" / "dep_cache.py"
    proc = subprocess.run(
        [sys.executable, str(script), "plan", "--worktree", str(tmp_path),
         "--cache-root", str(tmp_path / "cache")],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ecosystems"][0]["ecosystem"] == "npm"
