#!/usr/bin/env python3
"""Provider and role configuration for Chief Wiggum AI backends."""

from __future__ import annotations

import json
from dataclasses import dataclass
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
