"""Shared dependency-cache provisioning for /implement-wave workers (#329).

`/implement`'s worktree rule ("symlink node_modules/.venv instead of
reinstalling", `implement.md`) is only safe for a SEQUENTIAL run: one worker,
one worktree, nobody else touching the dependency tree while it installs.
`/implement-wave` runs `--max-parallel` workers concurrently, each in its own
isolated worktree — isolation is the point (#245). A raw symlink to a shared
node_modules/.venv breaks that: one worker's `npm install`/`pip install`
mutates (prunes, relinks, rewrites) the shared tree while a SIBLING worker is
mid-read of it — a race that produces corrupted or half-installed
dependencies, not merely a slow install.

The safe mechanization is a shared, package-manager-owned, CONTENT-ADDRESSED
cache — never the installed tree itself. Every ecosystem below ships a cache
store designed for concurrent access (internal per-entry locking, atomic
writes); pointing multiple workers' package managers at the same cache
directory is the SANCTIONED use of that store, not a repurposing of it. Each
worker still runs its own install into its OWN node_modules/.venv/GOPATH
(only that worker writes there — no shared mutable state) but resolves
packages from the shared cache instead of the network, so concurrent workers
are both safe and fast.

Per-ecosystem concurrency notes (what actually happens under parallel workers):

- npm    (`npm_config_cache`): npm's on-disk cache uses per-tarball integrity
  locks; documented safe for concurrent `npm install` processes.
- pnpm   (`npm_config_store_dir` / `PNPM_HOME`): the pnpm content-addressable
  store is explicitly designed to be shared across concurrent installs — this
  is pnpm's normal single-machine operating mode, not an edge case.
- yarn classic (`YARN_CACHE_FOLDER`): same category as npm; yarn locks each
  cached tarball individually.
- pip    (`PIP_CACHE_DIR`, pair with `--prefer-binary` in the install command
  to avoid concurrent sdist builds racing on build dirs): pip's wheel/http
  cache uses per-file locking.
- uv     (`UV_CACHE_DIR`): uv's cache is documented safe for concurrent
  processes (it is uv's default multi-project mode).
- go     (`GOMODCACHE`, optionally `GOFLAGS=-mod=mod`): the module cache has
  been safe for concurrent `go` invocations since Go 1.14 (internal lock
  files per module version).

None of these env vars point at node_modules/.venv/vendor/GOPATH itself —
only at the package manager's own cache store. This module never recommends
symlinking an installed dependency tree across parallel workers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CACHE_ROOT = Path.home() / ".chief-wiggum" / "cache" / "deps"

# ecosystem -> (marker files, {env_var: cache-subdir}, install hint)
_ECOSYSTEMS: dict[str, dict] = {
    "npm": {
        "markers": ("package-lock.json",),
        "env": {"npm_config_cache": "npm"},
        "note": "npm's on-disk cache is safe for concurrent `npm install` (per-tarball locks).",
    },
    "pnpm": {
        "markers": ("pnpm-lock.yaml",),
        "env": {"npm_config_store_dir": "pnpm-store"},
        "note": "pnpm's content-addressable store is DESIGNED to be shared across concurrent installs.",
    },
    "yarn": {
        "markers": ("yarn.lock",),
        "env": {"YARN_CACHE_FOLDER": "yarn"},
        "note": "yarn classic locks each cached tarball individually; safe for concurrent installs.",
    },
    "pip": {
        "markers": ("requirements.txt", "pyproject.toml", "setup.py"),
        "env": {"PIP_CACHE_DIR": "pip"},
        "note": "pip's wheel/http cache uses per-file locking. Pair with --prefer-binary to "
        "avoid concurrent sdist builds racing on a shared build dir.",
        "install_flags": ["--prefer-binary"],
    },
    "uv": {
        "markers": ("uv.lock",),
        "env": {"UV_CACHE_DIR": "uv"},
        "note": "uv's cache is documented safe for concurrent processes (its default multi-project mode).",
    },
    "go": {
        "markers": ("go.mod",),
        "env": {"GOMODCACHE": "go-mod"},
        "note": "the go module cache has been safe for concurrent `go` invocations since Go 1.14 "
        "(internal per-module-version lock files).",
    },
}


@dataclass
class EcosystemPlan:
    ecosystem: str
    env: dict[str, str]
    note: str
    install_flags: list[str] = field(default_factory=list)


@dataclass
class ProvisioningPlan:
    ecosystems: list[EcosystemPlan]

    @property
    def env(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for eco in self.ecosystems:
            merged.update(eco.env)
        return merged

    @property
    def cache_dirs(self) -> list[str]:
        return sorted({v for eco in self.ecosystems for v in eco.env.values()})


def detect_ecosystems(worktree: str | Path) -> list[str]:
    """Which ecosystems this worktree needs a dependency install for.

    Detection walks only the given directory's top level (lockfiles live at
    repo root for every ecosystem CW supports) — never a full tree walk.
    """
    root = Path(worktree)
    found = []
    for name, spec in _ECOSYSTEMS.items():
        if any((root / marker).exists() for marker in spec["markers"]):
            found.append(name)
    return found


def plan(worktree: str | Path, *, cache_root: str | Path | None = None) -> ProvisioningPlan:
    """Build the cache-env plan for whatever ecosystems this worktree needs.

    ``cache_root`` (default ``~/.chief-wiggum/cache/deps``) is the SHARED,
    read-through cache root — the same path handed to every concurrent
    worker in a wave. It is safe to share because each ecosystem's cache
    format is itself concurrency-safe (see module docstring); it is never
    the installed dependency tree.
    """
    root = Path(cache_root) if cache_root is not None else DEFAULT_CACHE_ROOT
    ecosystems = []
    for name in detect_ecosystems(worktree):
        spec = _ECOSYSTEMS[name]
        env = {var: str(root / subdir) for var, subdir in spec["env"].items()}
        ecosystems.append(
            EcosystemPlan(
                ecosystem=name,
                env=env,
                note=spec["note"],
                install_flags=list(spec.get("install_flags", [])),
            )
        )
    return ProvisioningPlan(ecosystems=ecosystems)


def render_shell(result: ProvisioningPlan) -> str:
    """Render the plan as `export VAR=path` + `mkdir -p` lines for the workflow
    to `eval` before launching a worker's install. Empty plan -> empty string
    (a repo with no recognized ecosystem falls through to whatever the worker
    would have done anyway)."""
    if not result.ecosystems:
        return ""
    lines = []
    for cache_dir in result.cache_dirs:
        lines.append(f'mkdir -p "{cache_dir}"')
    for var, value in sorted(result.env.items()):
        lines.append(f'export {var}="{value}"')
    return "\n".join(lines) + "\n"


def to_dict(result: ProvisioningPlan) -> dict:
    return {
        "ecosystems": [
            {
                "ecosystem": eco.ecosystem,
                "env": eco.env,
                "note": eco.note,
                "install_flags": eco.install_flags,
            }
            for eco in result.ecosystems
        ],
        "env": result.env,
        "cache_dirs": result.cache_dirs,
    }
