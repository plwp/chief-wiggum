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
DEFAULT_LENSES = Path(__file__).resolve().parents[1] / "config" / "lenses.json"

# Default wall-clock budget (seconds) for the claude-interactive delegate when it is
# running in an OPTIONAL role slot, used when the role doesn't set its own
# ``optional_timeout_seconds`` (chief-wiggum#188). claude-interactive timed out at its
# full 1800s budget on two consecutive large-prompt consults while contributing
# nothing — since it is never a role's required voice, there is no reason a role's
# wall-clock (required providers finish in 10-20 minutes) should be held hostage to a
# voice that's allowed to fail. Deliberately shorter than every required consult
# TOOL_TIMEOUTS entry: an optional provider should fail fast, not merely "less slow".
DEFAULT_OPTIONAL_TIMEOUT_SECONDS = 300


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
    # Optional provider -> lens name mapping (chief-wiggum#163). When a provider
    # is mapped, its charter (from config/lenses.json) is appended to the shared
    # prompt for that provider only — the shared prompt itself never changes.
    lenses: dict[str, str] = field(default_factory=dict)
    # Per-role override (seconds) for how long an OPTIONAL provider's delegate
    # call may run before it's abandoned (chief-wiggum#188). An optional voice
    # that hasn't answered by this deadline is failing softly by design — the
    # role's required providers must not sit blocked on it for the delegate's
    # full budget (1800s for claude-interactive). ``None`` falls back to
    # ``consult_ai.DEFAULT_OPTIONAL_TIMEOUT_SECONDS``.
    optional_timeout_seconds: int | None = None


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


def load_lenses(path: Path = DEFAULT_LENSES) -> dict[str, Any]:
    """Load named review-lens charters from ``config/lenses.json``.

    Returns an empty mapping if the file does not exist — lenses are an
    opt-in review-quorum feature (chief-wiggum#163), not a hard dependency.
    """
    path = path.expanduser()
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get("lenses", {})


def render_charter(lens: dict[str, Any]) -> str:
    """Render a lens as the markdown section appended to a provider's prompt."""
    goal = str(lens.get("goal", "")).strip()
    exclusions = lens.get("exclusions") or []
    lines = ["## Your charter", "", goal]
    if exclusions:
        lines.append("")
        lines.append("Do NOT evaluate:")
        for item in exclusions:
            lines.append(f"- {item}")
    return "\n".join(lines).rstrip() + "\n"


def prompt_for_provider(
    role: Role,
    provider_name: str,
    shared_prompt: str,
    lenses: dict[str, Any] | None,
) -> str:
    """Return the prompt to send ``provider_name`` for ``role``.

    Every provider in a role quorum gets identical context — the value is in
    natural divergence, not roleplay. When ``role`` maps ``provider_name`` to a
    lens, that lens's charter is appended after a clearly delimited section so
    the shared body stays byte-identical across every provider in the role;
    an unmapped provider's prompt is returned completely unchanged.
    """
    lens_name = role.lenses.get(provider_name)
    if not lens_name:
        return shared_prompt
    lenses = lenses or {}
    if lens_name not in lenses:
        raise KeyError(f"role {role.name!r} references unknown lens {lens_name!r}")
    return f"{shared_prompt}\n\n---\n\n{render_charter(lenses[lens_name])}"


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
            lenses=dict(raw.get("lenses", {})),
            optional_timeout_seconds=raw.get("optional_timeout_seconds"),
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


def optional_provider_timeout(
    role: Role,
    provider_name: str,
    default: int = DEFAULT_OPTIONAL_TIMEOUT_SECONDS,
) -> int | None:
    """Return the wall-clock cap (seconds) for ``provider_name``'s delegate call
    when it runs in ``role``'s OPTIONAL slot, else ``None`` (chief-wiggum#188).

    A required provider gets its full budget (``None`` = no override). An
    optional provider is capped to the role's ``optional_timeout_seconds`` when
    set, otherwise ``default``. This is the SINGLE source of the required/optional
    timeout decision — both ``consult_ai.py``'s own ``--role`` quorum and the
    ``/implement`` review pipeline (``chief_wiggum/review.run_review``) call it,
    so an optional ``claude-interactive`` fails fast on BOTH paths instead of
    holding a role's wall-clock to the delegate's 1800s budget.
    """
    if provider_name in role.required:
        return None
    return role.optional_timeout_seconds if role.optional_timeout_seconds is not None else default


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
        # A provider referenced twice (within a list or across required+optional)
        # would run twice and clobber its own output file.
        all_refs = list(role.required) + list(role.optional)
        seen: set[str] = set()
        for name in all_refs:
            if name in seen:
                errors.append(f"role {role_name} references provider {name} more than once")
            seen.add(name)
        # optional_timeout_seconds (chief-wiggum#188) only means anything for a
        # role with at least one optional provider — silently ignoring a typo
        # (a string, a negative number) would let a misconfigured role keep
        # blocking on the full delegate budget with no visible signal.
        ots = role.optional_timeout_seconds
        if ots is not None and (isinstance(ots, bool) or not isinstance(ots, int) or ots <= 0):
            errors.append(
                f"role {role_name} has invalid optional_timeout_seconds {ots!r} "
                "(must be a positive integer)"
            )
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


def validate_role_lenses(role: Role, lenses: dict[str, Any]) -> list[str]:
    """Validate one role's lens assignments before any provider is called.

    Catches two mistakes that would otherwise surface mid-quorum (or worse,
    silently no-op): a lens assigned to a provider that isn't actually in the
    role, and a lens name with no matching charter in ``config/lenses.json``.
    """
    errors: list[str] = []
    members = set(role.required) | set(role.optional)
    for provider_name, lens_name in role.lenses.items():
        if provider_name not in members:
            errors.append(
                f"role {role.name} assigns a lens to {provider_name!r}, "
                "which is not a required or optional provider of that role"
            )
        if lens_name not in lenses:
            errors.append(
                f"role {role.name} references unknown lens {lens_name!r}"
            )
    return errors


def validate_lenses(config: dict[str, Any], lenses: dict[str, Any]) -> list[str]:
    """Validate every role's lens assignments in ``config``."""
    errors: list[str] = []
    for role in roles_from_config(config).values():
        errors.extend(validate_role_lenses(role, lenses))
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
    error_path: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "required": self.required,
            "status": self.status,
            "path": self.path,
            "attempts": self.attempts,
            "error": self.error,
            "error_path": self.error_path,
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
    # Clear any stale artifacts from a previous run so a failure can't leave an
    # old success file (or vice versa) for a later reader to pick up.
    ok_path = output_dir / f"{role_name}-{provider.name}.md"
    err_path = output_dir / f"{role_name}-{provider.name}.error.md"
    ok_path.unlink(missing_ok=True)
    err_path.unlink(missing_ok=True)

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
        ok_path.write_text(text)
        return ProviderResult(provider.name, required, "ok", str(ok_path), attempt, None)

    err_path.write_text(last_error or "unknown error")
    return ProviderResult(
        provider.name, required, "failed", None, attempt, last_error, str(err_path)
    )


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

    # Required first; dedupe by name (a provider listed twice, or in both
    # required and optional, must not run twice and clobber its own file).
    tasks: list[tuple[Provider, bool]] = []
    seen: set[str] = set()
    for provider, required in [(p, True) for p in plan.required] + [(p, False) for p in plan.optional]:
        if provider.name in seen:
            continue
        seen.add(provider.name)
        tasks.append((provider, required))
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
