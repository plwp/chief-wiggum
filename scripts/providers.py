#!/usr/bin/env python3
"""Provider and role configuration for Chief Wiggum AI backends."""

from __future__ import annotations

import concurrent.futures
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "providers.json"


@dataclass(frozen=True)
class Provider:
    name: str
    type: str
    enabled: bool
    tool: str | None = None
    delegate: str | None = None


@dataclass(frozen=True)
class Role:
    name: str
    required: tuple[str, ...]
    optional: tuple[str, ...]


@dataclass(frozen=True)
class RolePlan:
    role: Role
    required: tuple[Provider, ...]
    optional: tuple[Provider, ...]
    missing_required: tuple[str, ...]
    skipped_optional: tuple[str, ...]

    @property
    def runnable(self) -> tuple[Provider, ...]:
        return self.required + self.optional

    @property
    def ok(self) -> bool:
        return not self.missing_required


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    return json.loads(path.expanduser().read_text())


def providers_from_config(config: dict[str, Any]) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    for name, raw in config.get("providers", {}).items():
        providers[name] = Provider(
            name=name,
            type=raw["type"],
            enabled=bool(raw.get("enabled", True)),
            tool=raw.get("tool"),
            delegate=raw.get("delegate"),
        )
    return providers


def roles_from_config(config: dict[str, Any]) -> dict[str, Role]:
    roles: dict[str, Role] = {}
    for name, raw in config.get("roles", {}).items():
        roles[name] = Role(
            name=name,
            required=tuple(raw.get("required", [])),
            optional=tuple(raw.get("optional", [])),
        )
    return roles


def provider_is_enabled(provider: Provider, enabled: set[str], disabled: set[str]) -> bool:
    if provider.name in disabled:
        return False
    if provider.name in enabled:
        return True
    return provider.enabled


def plan_role(
    role_name: str,
    config: dict[str, Any],
    *,
    enabled: set[str] | None = None,
    disabled: set[str] | None = None,
) -> RolePlan:
    providers = providers_from_config(config)
    roles = roles_from_config(config)
    enabled = enabled or set()
    disabled = disabled or set()

    if role_name not in roles:
        known = ", ".join(sorted(roles))
        raise KeyError(f"unknown role: {role_name}. Known roles: {known}")

    role = roles[role_name]
    required: list[Provider] = []
    optional: list[Provider] = []
    missing_required: list[str] = []
    skipped_optional: list[str] = []

    for name in role.required:
        provider = providers.get(name)
        if provider and provider_is_enabled(provider, enabled, disabled):
            required.append(provider)
        else:
            missing_required.append(name)

    for name in role.optional:
        provider = providers.get(name)
        if provider and provider_is_enabled(provider, enabled, disabled):
            optional.append(provider)
        else:
            skipped_optional.append(name)

    return RolePlan(
        role=role,
        required=tuple(required),
        optional=tuple(optional),
        missing_required=tuple(missing_required),
        skipped_optional=tuple(skipped_optional),
    )


def validate_config(
    config: dict[str, Any],
    *,
    supported_tools: set[str] | None = None,
    supported_delegates: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    providers = providers_from_config(config)
    for role_name, role in roles_from_config(config).items():
        for provider_name in role.required + role.optional:
            if provider_name not in providers:
                errors.append(f"role {role_name} references unknown provider {provider_name}")
    for provider in providers.values():
        if provider.type == "tool" and not provider.tool:
            errors.append(f"provider {provider.name} has type=tool but no tool")
        if supported_tools is not None and provider.type == "tool" and provider.tool not in supported_tools:
            errors.append(f"provider {provider.name} references unsupported tool {provider.tool}")
        if provider.type == "delegate" and not provider.delegate:
            errors.append(f"provider {provider.name} has type=delegate but no delegate")
        if (
            supported_delegates is not None
            and provider.type == "delegate"
            and provider.delegate not in supported_delegates
        ):
            errors.append(
                f"provider {provider.name} references unsupported delegate {provider.delegate}"
            )
        if provider.type not in {"tool", "delegate"}:
            errors.append(f"provider {provider.name} has unsupported type {provider.type}")
    return errors


# --- parallel quorum execution ----------------------------------------------

# Output beginning with one of these markers is a failure sentinel written by a
# failed provider call, not a substantive response.
INVALID_MARKERS = ("Timeout:", "Error:")

# An ``execute`` callable runs a single provider and returns its response text.
# It is injected so the quorum runner is testable without real provider calls.
ExecuteFn = Callable[[Provider], str]


@dataclass
class ProviderResult:
    name: str
    required: bool
    status: str  # "ok" | "failed"
    path: str | None = None
    attempts: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "required": self.required,
            "status": self.status,
            "path": self.path,
            "attempts": self.attempts,
            "error": self.error,
        }


@dataclass
class QuorumManifest:
    role: str
    results: list[ProviderResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff every required provider produced valid output."""
        return all(r.status == "ok" for r in self.results if r.required)

    @property
    def failed_required(self) -> list[str]:
        return [r.name for r in self.results if r.required and r.status != "ok"]

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "ok": self.ok,
            "failed_required": self.failed_required,
            "results": [r.to_dict() for r in self.results],
        }


def validate_output(text: str | None, *, min_bytes: int = 20) -> str | None:
    """Return a failure reason if ``text`` is not a substantive response, else None."""
    if text is None:
        return "no output"
    stripped = text.strip()
    if len(stripped.encode("utf-8")) < min_bytes:
        return f"output too short (<{min_bytes} bytes)"
    for marker in INVALID_MARKERS:
        if stripped.startswith(marker):
            return f"output starts with failure marker {marker!r}"
    return None


def _run_one_provider(
    provider: Provider,
    required: bool,
    execute: ExecuteFn,
    output_dir: Path,
    role_name: str,
    max_attempts: int,
    min_bytes: int,
) -> ProviderResult:
    # Only required providers are retried; an optional provider gets one shot.
    attempts_allowed = max(1, max_attempts) if required else 1
    last_error: str | None = None
    attempt = 0
    for attempt in range(1, attempts_allowed + 1):
        try:
            text = execute(provider)
        except Exception as exc:  # noqa: BLE001 - any provider failure is retryable
            last_error = f"execution failed: {exc}"
            continue
        problem = validate_output(text, min_bytes=min_bytes)
        if problem:
            last_error = problem
            continue
        path = output_dir / f"{role_name}-{provider.name}.md"
        path.write_text(text)
        return ProviderResult(provider.name, required, "ok", str(path), attempt, None)

    err_path = output_dir / f"{role_name}-{provider.name}.error.md"
    err_path.write_text(last_error or "unknown error")
    return ProviderResult(provider.name, required, "failed", None, attempt, last_error)


def run_role_quorum(
    plan: RolePlan,
    execute: ExecuteFn,
    output_dir: str | Path,
    *,
    max_attempts: int = 2,
    min_bytes: int = 20,
    max_workers: int | None = None,
    write_manifest: bool = True,
) -> QuorumManifest:
    """Run a role's providers concurrently with retries and output validation.

    Required and optional providers run in parallel. Required providers are
    retried up to ``max_attempts`` times; optional providers fail without
    blocking the quorum. A ``{role}-manifest.json`` records per-provider status.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    tasks: list[tuple[Provider, bool]] = [(p, True) for p in plan.required]
    tasks += [(p, False) for p in plan.optional]
    order = {p.name: i for i, (p, _) in enumerate(tasks)}

    results: list[ProviderResult] = []
    if tasks:
        workers = max_workers or len(tasks)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    _run_one_provider,
                    provider, required, execute, out, plan.role.name, max_attempts, min_bytes,
                )
                for provider, required in tasks
            ]
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())

    # Deterministic order: required (config order) then optional.
    results.sort(key=lambda r: order.get(r.name, 1_000))
    manifest = QuorumManifest(plan.role.name, results)

    if write_manifest:
        (out / f"{plan.role.name}-manifest.json").write_text(
            json.dumps(manifest.to_dict(), indent=2)
        )
    return manifest
