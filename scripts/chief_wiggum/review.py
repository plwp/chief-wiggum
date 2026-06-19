"""Review prompt assembly and review run (P1-7).

`/implement` Step 7 is mandatory and repeated in wave sub-agent prompts: capture
the diff, assemble a review prompt from templates + epic artifacts, run the
reviewer provider quorum, validate outputs, and produce synthesis inputs. This
module makes that deterministic pipeline one tested helper.

The pure parts (template substitution, diff truncation, synthesis prompt) are
unit-testable; git and provider execution are injected.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import providers

Runner = Callable[..., subprocess.CompletedProcess]

# Truncate very large diffs so a provider call isn't blown past its context.
DEFAULT_MAX_DIFF_BYTES = 200_000


class ReviewError(RuntimeError):
    """Raised when a review cannot be set up (not a git repo, no base, etc.)."""


@dataclass
class TicketContext:
    number: int | None
    title: str
    body: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> TicketContext:
        ac = data.get("acceptance_criteria") or data.get("ac") or []
        if isinstance(ac, str):
            ac = [line.strip("-* ").strip() for line in ac.splitlines() if line.strip()]
        return cls(
            number=data.get("number"),
            title=data.get("title", ""),
            body=data.get("body", ""),
            acceptance_criteria=list(ac),
        )


# --- pure assembly ----------------------------------------------------------


def _format_acceptance(criteria: list[str]) -> str:
    if not criteria:
        return "(none specified)"
    return "\n".join(f"- {c}" for c in criteria)


def assemble_review_prompt(
    template: str,
    ticket: TicketContext,
    diff: str,
    *,
    checklist: str | None = None,
    epic_sections: Iterable[tuple[str, str]] = (),
) -> str:
    """Substitute the review template and append the checklist + epic context.

    Substitution is **single-pass** (one regex sweep over the template), so a
    value that itself contains a token name (e.g. a ticket body mentioning
    ``{{DIFF}}``) is never re-scanned and replaced. Braces in the diff are not
    interpreted as format fields.
    """
    replacements = {
        "TICKET_TITLE": ticket.title or "(untitled)",
        "TICKET_DESCRIPTION": ticket.body or "(no description)",
        "ACCEPTANCE_CRITERIA": _format_acceptance(ticket.acceptance_criteria),
        "DIFF": diff,
    }
    prompt = re.sub(
        r"\{\{(TICKET_TITLE|TICKET_DESCRIPTION|ACCEPTANCE_CRITERIA|DIFF)\}\}",
        lambda m: replacements[m.group(1)],
        template,
    )

    extra: list[str] = []
    for title, content in epic_sections:
        if content and content.strip():
            extra.append(f"\n\n## {title}\n\n{content.strip()}")
    if checklist and checklist.strip():
        extra.append(f"\n\n---\n\n{checklist.strip()}")
    return prompt + "".join(extra)


def truncate_diff(diff: str, max_bytes: int = DEFAULT_MAX_DIFF_BYTES) -> str:
    encoded = diff.encode("utf-8")
    if len(encoded) <= max_bytes:
        return diff
    head = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return head + f"\n\n... [diff truncated at {max_bytes} bytes of {len(encoded)}] ..."


def build_synthesis_prompt(response_paths: list[str]) -> str:
    listing = "\n".join(f"- {p}" for p in response_paths) or "- (no reviewer responses)"
    return (
        "Synthesize the independent code reviews below into one actionable report.\n"
        "Categorize each finding as high-confidence (apply), medium (verify first), "
        "or low/architectural (flag for user). Note consensus vs single-reviewer findings.\n\n"
        f"Reviewer responses:\n{listing}\n"
    )


# --- git --------------------------------------------------------------------


def _git(args: list[str], cwd: str | Path, runner: Runner) -> subprocess.CompletedProcess:
    return runner(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)


def assert_git_repo(worktree: str | Path, *, runner: Runner = subprocess.run) -> Path:
    result = _git(["rev-parse", "--show-toplevel"], worktree, runner)
    if result.returncode != 0:
        raise ReviewError(f"not a git repository: {worktree}")
    return Path(result.stdout.strip())


def capture_diff(
    worktree: str | Path,
    base: str,
    *,
    runner: Runner = subprocess.run,
    max_bytes: int = DEFAULT_MAX_DIFF_BYTES,
) -> str:
    """Capture ``base...HEAD`` diff, refusing if the base ref can't be resolved."""
    check = _git(["rev-parse", "--verify", base], worktree, runner)
    if check.returncode != 0:
        raise ReviewError(f"base ref cannot be resolved: {base}")
    result = _git(["diff", f"{base}...HEAD"], worktree, runner)
    if result.returncode != 0:
        raise ReviewError(f"git diff failed: {(result.stderr or '').strip()}")
    return truncate_diff(result.stdout, max_bytes)


# --- run --------------------------------------------------------------------


@dataclass
class ReviewManifest:
    ticket: int | None
    base: str
    role: str
    diff_path: str
    prompt_path: str
    synthesis_prompt_path: str
    provider_manifest: dict
    response_paths: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.provider_manifest.get("ok"))

    def to_dict(self) -> dict:
        return asdict(self)


def run_review(
    ticket: TicketContext,
    worktree: str | Path,
    base: str,
    output_dir: str | Path,
    *,
    template: str,
    checklist: str | None = None,
    epic_sections: Iterable[tuple[str, str]] = (),
    role: str = "reviewer",
    config: dict | None = None,
    execute: Callable[[providers.Provider, str], str] | None = None,
    runner: Runner = subprocess.run,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
) -> ReviewManifest:
    """Assemble the review prompt, run the reviewer quorum, write synthesis inputs.

    Refuses to run outside a git repo or when ``base`` cannot be resolved.
    ``execute`` (the provider call) is injected so the pipeline is testable.
    """
    assert_git_repo(worktree, runner=runner)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    diff = capture_diff(worktree, base, runner=runner, max_bytes=max_diff_bytes)
    diff_path = out / "impl-diff.txt"
    diff_path.write_text(diff)

    prompt = assemble_review_prompt(
        template, ticket, diff, checklist=checklist, epic_sections=epic_sections
    )
    prompt_path = out / "review-prompt.md"
    prompt_path.write_text(prompt)

    if config is None:
        config = providers.load_config()
    plan = providers.plan_role(role, config)
    if not plan.ok:
        raise ReviewError(
            f"role {role} missing required providers: {', '.join(plan.missing_required)}"
        )
    if execute is None:
        raise ReviewError("an execute callable is required to run the reviewer quorum")

    # The quorum calls execute(provider); bind the assembled prompt here.
    quorum = providers.run_role_quorum(plan, lambda p: execute(p, prompt), out)
    response_paths = [r.path for r in quorum.results if r.path]

    synthesis = build_synthesis_prompt(response_paths)
    synthesis_path = out / "synthesis-prompt.md"
    synthesis_path.write_text(synthesis)

    manifest = ReviewManifest(
        ticket=ticket.number,
        base=base,
        role=role,
        diff_path=str(diff_path),
        prompt_path=str(prompt_path),
        synthesis_prompt_path=str(synthesis_path),
        provider_manifest=quorum.to_dict(),
        response_paths=response_paths,
    )
    (out / "review-manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2))
    return manifest
