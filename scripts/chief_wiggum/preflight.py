"""Parallel provider health checks, run before any phase needs a provider.

The operator complaint this answers (chief-wiggum#375): roughly 15 of a
25-minute consult phase went on environment failures discovered SERIALLY, one
relaunch at a time, after the prompt was already built. A missing keyring
entry, a dead CLI and an uninstalled SDK each cost a full round trip.

So: check every configured provider at once, before the work starts, and say
what is broken and what the role can still do.

Four states, never three (chief-wiggum#289). A probe that could not run reports
`unknown`, which is not `ok`. "We could not tell" and "it is fine" are
different answers, and collapsing them is how a preflight becomes a rubber
stamp.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Health(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"   # a hard requirement is missing, and we know which
    UNKNOWN = "unknown"           # the probe itself could not run
    DISABLED = "disabled"         # switched off in config; a real answer, not a fault
    # Installed, authenticated, answering `--version` — and unable to serve a
    # single request. A quota-exhausted CLI passes every structural check
    # there is, so without its own state it reports `ok` and the operator
    # discovers the truth mid-phase, serially, which is the cost #375 is
    # about. Distinct from UNAVAILABLE because nothing is missing, and from
    # UNKNOWN because the probe ran and got a definite answer.
    EXHAUSTED = "exhausted"


class RoleStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"   # an optional voice is down; the quorum still stands
    BLOCKED = "blocked"     # a required provider is down


@dataclass(frozen=True)
class Requirement:
    """One thing a provider needs before it can be called."""

    kind: str          # "command" | "python" | "secret" | "env"
    name: str

    def describe(self) -> str:
        return f"{self.kind}:{self.name}"


# What each tool/delegate needs. Additive: a provider entry may override this
# with its own "preflight" list, so a new provider does not need a code change.
REQUIREMENTS: dict[str, tuple[Requirement, ...]] = {
    "codex": (Requirement("command", "codex"),),
    "gemini": (Requirement("command", "gemini"),),
    "claude": (Requirement("command", "claude"),),
    "openrouter": (Requirement("secret", "OPENROUTER_API_KEY"),),
    "gemini-vertex": (
        Requirement("python", "google.cloud.aiplatform"),
        Requirement("env", "GOOGLE_CLOUD_PROJECT"),
    ),
    "claude-interactive": (
        Requirement("command", "claude"),
        Requirement("command", "tmux"),
    ),
    "codex-responses": (Requirement("command", "codex"),),
}


@dataclass(frozen=True)
class ProviderReport:
    name: str
    health: Health
    missing: tuple[str, ...] = ()
    detail: str = ""
    duration_ms: float = 0.0

    @property
    def usable(self) -> bool:
        return self.health is Health.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "health": str(self.health),
            "missing": list(self.missing),
            "detail": self.detail,
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass(frozen=True)
class RoleReport:
    role: str
    status: RoleStatus
    required_down: tuple[str, ...] = ()
    optional_down: tuple[str, ...] = ()
    fallback: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "status": str(self.status),
            "required_down": list(self.required_down),
            "optional_down": list(self.optional_down),
            "fallback": list(self.fallback),
            "detail": self.detail,
        }


@dataclass
class Probes:
    """Injectable checks. Defaults hit the real environment."""

    command: Callable[[str], bool] = field(default=lambda name: shutil.which(name) is not None)
    python: Callable[[str], bool] = field(
        default=lambda name: importlib.util.find_spec(name) is not None
    )
    env: Callable[[str], bool] = field(default=lambda name: bool(os.environ.get(name)))
    secret: Callable[[str], bool] | None = None

    def check(self, requirement: Requirement) -> bool:
        if requirement.kind == "command":
            return bool(self.command(requirement.name))
        if requirement.kind == "python":
            return bool(self.python(requirement.name))
        if requirement.kind == "env":
            return bool(self.env(requirement.name))
        if requirement.kind == "secret":
            return bool(self._secret(requirement.name))
        raise ValueError(f"unknown requirement kind {requirement.kind!r}")

    def _secret(self, name: str) -> bool:
        if self.secret is not None:
            return self.secret(name)
        return default_secret_probe(name)


def default_secret_probe(name: str) -> bool:
    """Ask the keyring whether a secret exists, without ever reading its value.

    Presence only: CW's rule is that secrets are fetched at call time and never
    printed or logged, and a preflight has no business handling the value.
    """
    try:
        import keyring  # noqa: PLC0415 - optional dependency, probed on demand
    except ImportError as exc:
        raise ProbeUnavailable(f"keyring is not importable: {exc}") from exc
    try:
        return keyring.get_password("chief-wiggum", name) is not None
    except Exception as exc:  # noqa: BLE001 - a locked keychain is "unknown", not "absent"
        raise ProbeUnavailable(f"keyring lookup failed: {exc}") from exc


class ProbeUnavailable(RuntimeError):
    """The probe could not run, so the answer is `unknown`, never `ok`."""


def requirements_for(name: str, entry: Mapping[str, Any]) -> tuple[Requirement, ...]:
    """What this provider needs, from its config entry."""
    override = entry.get("preflight")
    if override:
        return tuple(
            Requirement(str(item.get("kind", "command")), str(item.get("name", "")))
            for item in override
        )
    key = str(entry.get("tool") or entry.get("delegate") or name)
    return REQUIREMENTS.get(key, ())


def check_provider(
    name: str, entry: Mapping[str, Any], probes: Probes | None = None
) -> ProviderReport:
    """Health-check one provider. Never raises: an unrunnable probe is a state."""
    import time

    probes = probes or Probes()
    started = time.perf_counter()
    if not entry.get("enabled", True):
        return ProviderReport(name, Health.DISABLED, detail="disabled in config")

    requirements = requirements_for(name, entry)
    if not requirements:
        # An unknown provider shape is not a pass. Say so rather than assume.
        return ProviderReport(
            name,
            Health.UNKNOWN,
            detail=f"no preflight requirements known for {name!r}; cannot verify",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    missing: list[str] = []
    for requirement in requirements:
        try:
            present = probes.check(requirement)
        except ProbeUnavailable as exc:
            return ProviderReport(
                name,
                Health.UNKNOWN,
                detail=str(exc),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        if not present:
            missing.append(requirement.describe())

    elapsed = (time.perf_counter() - started) * 1000
    if missing:
        return ProviderReport(
            name, Health.UNAVAILABLE, tuple(missing),
            detail="missing: " + ", ".join(missing), duration_ms=elapsed,
        )
    return ProviderReport(name, Health.OK, duration_ms=elapsed)


def check_all(
    providers: Mapping[str, Mapping[str, Any]],
    probes: Probes | None = None,
    *,
    max_workers: int = 8,
    usage: bool = False,
    usage_probe: Callable[[str], tuple[Health, str]] | None = None,
) -> dict[str, ProviderReport]:
    """Check every provider at once.

    Parallel because serial discovery is the actual complaint: each failure
    costs a round trip, and they are independent.
    """
    probes = probes or Probes()
    names = sorted(providers)
    if not names:
        return {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(names))) as pool:
        reports = pool.map(
            lambda name: check_provider(name, providers[name], probes), names
        )
    result = dict(zip(names, reports, strict=True))
    if usage:
        result = _apply_usage_probes(result, max_workers=max_workers,
                                     usage_probe=usage_probe)
    return result


def _apply_usage_probes(
    reports: dict[str, ProviderReport], *, max_workers: int = 8,
    usage_probe: Callable[[str], tuple[Health, str]] | None = None,
) -> dict[str, ProviderReport]:
    """Re-classify structurally-OK providers that cannot actually serve.

    Only OK providers are asked: there is nothing to learn from spending a
    call on one already known to be missing a requirement, and nothing to
    gain from asking a disabled one.
    """
    probe = usage_probe or (lambda name: probe_command_usable(name))
    askable = [name for name, report in reports.items()
               if report.health is Health.OK and name in USAGE_PROBE_COMMANDS]
    if not askable:
        return reports
    with ThreadPoolExecutor(max_workers=min(max_workers, len(askable))) as pool:
        answers = dict(zip(askable, pool.map(probe, askable), strict=True))
    for name, (health, detail) in answers.items():
        if health is Health.OK:
            continue
        reports[name] = ProviderReport(name, health, detail=detail,
                                       duration_ms=reports[name].duration_ms)
    return reports


def role_report(
    role: str,
    spec: Mapping[str, Any],
    reports: Mapping[str, ProviderReport],
) -> RoleReport:
    """Whether a role can run, and what it falls back to if not.

    A required provider that is down blocks the role and the fallback is named
    here rather than improvised by the orchestrator mid-phase.
    """
    required = [str(name) for name in spec.get("required", [])]
    optional = [str(name) for name in spec.get("optional", [])]

    def down(name: str) -> bool:
        report = reports.get(name)
        return report is None or not report.usable

    required_down = tuple(name for name in required if down(name))
    optional_down = tuple(name for name in optional if down(name))
    healthy_optional = tuple(name for name in optional if not down(name))

    if required_down:
        return RoleReport(
            role, RoleStatus.BLOCKED, required_down, optional_down,
            fallback=healthy_optional,
            detail=(
                f"required provider(s) {list(required_down)} are not usable; "
                + (f"healthy optional voices: {list(healthy_optional)}"
                   if healthy_optional else "no healthy fallback in this role")
            ),
        )
    if optional_down:
        return RoleReport(
            role, RoleStatus.DEGRADED, (), optional_down,
            detail=f"optional provider(s) {list(optional_down)} are not usable; quorum stands",
        )
    return RoleReport(role, RoleStatus.OK)


def preflight(
    config: Mapping[str, Any],
    probes: Probes | None = None,
    *,
    roles: Sequence[str] | None = None,
    usage: bool = False,
    usage_probe: Callable[[str], tuple[Health, str]] | None = None,
) -> dict[str, Any]:
    """Full preflight: every provider, then every role's readiness."""
    providers = dict(config.get("providers") or {})
    role_specs = dict(config.get("roles") or {})
    if roles is not None:
        role_specs = {name: spec for name, spec in role_specs.items() if name in roles}

    reports = check_all(providers, probes, usage=usage,
                        usage_probe=usage_probe)
    role_reports = {
        name: role_report(name, spec, reports) for name, spec in sorted(role_specs.items())
    }
    blocked = sorted(name for name, r in role_reports.items() if r.status is RoleStatus.BLOCKED)
    unknown = sorted(name for name, r in reports.items() if r.health is Health.UNKNOWN)
    return {
        "ok": not blocked and not unknown,
        "providers": {name: report.to_dict() for name, report in sorted(reports.items())},
        "roles": {name: report.to_dict() for name, report in role_reports.items()},
        "blocked_roles": blocked,
        "unverifiable_providers": unknown,
    }


# Markers that mean "this account cannot serve a request right now", as
# distinct from "this call was malformed". Matched case-insensitively against
# the provider's own stderr/stdout.
EXHAUSTION_MARKERS = (
    "usage limit",
    "quota",
    "rate limit exceeded",
    "insufficient_quota",
    "billing",
    "credit balance is too low",
    "429",
)

# How to ask each CLI to do the smallest real unit of work. `--version` cannot
# answer this question: the binary is installed and answering in exactly the
# case that matters.
USAGE_PROBE_COMMANDS: dict[str, tuple[str, ...]] = {
    "codex": ("codex", "exec", "--sandbox", "read-only", "-"),
}


def probe_command_usable(
    name: str, *, timeout: float = 60.0, prompt: str = "Reply with: ok",
) -> tuple[Health, str]:
    """Ask the provider to do the smallest real thing, and classify the answer.

    Opt-in, because it costs a genuine (tiny) call when the provider is
    healthy. On an exhausted account it costs nothing — the provider refuses
    before doing any work, which is exactly why this is worth asking.

    Returns UNKNOWN rather than guessing when the probe cannot run or the
    output does not clearly say why it failed. A provider that failed for an
    unnameable reason is not `ok`, and it is not `exhausted` either.
    """
    argv = USAGE_PROBE_COMMANDS.get(name)
    if argv is None:
        return Health.UNKNOWN, f"no usage probe defined for {name!r}"
    if shutil.which(argv[0]) is None:
        return Health.UNAVAILABLE, f"command:{argv[0]}"
    try:
        result = subprocess.run(
            list(argv), input=prompt, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Health.UNKNOWN, f"{name} usage probe did not complete: {exc}"

    blob = f"{result.stdout}\n{result.stderr}".lower()
    hit = next((m for m in EXHAUSTION_MARKERS if m in blob), None)
    if hit:
        return Health.EXHAUSTED, f"{name} reports {hit!r} — installed and out of budget"
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()
        return Health.UNKNOWN, (
            f"{name} usage probe exited {result.returncode}: "
            f"{tail[-1][:160] if tail else '(no output)'}")
    return Health.OK, ""


def probe_command_alive(name: str, *, timeout: float = 10.0) -> bool:
    """Heavier probe: the binary exists AND answers. Not the default.

    `shutil.which` only proves a file is on PATH; this proves it runs. Used
    when a stale shim or a broken install is the suspected failure.
    """
    if shutil.which(name) is None:
        return False
    try:
        result = subprocess.run(
            [name, "--version"], capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeUnavailable(f"{name} did not answer --version: {exc}") from exc
    return result.returncode == 0
