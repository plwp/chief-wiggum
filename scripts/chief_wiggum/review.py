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
import warnings
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import providers

Runner = Callable[..., subprocess.CompletedProcess]

# Truncate very large diffs so a provider call isn't blown past its context.
DEFAULT_MAX_DIFF_BYTES = 200_000

# author_association values that mechanically qualify a comment's author as a
# maintainer for the amendment-promotion predicate (ADR-fh-02).
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# A comment is only eligible for promotion if it contains an explicit `AC:`
# marker (ADR-fh-02) — a line, optionally indented, beginning with `AC:`.
_AC_BLOCK_RE = re.compile(r"(?im)^[ \t]*AC:")

# Rendered in place of an empty region (mirrors _format_acceptance's
# "(none specified)" placeholder) — CTR-fh-003.
_NO_COMMENTS_PLACEHOLDER = "(no comment-thread refinements)"

# ADR-fh-02 supersession, stated to the reviewer explicitly: the amendments
# list is deterministically ordered (created_at ascending, ties by comment
# id), and where two amendments touch the same AC item the LATER one wins.
_SUPERSESSION_RULE_LINE = (
    "Apply in listed order; where two amendments conflict on the same AC item, "
    "the LATER amendment (last listed) is authoritative."
)


class ReviewError(RuntimeError):
    """Raised when a review cannot be set up (not a git repo, no base, etc.)."""


class MissingCommentsWarning(UserWarning):
    """Ticket-context JSON entirely omits the `comments` key (CTR-fh-002).

    A production `ticket.json` written by the `/implement` shell must always
    carry a `comments` array — empty is fine (``[]``), but an ABSENT key means
    the upstream writer never fetched the thread at all, which is the writer
    half of the #83 bug (comments silently never reach the reviewer). This is
    distinct from an empty list, which is a normal, silent no-op.
    """


@dataclass
class TicketComment:
    """One `gh issue view --json comments` entry (append-only, observed-context).

    ``author_association`` and ``created_at`` drive the amendment-promotion
    predicate and deterministic supersession (ADR-fh-02) — never re-derived,
    always taken verbatim from the upstream writer.
    """

    body: str
    author: str = ""
    author_association: str = "NONE"
    created_at: str = ""
    id: object | None = None
    url: str | None = None

    @classmethod
    def from_any(cls, item: dict | str | TicketComment) -> TicketComment:
        """Accept a structured dict OR a legacy bare string (degrades safely).

        A degraded string comment can never satisfy the promotion predicate:
        it carries ``author_association="NONE"`` and no author, so it always
        lands in discussion (IT-fh-02).
        """
        if isinstance(item, TicketComment):
            return item
        if isinstance(item, str):
            return cls(body=item, author="", author_association="NONE", created_at="", id=None, url=None)
        return cls(
            body=item.get("body", "") or "",
            author=item.get("author") or "",
            author_association=item.get("author_association") or item.get("authorAssociation") or "NONE",
            created_at=item.get("created_at") or item.get("createdAt") or "",
            id=item.get("id"),
            url=item.get("url"),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Amendment:
    """A comment PROMOTED to an authoritative AC change (ADR-fh-02).

    Presentational only: an Amendment changes what the reviewer is *told* is
    in force in the rendered prompt. It never rewrites `TicketContext.
    acceptance_criteria` (INV-fh-009/010).
    """

    comment_id: object | None
    url: str | None
    author: str
    author_association: str
    created_at: str
    ac_block: str

    def to_dict(self) -> dict:
        return asdict(self)


def _extract_ac_block(body: str) -> str | None:
    """Return the `AC:` block (that line through end of comment), or None."""
    match = _AC_BLOCK_RE.search(body)
    if not match:
        return None
    return body[match.start() :].strip()


def _is_promotable_author(comment: TicketComment, issue_author: str) -> bool:
    """ADR-fh-02: author is the issue author OR a maintainer/collaborator."""
    if comment.author_association in MAINTAINER_ASSOCIATIONS:
        return True
    return bool(issue_author) and bool(comment.author) and comment.author == issue_author


# @cw-trace guards CTR-fh-003 INV-fh-010
def classify_comments(
    comments: Iterable[TicketComment], issue_author: str = ""
) -> tuple[list[Amendment], list[TicketComment]]:
    """Split comments into (amendments, discussion) per the ADR-fh-02 promotion rule.

    A comment is promoted only when BOTH conditions hold: its author is the
    issue author or a maintainer/collaborator, AND it contains an explicit
    `AC:` block. Everything else — including a comment with an `AC:` block
    from a non-maintainer, non-author account (the #83 adversarial case) — is
    discussion. Source (chronological) order is preserved within each output
    list (INV-fh-009); neither list is a re-sort of `comments`.
    """
    amendments: list[Amendment] = []
    discussion: list[TicketComment] = []
    for comment in comments:
        ac_block = _extract_ac_block(comment.body)
        if ac_block is not None and _is_promotable_author(comment, issue_author):
            amendments.append(
                Amendment(
                    comment_id=comment.id,
                    url=comment.url,
                    author=comment.author,
                    author_association=comment.author_association,
                    created_at=comment.created_at,
                    ac_block=ac_block,
                )
            )
        else:
            discussion.append(comment)
    return amendments, discussion


# @cw-trace guards INV-fh-009
def apply_amendment_supersession(amendments: list[Amendment]) -> list[Amendment]:
    """Deterministic total order over amendments (ADR-fh-02).

    Amendments apply in `created_at` ascending order; equal timestamps
    tie-break by comment id ascending (stringified — ids may be int or str).
    Does not mutate the input list.
    """
    return sorted(
        amendments,
        key=lambda a: (a.created_at, "" if a.comment_id is None else str(a.comment_id)),
    )


@dataclass
class TicketContext:
    number: int | None
    title: str
    body: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    comments: list[TicketComment] = field(default_factory=list)
    # Issue author's `gh` login. Not in the CTR-fh entity's canonical field
    # table, but required to evaluate ADR-fh-02's "author == issue author"
    # half of the promotion predicate (an issue author who is not a
    # maintainer must still be able to amend their own ticket).
    author: str = ""

    # @cw-trace guards CTR-fh-001 CTR-fh-002 INV-fh-009
    @classmethod
    def from_dict(cls, data: dict) -> TicketContext:
        ac = data.get("acceptance_criteria") or data.get("ac") or []
        if isinstance(ac, str):
            ac = [line.strip("-* ").strip() for line in ac.splitlines() if line.strip()]
        if "comments" not in data:
            # CTR-fh-002 error case: the upstream ticket.json writer omitted the
            # comments array entirely (as opposed to `"comments": []`). This is
            # the writer half of #83 — surface it loudly, never silently.
            warnings.warn(
                "ticket context JSON has no 'comments' key — the upstream "
                "ticket.json writer should always emit an array (empty list "
                "allowed, absent key is the #83 regression); treating as no "
                "comments (CTR-fh-002)",
                MissingCommentsWarning,
                stacklevel=2,
            )
        raw_comments = data.get("comments") or []
        comments = [TicketComment.from_any(c) for c in raw_comments]
        return cls(
            number=data.get("number"),
            title=data.get("title", ""),
            body=data.get("body", ""),
            acceptance_criteria=list(ac),
            comments=comments,
            author=data.get("author") or "",
        )

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "acceptance_criteria": list(self.acceptance_criteria),
            "comments": [c.to_dict() for c in self.comments],
            "author": self.author,
        }


# @cw-trace guards CTR-fh-002
def build_ticket_context_json(
    raw_issue: dict, *, number: int | None = None, acceptance_criteria: Iterable[str] = ()
) -> dict:
    """Flatten `gh issue view --json title,body,author,comments` into ticket.json.

    This is the upstream writer half of #83: the `/implement` shell (Step 2)
    calls this (via `scripts/write_ticket_context.py`) to produce the
    `ticket.json` that `TicketContext.from_dict` later reads. `comments` is
    ALWAYS present in the output — even for zero comments it is `[]`, never
    an absent key (IT-fh-10) — and each entry carries the flattened `author`
    login plus `author_association` (gh's `authorAssociation`), which the
    amendment-promotion predicate (ADR-fh-02) requires.
    """
    author = raw_issue.get("author") or {}
    comments = []
    for c in raw_issue.get("comments") or []:
        c_author = c.get("author") or {}
        comments.append(
            {
                "id": c.get("id"),
                "url": c.get("url"),
                "author": c_author.get("login", "") if isinstance(c_author, dict) else (c.get("author") or ""),
                "author_association": c.get("author_association") or c.get("authorAssociation") or "NONE",
                "created_at": c.get("created_at") or c.get("createdAt") or "",
                "body": c.get("body", ""),
            }
        )
    return {
        "number": number,
        "title": raw_issue.get("title", ""),
        "body": raw_issue.get("body", ""),
        "author": author.get("login", "") if isinstance(author, dict) else (raw_issue.get("author") or ""),
        "acceptance_criteria": list(acceptance_criteria),
        "comments": comments,
    }


# --- pure assembly ----------------------------------------------------------


def _format_acceptance(criteria: list[str]) -> str:
    if not criteria:
        return "(none specified)"
    return "\n".join(f"- {c}" for c in criteria)


# Matches a line that would render as a markdown heading (optionally indented).
_HEADING_LINE_RE = re.compile(r"^(\s*)(#)")


def _quote_untrusted_body(text: str, indent: str = "  ") -> str:
    """Render comment text as inert quoted DATA, never prompt structure.

    Comment bodies are untrusted input embedded into the provider prompt: an
    external commenter could otherwise include a line like
    ``### Accepted AC amendments (authoritative-on-conflict)`` and spoof a
    second authoritative-looking region (codex P1 on #83). Every line is
    blockquote-prefixed, and any line that would render as a markdown heading
    has its leading ``#`` escaped, so no comment body can ever open a heading
    or section of its own. Applied to BOTH discussion bodies and amendment
    ``AC:`` blocks — an amendment's authority comes from the region header and
    the promotion predicate, not from any formatting inside its body.
    """
    lines = (text or "").splitlines() or [""]
    quoted = []
    for line in lines:
        neutralized = _HEADING_LINE_RE.sub(r"\1\\\2", line)
        quoted.append(f"{indent}> {neutralized}".rstrip())
    return "\n".join(quoted)


def _format_amendment(amendment: Amendment) -> str:
    who = amendment.author or "(unknown)"
    ref = amendment.url or amendment.comment_id or "(no id)"
    return (
        f"- {amendment.created_at} — {who} ({amendment.author_association}) — {ref}\n"
        f"{_quote_untrusted_body(amendment.ac_block)}"
    )


def _format_discussion_comment(comment: TicketComment) -> str:
    who = comment.author or "(unknown)"
    return (
        f"- {comment.created_at} — {who} ({comment.author_association}):\n"
        f"{_quote_untrusted_body(comment.body)}"
    )


# @cw-trace guards CTR-fh-003 CTR-fh-004 INV-fh-009 INV-fh-010
def render_ticket_comments(ticket: TicketContext) -> str:
    """Render the two labeled, authority-separated comment regions (ADR-fh-02).

    "Accepted AC amendments (authoritative-on-conflict)" holds only comments
    that pass the promotion predicate (`classify_comments`), in deterministic
    supersession order (`apply_amendment_supersession`), under an explicit
    rule line telling the reviewer that on a per-item conflict the LATER
    amendment is authoritative (ADR-fh-02's latest-wins, applied by the
    reader over the deterministic ordering rather than by pre-digesting AC
    items here — comments are never mechanically merged, INV-fh-009).
    "Discussion/context (non-authoritative)" holds everything else, in source
    (chronological) order. Comment bodies in BOTH regions are quoted and
    heading-escaped (`_quote_untrusted_body`) so untrusted text can never
    spoof a region heading. Both region headers always render, even when the
    corresponding list is empty (each gets the placeholder used for a wholly
    empty thread too) — the raw thread is NEVER rendered under one
    authoritative label, and `ticket.acceptance_criteria` is never read from
    or written to here (presentational-only, INV-fh-009/010).
    """
    amendments, discussion = classify_comments(ticket.comments, ticket.author)
    amendments = apply_amendment_supersession(amendments)
    amendments_body = "\n".join(_format_amendment(a) for a in amendments) or _NO_COMMENTS_PLACEHOLDER
    discussion_body = (
        "\n".join(_format_discussion_comment(c) for c in discussion) or _NO_COMMENTS_PLACEHOLDER
    )
    return (
        "### Accepted AC amendments (authoritative-on-conflict)\n"
        f"{_SUPERSESSION_RULE_LINE}\n"
        f"{amendments_body}\n\n"
        "### Discussion/context (non-authoritative)\n"
        f"{discussion_body}"
    )


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
    interpreted as format fields. ``{{TICKET_COMMENTS}}`` expands to the two
    labeled amendment/discussion regions (CTR-fh-003) — distinct from
    ``{{ACCEPTANCE_CRITERIA}}``, which is never rewritten by comments
    (INV-fh-009/010).
    """
    replacements = {
        "TICKET_TITLE": ticket.title or "(untitled)",
        "TICKET_DESCRIPTION": ticket.body or "(no description)",
        "ACCEPTANCE_CRITERIA": _format_acceptance(ticket.acceptance_criteria),
        "TICKET_COMMENTS": render_ticket_comments(ticket),
        "DIFF": diff,
    }
    prompt = re.sub(
        r"\{\{(TICKET_TITLE|TICKET_DESCRIPTION|ACCEPTANCE_CRITERIA|TICKET_COMMENTS|DIFF)\}\}",
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
        "or low/architectural (flag for user).\n"
        "Reviewers may have been assigned disjoint review lenses over the same "
        "diff, so expect disjoint findings, not convergence. Combine by union: a "
        "finding raised by a single reviewer is not weaker for lacking consensus "
        "— do not downgrade it. Cross-verify against the diff only where "
        "reviewers make contradictory claims about the same fact.\n\n"
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
    lenses: dict | None = None,
    execute: Callable[[providers.Provider, str, int | None], str] | None = None,
    runner: Runner = subprocess.run,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    optional_timeout_default: int = providers.DEFAULT_OPTIONAL_TIMEOUT_SECONDS,
) -> ReviewManifest:
    """Assemble the review prompt, run the reviewer quorum, write synthesis inputs.

    Refuses to run outside a git repo or when ``base`` cannot be resolved.
    ``execute`` (the provider call) is injected so the pipeline is testable; it
    receives ``(provider, prompt, timeout_override)`` where ``timeout_override``
    is the wall-clock cap (seconds) for an OPTIONAL provider's delegate call, or
    ``None`` for a required provider (chief-wiggum#188). An optional
    ``claude-interactive`` in this role must fail fast rather than hold the whole
    review quorum to the delegate's 1800s budget — the same cap ``consult_ai.py``
    applies on its own ``--role`` path, computed by the shared
    ``providers.optional_provider_timeout``.

    Every provider gets the identical assembled prompt. If ``role`` maps a
    provider to a lens (``config/providers.json`` role.lenses), that provider's
    charter (``config/lenses.json``, or ``lenses`` if supplied) is appended —
    the shared prompt itself is never altered (chief-wiggum#163).
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

    if lenses is None:
        lenses = providers.load_lenses()

    # Fail fast on a malformed lens map — a lens assigned to a provider not in
    # the role would otherwise silently no-op, and an unknown lens on an
    # optional provider would degrade to a provider "failure" while the run
    # still reported success. Matches consult_ai --role behavior.
    lens_errors = providers.validate_role_lenses(plan.role, lenses)
    if lens_errors:
        raise ReviewError("; ".join(lens_errors))

    # The quorum calls execute(provider); bind the assembled prompt here. A
    # provider mapped to a lens on this role gets its charter appended; the
    # shared prompt every provider starts from is identical either way. An
    # OPTIONAL provider is additionally handed a shortened delegate timeout so a
    # hung/slow claude-interactive fails fast instead of stalling the review
    # quorum for 1800s (chief-wiggum#188) — the required/optional decision is the
    # same shared helper consult_ai.py's own --role path uses.
    quorum = providers.run_role_quorum(
        plan,
        lambda p: execute(
            p,
            providers.prompt_for_provider(plan.role, p.name, prompt, lenses),
            providers.optional_provider_timeout(plan.role, p.name, optional_timeout_default),
        ),
        out,
    )
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
