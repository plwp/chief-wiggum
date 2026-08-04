#!/usr/bin/env python3
"""Provider and role configuration for Chief Wiggum AI backends."""

from __future__ import annotations

import concurrent.futures
import inspect
import json
import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
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

# A shared prompt smaller than this is almost never intentional — the
# signature of a truncated write (chief-wiggum#163/#330). SINGLE source of
# truth: consult_ai.py re-exports this rather than defining its own copy, so
# every ``run_role_quorum`` entry path (consult_ai.py's own ``--role`` CLI
# AND scripts/run_review.py, which calls straight into
# ``chief_wiggum.review.run_review`` -> this module) is covered, not just
# the CLI that happened to add a pre-check first.
MIN_PROMPT_BYTES = 200


class ShortPromptError(RuntimeError):
    """Raised by ``run_role_quorum`` when the shared prompt is below
    ``MIN_PROMPT_BYTES`` (chief-wiggum#330) — refuses BEFORE any provider
    task is submitted, generalizing consult_ai.py's own pre-check (chief-
    wiggum#163) to every caller of this module, not just that one CLI. A
    truncated/empty prompt would otherwise burn a whole quorum's worth of
    provider calls (each failing identically and uselessly) before the
    caller ever finds out."""


# Upper bound (seconds) on a WHOLE quorum's wall clock, independent of any
# individual provider's own internal timeout (chief-wiggum#330). Every
# execute() call this codebase ships is already individually bounded (a
# subprocess process-group kill for CLI tools, a daemon-thread-join deadline
# for the Vertex SDK call, a thread+join deadline for OpenRouter's HTTP
# call) — this is deliberately generous (comfortably above claude-
# interactive's own 1800s times a couple of retries) because it should
# almost never fire in normal operation; it exists so a provider whose OWN
# bound is missing or buggy cannot hang ``run_role_quorum`` forever. A
# caller with tighter knowledge of its configured budgets may pass a smaller
# ``quorum_timeout``, or ``None`` to fully opt out (matching the pre-#330
# unbounded behavior).
DEFAULT_QUORUM_TIMEOUT_SECONDS = 4200  # 70 minutes

# Short exponential backoff between retry attempts (chief-wiggum#330) — NOT
# a "wait for the API to be less busy" scheme, just enough breathing room
# that an instant, full-payload, identical-prompt retry (the pre-#330
# behavior: "a codex timeout at 600s is retried for another full 600s with
# the identical ~60k-token prompt") doesn't hammer a provider that just
# failed. Doubles per attempt, capped, with a small jitter so concurrent
# retries across providers don't all land on the same instant.
RETRY_BACKOFF_BASE_SECONDS = 0.5
RETRY_BACKOFF_CAP_SECONDS = 8.0
# A rate-limit-classified failure waits noticeably longer than a plain one —
# a 429 wants the caller to back off, not retry at the same cadence.
RETRY_BACKOFF_RATE_LIMIT_MULTIPLIER = 3.0


def classify_failure(exc: BaseException) -> str:
    """Coarse classification of a provider-call failure (chief-wiggum#330):
    ``'timeout' | 'rate_limit' | 'other'``. Used to pick a retry backoff and
    (for a caller whose ``execute`` opts into retry context — see
    ``execute_accepts_retry_context``) to let the NEXT attempt reduce its
    own budget after a timeout, rather than retrying with the identical
    full budget that just expired.

    Duck-typed on exception TYPE (recognizing every timeout shape this
    codebase actually raises: the stdlib ``TimeoutError``,
    ``concurrent.futures.TimeoutError`` — an alias of the former on the
    Python versions this repo targets — and ``subprocess.TimeoutExpired``)
    and, as a fallback, on the exception's message text — never on a
    provider-specific payload shape, so a new provider's own wording still
    classifies reasonably rather than raising.
    """
    if isinstance(exc, (TimeoutError, concurrent.futures.TimeoutError)):
        return "timeout"
    # subprocess.TimeoutExpired is a subclass of Exception, not TimeoutError;
    # imported lazily so this module doesn't need `subprocess` for anything
    # else.
    import subprocess as _subprocess

    if isinstance(exc, _subprocess.TimeoutExpired):
        return "timeout"
    message = str(exc).lower()
    if "429" in message or "rate limit" in message or "too many requests" in message:
        return "rate_limit"
    if "timeout" in message or "timed out" in message:
        return "timeout"
    return "other"


def _retry_backoff_seconds(attempt: int, previous_failure_kind: str | None) -> float:
    """Backoff (seconds) before retry attempt ``attempt`` (2, 3, ...),
    chosen from ``previous_failure_kind`` (chief-wiggum#330). Exponential in
    the attempt number, capped, with up to 20% jitter so concurrent retries
    don't all land in lockstep; a ``rate_limit`` classified failure gets a
    multiplied delay on top of that schedule."""
    exponent = max(0, attempt - 2)
    delay = min(RETRY_BACKOFF_CAP_SECONDS, RETRY_BACKOFF_BASE_SECONDS * (2 ** exponent))
    if previous_failure_kind == "rate_limit":
        delay *= RETRY_BACKOFF_RATE_LIMIT_MULTIPLIER
    jitter = delay * 0.2 * random.random()
    return delay + jitter


def execute_accepts_retry_context(execute: "ExecuteFn") -> bool:
    """Whether ``execute`` opts into per-attempt retry context
    (chief-wiggum#330): an ``attempt`` keyword parameter, or ``**kwargs``.

    Introspected once per provider task so every EXISTING ``execute(provider)``
    1-arg callable — every test fixture in this repo, and any external
    caller that hasn't opted in — keeps working completely unchanged: passing
    extra keyword arguments to a callable that doesn't declare them would
    raise a ``TypeError``, so this must be checked BEFORE the first call, not
    discovered by trial and error. A callable ``inspect.signature`` cannot
    introspect (a C builtin, some exotic callable) degrades to "not opted
    in" rather than raising.
    """
    try:
        sig = inspect.signature(execute)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.name == "attempt" or param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return False


@dataclass(frozen=True)
class Provider:
    name: str
    type: str
    enabled: bool
    tool: str | None = None
    delegate: str | None = None
    # Per-provider default model id (chief-wiggum#237). Lets two provider
    # entries share one tool with different models (e.g. `opus` = the claude
    # CLI pinned to the opus model). A caller's explicit --model still wins.
    model: str | None = None
    # Does this provider's call path ever give it a chance to read the target
    # repo (chief-wiggum#319)? True for every CLI/delegate that runs with a
    # real ``cwd`` and its own file access (codex, gemini CLI's --yolo tool
    # loop, claude/claude-interactive) — including ``gemini-vertex``, which
    # now retrieves touched-file content for diff-bearing prompts (still no
    # arbitrary repo browsing, so a measured call can legitimately come back
    # blind — see ``detect_blind_providers``). False for a provider whose
    # ENTIRE call path is a plain text completion with no file access under
    # any circumstance (the openrouter-backed models) — declared explicitly
    # here rather than inferred, so a role can tell a code-reading provider
    # from a text-only one without re-deriving it from behavior.
    reads_repo: bool = True
    # Can this provider's call path ever RECEIVE image bytes (chief-wiggum#321)?
    # True for every CLI/delegate that has real filesystem access via ``cwd``
    # and can open a named screenshot itself (codex, gemini CLI, claude,
    # claude-interactive) and for ``gemini-vertex``, which now attaches image
    # parts directly to the SDK request for any prompt that names images
    # under ``cwd`` (see ``consult_ai._read_touched_images``). False for a
    # provider whose ENTIRE call path is a plain text completion with no
    # attachment mechanism at all (the openrouter-backed models) — declared
    # explicitly, mirroring ``reads_repo``, so a role that SENDS images can
    # tell an image-capable provider from a text-only one without waiting to
    # observe a suspiciously text-only critique.
    accepts_images: bool = True


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
    # Does this role's job actually require a provider to have read the repo
    # (chief-wiggum#319)? True for the roles that explore, implement, or
    # review CODE (``explorer``, ``implementer``, ``reviewer``,
    # ``architecture_critic``, ``risky_diff_review``) — a required or
    # optional member of one of these that comes back having plainly only
    # seen its own prompt is a silent quorum gap, not a quiet success.
    # False for a role whose job was never repo-grounded in the first place:
    # ``design_critic`` reviews rendered SCREENSHOTS (an image-reading gap is
    # a separate, real defect, filed rather than fixed here — see the final
    # report), ``kill-review`` evaluates a business-bet writeup, and
    # ``divergence`` exists purely to widen textual distribution (see its
    # own docstring below). Declared per role rather than inferred so a role
    # that never needed repo-reading is never reported as if a provider
    # failed it.
    requires_repo_read: bool = True
    # Does this role's job send IMAGE bytes to its providers (chief-wiggum#321)?
    # True only for ``design_critic`` (sends rendered screenshots to critique).
    # This is deliberately a SEPARATE axis from ``requires_repo_read`` —
    # ``design_critic`` is correctly ``requires_repo_read=False`` (it was
    # never asking a provider to read the repo), which is exactly why #319's
    # ``detect_blind_providers`` cannot see this role's blindness: it never
    # sends a diff, so there is nothing repo-shaped to be blind to. The
    # blindness here is image-shaped — a required provider with no
    # image-attachment path critiquing a written description of screenshots
    # it never saw. Declared per role, checked against each provider's
    # ``accepts_images`` by ``detect_image_blind_providers``.
    sends_images: bool = False


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
            model=raw.get("model"),
            reads_repo=bool(raw.get("reads_repo", True)),
            accepts_images=bool(raw.get("accepts_images", True)),
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
            requires_repo_read=bool(raw.get("requires_repo_read", True)),
            sends_images=bool(raw.get("sends_images", False)),
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

# An ``execute`` callable runs a single provider and returns its response text
# — OR, when the caller can supply it, a ``(text, usage)`` pair where ``usage``
# is anything exposing ``.tokens_in``/``.tokens_out``/``.usage_status``/
# ``.resolved_model`` (duck-typed against ``consult_ai.Usage`` so this module
# never imports it — chief-wiggum#319). ``_run_one_provider`` accepts either
# shape: existing callers (and every test) that return a bare string keep
# working unchanged; ``consult_ai.consult_provider`` and the review pipeline
# now return the pair, which is how per-provider token usage reaches the
# manifest at all.
ExecuteFn = Callable[[Provider], "str | tuple[str, object]"]


@dataclass
class ProviderResult:
    name: str
    required: bool
    status: str  # "ok" | "failed"
    path: str | None = None
    attempts: int = 0
    error: str | None = None
    error_path: str | None = None
    # Usage, threaded from whatever ``execute`` returned (chief-wiggum#319).
    # ``None`` when the execute callable didn't supply usage (the plain-string
    # contract) or the call failed — never fabricated, never zero-filled.
    tokens_in: int | None = None
    tokens_out: int | None = None
    usage_status: str | None = None
    resolved_model: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "required": self.required,
            "status": self.status,
            "path": self.path,
            "attempts": self.attempts,
            "error": self.error,
            "error_path": self.error_path,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "usage_status": self.usage_status,
            "resolved_model": self.resolved_model,
        }


# A provider's tokens_in may exceed the estimated size of its OWN prompt by up
# to this factor and still count as "answered from the prompt alone, never
# touched the repo" (chief-wiggum#319). Real repo reading — even one opened
# file — inflates tokens_in by orders of magnitude (the ticket's own evidence:
# 613k-1.1M vs ~1.3k-1.4k, a ~165:1 ratio); a small multiplier here is
# generous headroom for SDK-added wrapping, not a real detection risk.
BLIND_PROVIDER_MARGIN = 1.5


def estimate_prompt_tokens(text: str) -> int:
    """Coarse, model-agnostic token estimate (~4 chars/token — the standard
    rough heuristic for English prose/code) used ONLY as a floor-check
    denominator, never for billing. Precision doesn't matter here: the gap
    this is built to catch is two-plus orders of magnitude wide."""
    return max(1, len(text) // 4)


@dataclass
class BlindnessFinding:
    """One provider whose measured usage contradicts what the role required
    of it (chief-wiggum#319) — either it answered from the prompt alone
    (``kind="blind"``), or its usage could not be measured at all
    (``kind="unmeasured"``), which is NOT the same as measured-and-fine and
    must never be reported as a quiet pass."""

    provider: str
    kind: str  # "blind" | "unmeasured"
    tokens_in: int | None
    prompt_tokens_estimate: int | None
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BlindnessReport:
    """chief-wiggum#319: did every provider a role DECLARED needs repo
    reading actually read anything beyond its own prompt? Follows the
    established four-state gate vocabulary (chief-wiggum#289,
    ``check_traceability.py`` is the reference): ``pass`` / ``findings`` /
    ``inapplicable`` / ``error``. This is a REPORT, never a gate — it
    surfaces into the existing consult/review manifest and never blocks a
    quorum on its own.
    """

    role: str
    requires_repo_read: bool
    findings: list[BlindnessFinding] = field(default_factory=list)
    providers_checked: int = 0
    error: str | None = None

    @property
    def applicability(self) -> str:
        if self.error:
            return "error"
        if not self.requires_repo_read:
            return "inapplicable"
        return "applicable"

    @property
    def outcome(self) -> str:
        """The standard four-state gate outcome (#289): pass | findings |
        inapplicable | error. Derived, never stored."""
        if self.applicability in ("error", "inapplicable"):
            return self.applicability
        return "findings" if self.findings else "pass"

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "requires_repo_read": self.requires_repo_read,
            "applicability": self.applicability,
            "outcome": self.outcome,
            "providers_checked": self.providers_checked,
            "error": self.error,
            "findings": [f.to_dict() for f in self.findings],
        }


def detect_blind_providers(
    role: Role,
    providers_by_name: dict[str, Provider],
    results: list[ProviderResult],
    prompt_tokens_by_provider: dict[str, int],
    *,
    margin: float = BLIND_PROVIDER_MARGIN,
) -> BlindnessReport:
    """chief-wiggum#319: catch a quorum reporting healthy while a required
    voice never opened a file — the review-layer shape of chief-wiggum#289
    (absence-of-reading rendering as success).

    Applies only when ``role.requires_repo_read`` — a role like
    ``design_critic`` (reviews rendered screenshots) or ``divergence``
    (exists to widen textual distribution) was never asked to read the repo,
    so never touching it is not a finding, it's ``inapplicable``.

    Within a repo-reading role, a provider declared text-only
    (``provider.reads_repo is False``) is skipped too — it already told the
    config it can't read files; that's not a silent surprise. Everything else
    is measured per call, not assumed from a static label: a provider capable
    of reading the repo under SOME conditions (e.g. ``gemini-vertex``'s
    diff-scoped retrieval, chief-wiggum#319) that got nothing to retrieve on
    THIS call is still reported blind on THIS call.

    Two distinct findings, both loud, neither a pass:
      - ``"blind"`` — ``tokens_in`` measured and within ``margin``x of the
        provider's own prompt's estimated size: it answered from the prompt
        alone, by definition (the ticket's own detection note).
      - ``"unmeasured"`` — usage could not be measured at all. A provider that
        cannot be measured is not the same as one measured and fine.
    """
    if not role.requires_repo_read:
        return BlindnessReport(role=role.name, requires_repo_read=False)

    findings: list[BlindnessFinding] = []
    checked = 0
    for result in results:
        if result.status != "ok":
            continue  # already visible as a quorum failure
        provider = providers_by_name.get(result.name)
        if provider is None or not provider.reads_repo:
            continue  # declared text-only — not a surprise, nothing to detect
        checked += 1
        prompt_tokens = prompt_tokens_by_provider.get(result.name)
        if result.tokens_in is None:
            findings.append(BlindnessFinding(
                provider=result.name, kind="unmeasured", tokens_in=None,
                prompt_tokens_estimate=prompt_tokens,
                message=(
                    f"{result.name} is declared repo-reading and required by role "
                    f"{role.name!r}, but its tokens_in could not be measured "
                    f"(usage_status={result.usage_status!r}). A provider that cannot "
                    "be measured is not the same as one measured and fine — do not "
                    "count this voice as confirmed-informed."
                ),
            ))
            continue
        if prompt_tokens is not None and result.tokens_in <= prompt_tokens * margin:
            findings.append(BlindnessFinding(
                provider=result.name, kind="blind", tokens_in=result.tokens_in,
                prompt_tokens_estimate=prompt_tokens,
                message=(
                    f"{result.name}'s tokens_in ({result.tokens_in}) is within {margin}x "
                    f"of its own prompt's estimated size (~{prompt_tokens} tokens) in role "
                    f"{role.name!r} — it answered from the prompt text alone, not the repo. "
                    "A provider whose tokens_in is ~the prompt size did not read the repo, "
                    "by definition (chief-wiggum#319)."
                ),
            ))
    return BlindnessReport(
        role=role.name, requires_repo_read=True, findings=findings, providers_checked=checked
    )


@dataclass
class ImageBlindnessFinding:
    """One provider composed into an image-sending role despite being
    declared unable to receive images (chief-wiggum#321)."""

    provider: str
    required: bool
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ImageBlindnessReport:
    """chief-wiggum#321: does every provider a role SENDS IMAGES to actually
    declare that it can receive them? The same four-state gate vocabulary as
    ``BlindnessReport`` (chief-wiggum#289): ``pass`` / ``findings`` /
    ``inapplicable`` / ``error``. A report, never a gate — attached to the
    quorum manifest alongside the repo-read ``BlindnessReport``, and computed
    unconditionally (it needs no ``prompt``, unlike ``BlindnessReport``,
    because it is a structural declaration check, not a per-call
    measurement — see ``detect_image_blind_providers``).
    """

    role: str
    sends_images: bool
    findings: list[ImageBlindnessFinding] = field(default_factory=list)
    providers_checked: int = 0
    error: str | None = None

    @property
    def applicability(self) -> str:
        if self.error:
            return "error"
        if not self.sends_images:
            return "inapplicable"
        return "applicable"

    @property
    def outcome(self) -> str:
        """The standard four-state gate outcome (#289): pass | findings |
        inapplicable | error. Derived, never stored."""
        if self.applicability in ("error", "inapplicable"):
            return self.applicability
        return "findings" if self.findings else "pass"

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "sends_images": self.sends_images,
            "applicability": self.applicability,
            "outcome": self.outcome,
            "providers_checked": self.providers_checked,
            "error": self.error,
            "findings": [f.to_dict() for f in self.findings],
        }


def detect_image_blind_providers(
    role: Role,
    providers_by_name: dict[str, Provider],
) -> ImageBlindnessReport:
    """chief-wiggum#321: catch a role that SENDS images (``design_critic``
    critiques rendered screenshots) composed with a required or optional
    provider declared unable to RECEIVE images (``provider.accepts_images``
    is ``False``) — the blindness shape #319's ``detect_blind_providers``
    cannot see, because that check keys on ``requires_repo_read``/
    ``reads_repo`` (a repo-reading gap), and ``design_critic`` is correctly
    ``requires_repo_read=False``: it was never asking a provider to read the
    repo, so nothing there is anomalous. The gap is a different axis
    entirely — images, not repo access.

    Unlike ``detect_blind_providers``, this is a STRUCTURAL/declaration
    check, not a per-call measurement: whether a provider's call path can
    receive image bytes at all is a fixed property of that call path (an
    attachment mechanism either exists in the adapter or it doesn't), not
    something that varies call to call. That means the defect this catches
    — a role sending images to a provider wired to only ever see text — is
    checkable from config alone, BEFORE any provider ever runs, which is
    the whole point: the next role that sends images must be caught by
    declaring it correctly, not by noticing after the fact that a critique
    read suspiciously text-only (the ticket's own note that a token-floor
    check can look "large enough" while carrying zero image content).

    ``role.sends_images is False`` (every shipped role except
    ``design_critic``) is ``inapplicable`` — a role that never sends images
    was never asking a provider to look at one.
    """
    if not role.sends_images:
        return ImageBlindnessReport(role=role.name, sends_images=False)

    findings: list[ImageBlindnessFinding] = []
    checked = 0
    for name in list(role.required) + list(role.optional):
        provider = providers_by_name.get(name)
        if provider is None:
            continue  # not enabled/available — a plan gap visible elsewhere
        checked += 1
        if provider.accepts_images:
            continue
        required = name in role.required
        findings.append(ImageBlindnessFinding(
            provider=name,
            required=required,
            message=(
                f"{name} is a {'required' if required else 'optional'} provider "
                f"of role {role.name!r}, which sends images, but {name} is "
                "declared unable to accept images (provider.accepts_images="
                "False) — it critiques a written description of the images, "
                "never the rendered pixels (chief-wiggum#321)."
            ),
        ))
    return ImageBlindnessReport(
        role=role.name, sends_images=True, findings=findings, providers_checked=checked
    )


@dataclass
class QuorumManifest:
    role: str
    results: list[ProviderResult] = field(default_factory=list)
    # None when the caller didn't ask for the blindness check (no ``prompt``
    # passed to ``run_role_quorum``) — distinct from a check that RAN and
    # found nothing (chief-wiggum#319).
    blindness: BlindnessReport | None = None
    # chief-wiggum#321: unlike ``blindness``, this needs no ``prompt`` (it's a
    # static declaration check — see ``detect_image_blind_providers``), so it
    # is always computed once there is at least one task to check. ``None``
    # only when the plan had no tasks at all.
    image_blindness: ImageBlindnessReport | None = None

    @property
    def ok(self) -> bool:
        """True iff every required provider produced valid output."""
        return all(r.status == "ok" for r in self.results if r.required)

    @property
    def failed_required(self) -> list[str]:
        return [r.name for r in self.results if r.required and r.status != "ok"]

    def to_dict(self) -> dict:
        d = {
            "role": self.role,
            "ok": self.ok,
            "failed_required": self.failed_required,
            "results": [r.to_dict() for r in self.results],
        }
        if self.blindness is not None:
            d["blindness"] = self.blindness.to_dict()
        if self.image_blindness is not None:
            d["image_blindness"] = self.image_blindness.to_dict()
        return d


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
    last_failure_kind: str | None = None
    attempt = 0
    # chief-wiggum#330: check ONCE, before the first call — never per-attempt
    # (the callable's signature doesn't change between attempts, and a
    # per-attempt check would be wasted introspection).
    retry_context_aware = execute_accepts_retry_context(execute)
    for attempt in range(1, attempts_allowed + 1):
        if attempt > 1:
            # Short backoff before a retry (chief-wiggum#330) — not before
            # the first attempt, which should run immediately.
            time.sleep(_retry_backoff_seconds(attempt, last_failure_kind))
        try:
            if retry_context_aware:
                outcome = execute(provider, attempt=attempt, previous_failure_kind=last_failure_kind)
            else:
                outcome = execute(provider)
        except Exception as exc:  # noqa: BLE001 - any provider failure is retryable
            last_failure_kind = classify_failure(exc)
            last_error = f"execution failed: {exc}"
            continue
        # ``execute`` may return a bare string (the original contract, still
        # used by every pre-#319 caller/test) or a ``(text, usage)`` pair
        # (consult_ai.consult_provider and the review pipeline, chief-wiggum#319).
        # Usage is read duck-typed, never imported, so this module stays
        # decoupled from consult_ai.Usage.
        if isinstance(outcome, tuple):
            text, usage = outcome
        else:
            text, usage = outcome, None
        problem = validate_output(text, min_bytes=min_bytes)
        if problem:
            last_error = problem
            continue
        ok_path.write_text(text)
        return ProviderResult(
            provider.name, required, "ok", str(ok_path), attempt, None,
            tokens_in=getattr(usage, "tokens_in", None),
            tokens_out=getattr(usage, "tokens_out", None),
            usage_status=getattr(usage, "usage_status", None),
            resolved_model=getattr(usage, "resolved_model", None),
        )

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
    prompt: str | None = None,
    lenses: dict[str, Any] | None = None,
    blindness_margin: float = BLIND_PROVIDER_MARGIN,
    quorum_timeout: float | None = DEFAULT_QUORUM_TIMEOUT_SECONDS,
) -> QuorumManifest:
    """Run a role's providers concurrently with retries and output validation.

    Required and optional providers run in parallel. Required providers are
    retried up to ``max_attempts`` times; optional providers fail without
    blocking the quorum. A ``{role}-manifest.json`` records per-provider status.

    ``prompt`` (chief-wiggum#319): the SHARED prompt every provider in this
    role started from, BEFORE per-provider lens rendering — pass it (with
    ``lenses`` when the role uses any) to also run the blindness check
    (``detect_blind_providers``) and attach its ``BlindnessReport`` to the
    manifest. Omitted (the default) means "the caller didn't ask" — the
    manifest's ``blindness`` stays ``None``, distinct from a check that ran
    and found nothing. chief-wiggum#330: when given, ``prompt`` is ALSO
    refused with ``ShortPromptError`` (before any provider task is
    submitted) when it's below ``MIN_PROMPT_BYTES`` — generalizing
    consult_ai.py's own CLI-level pre-check to every caller of this function.

    ``quorum_timeout`` (chief-wiggum#330): upper bound (seconds) on this
    call's OWN wall clock, independent of any per-provider timeout — a
    provider whose call ignores its own budget (a bug elsewhere) is
    abandoned as a failure at this deadline rather than blocking the whole
    quorum forever. Defaults to a generous ceiling that should never fire in
    normal operation; pass ``None`` to fully opt out (the pre-#330
    unbounded behavior).
    """
    if prompt is not None:
        prompt_bytes = len(prompt.strip().encode("utf-8"))
        if prompt_bytes < MIN_PROMPT_BYTES:
            raise ShortPromptError(
                f"shared prompt is only {prompt_bytes} bytes (minimum {MIN_PROMPT_BYTES}) "
                "— refusing to run the quorum. This is the signature of a truncated or "
                "empty prompt (chief-wiggum#163/#330); fix it before spending any "
                "provider call on it."
            )

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
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        future_map = {
            pool.submit(
                _run_one_provider,
                provider, required, execute, out, plan.role.name, max_attempts, min_bytes,
            ): (provider, required)
            for provider, required in tasks
        }
        completed: set[concurrent.futures.Future] = set()
        try:
            for fut in concurrent.futures.as_completed(future_map, timeout=quorum_timeout):
                completed.add(fut)
                results.append(fut.result())
        except concurrent.futures.TimeoutError:
            # chief-wiggum#330: the quorum-level backstop fired. Every task
            # not already collected above is abandoned as a failure — a
            # thread that's still running cannot be killed (Python threads
            # aren't forcibly cancellable, only OS processes are, via
            # _kill_group in consult_ai.py's subprocess path), but the
            # CALLER must not wait on it any longer than this deadline.
            for fut, (provider, required) in future_map.items():
                if fut in completed:
                    continue
                if fut.done():
                    results.append(fut.result())
                    continue
                results.append(ProviderResult(
                    provider.name, required, "failed", None, 0,
                    f"abandoned: quorum deadline of {quorum_timeout}s exceeded",
                ))
        finally:
            # wait=False: don't block returning on a task that ignored its
            # own timeout — see the TimeoutError branch's comment above.
            pool.shutdown(wait=False)

    # Deterministic order: required (config order) then optional.
    results.sort(key=lambda r: order.get(r.name, 1_000))
    manifest = QuorumManifest(plan.role.name, results)

    providers_by_name = {p.name: p for p, _ in tasks}

    # chief-wiggum#321: structural, needs no ``prompt`` — computed every run,
    # unlike the token-floor ``blindness`` check below.
    try:
        manifest.image_blindness = detect_image_blind_providers(plan.role, providers_by_name)
    except Exception as exc:  # noqa: BLE001 - the check itself must never break the quorum
        manifest.image_blindness = ImageBlindnessReport(
            role=plan.role.name, sends_images=plan.role.sends_images,
            error=f"image blindness check failed: {exc}",
        )

    if prompt is not None:
        try:
            prompt_tokens_by_provider = {
                name: estimate_prompt_tokens(
                    prompt_for_provider(plan.role, name, prompt, lenses or {})
                )
                for name in seen
            }
            manifest.blindness = detect_blind_providers(
                plan.role, providers_by_name, results, prompt_tokens_by_provider,
                margin=blindness_margin,
            )
        except Exception as exc:  # noqa: BLE001 - the check itself must never break the quorum
            manifest.blindness = BlindnessReport(
                role=plan.role.name, requires_repo_read=plan.role.requires_repo_read,
                error=f"blindness check failed: {exc}",
            )

    if write_manifest:
        (out / f"{plan.role.name}-manifest.json").write_text(
            json.dumps(manifest.to_dict(), indent=2)
        )
    return manifest
