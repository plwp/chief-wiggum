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


# chief-wiggum#332: a literal line separating a review-prompt template's
# STATIC preamble (task framing, review standard, output format — none of
# which ever reference a template var) from its VOLATILE per-ticket body
# (title/description/AC/comments/diff). Ordering static-first — and
# appending the checklist/epic sections to the STATIC half rather than the
# very end of the whole prompt — means two DIFFERENT tickets sharing the
# same template/checklist/epic artifacts produce a byte-identical prefix,
# which is what lets a provider-side prompt-prefix cache hit across tickets
# in the same epic/review role (the ticket_cost.py ledger already reads
# ``cache_read`` fields; the pre-#332 layout could never earn one). A
# template with no marker (every pre-#332 template) degrades to "entirely
# volatile" — every substitution/section still lands somewhere, it just
# isn't prefix-cacheable.
VOLATILE_MARKER = "<!-- CW:VOLATILE -->"

# A rendered comment thread is the one unbounded payload in the assembled
# prompt before #332 — the diff has DEFAULT_MAX_DIFF_BYTES, comments had no
# cap at all. Smaller than the diff cap: a review-shaped comment thread is
# rarely legitimately huge, and this is a floor against pathological cases
# (a long adversarial or bot-generated thread), not a routine truncation.
DEFAULT_MAX_COMMENTS_BYTES = 50_000


def truncate_text(text: str, max_bytes: int, *, label: str = "content") -> str:
    """Truncate ``text`` to ``max_bytes`` (UTF-8), appending a labeled marker
    stating the original size — the generic form ``truncate_diff`` below is
    now a thin wrapper over (chief-wiggum#332), so the diff and the comment
    thread share one truncation implementation rather than two copies that
    could drift."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    head = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return head + f"\n\n... [{label} truncated at {max_bytes} bytes of {len(encoded)}] ..."


def assemble_review_prompt(
    template: str,
    ticket: TicketContext,
    diff: str,
    *,
    checklist: str | None = None,
    epic_sections: Iterable[tuple[str, str]] = (),
    max_comments_bytes: int = DEFAULT_MAX_COMMENTS_BYTES,
) -> str:
    """Substitute the review template and assemble the checklist + epic context
    STATIC-FIRST, ticket content last (chief-wiggum#332).

    Substitution is **single-pass** (one regex sweep over the VOLATILE half
    of the template only), so a value that itself contains a token name
    (e.g. a ticket body mentioning ``{{DIFF}}``) is never re-scanned and
    replaced. Braces in the diff are not interpreted as format fields.
    ``{{TICKET_COMMENTS}}`` expands to the two labeled amendment/discussion
    regions (CTR-fh-003), capped like the diff — distinct from
    ``{{ACCEPTANCE_CRITERIA}}``, which is never rewritten by comments
    (INV-fh-009/010).

    ``template`` splits on ``VOLATILE_MARKER`` into a static prefix and a
    volatile suffix; ``checklist``/``epic_sections`` are appended to the
    STATIC prefix (not the end of the whole prompt, the pre-#332 layout) so
    two different tickets sharing the same static inputs produce a
    byte-identical prefix. A template with no marker is treated as entirely
    volatile (``static_prefix`` empty) — every substitution and appended
    section still lands in the output, it's simply not prefix-cacheable.
    """
    if VOLATILE_MARKER in template:
        static_prefix, volatile_body = template.split(VOLATILE_MARKER, 1)
    else:
        static_prefix, volatile_body = "", template

    replacements = {
        "TICKET_TITLE": ticket.title or "(untitled)",
        "TICKET_DESCRIPTION": ticket.body or "(no description)",
        "ACCEPTANCE_CRITERIA": _format_acceptance(ticket.acceptance_criteria),
        "TICKET_COMMENTS": truncate_text(
            render_ticket_comments(ticket), max_comments_bytes, label="comment thread"
        ),
        "DIFF": diff,
    }
    volatile_rendered = re.sub(
        r"\{\{(TICKET_TITLE|TICKET_DESCRIPTION|ACCEPTANCE_CRITERIA|TICKET_COMMENTS|DIFF)\}\}",
        lambda m: replacements[m.group(1)],
        volatile_body,
    )

    extra: list[str] = []
    for title, content in epic_sections:
        if content and content.strip():
            extra.append(f"\n\n## {title}\n\n{content.strip()}")
    if checklist and checklist.strip():
        extra.append(f"\n\n---\n\n{checklist.strip()}")

    static_rendered = static_prefix.rstrip() + "".join(extra)
    if not static_rendered:
        return volatile_rendered
    return static_rendered + "\n\n---\n\n" + volatile_rendered.lstrip()


def truncate_diff(diff: str, max_bytes: int = DEFAULT_MAX_DIFF_BYTES) -> str:
    return truncate_text(diff, max_bytes, label="diff")


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


@dataclass
class ResolvedBase:
    """Which ref a review diff was actually computed against (chief-wiggum#269).

    A worktree created fresh from ``origin/main`` still carries a LOCAL
    ``main`` ref that never advances on its own — the moment anything else
    merges upstream, that local ref is stale. Diffing ``stale_main...HEAD``
    (three-dot: merge-base(stale_main, HEAD)...HEAD) then pulls in every
    commit merged into origin/main since the local ref last moved, rendered
    as if it were part of THIS diff (confirmed live, #281: a 6-ahead/2-behind
    local ``main`` produced a ~13,800-line diff where the true PR diff was
    ~900 lines).

    ``ref`` is what ``capture_diff`` should be pointed at instead of the raw
    ``base`` the caller asked for. ``source`` is ``"remote-tracking"`` when a
    remote's freshly-fetched ref was used, or ``"local-fallback"`` when there
    was no usable remote — in which case ``fallback_reason`` is always set
    (never a silent substitution).
    """

    ref: str
    sha: str | None
    source: str
    fallback_reason: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _merge_base_sha(worktree: str | Path, ref: str, runner: Runner) -> str | None:
    result = _git(["merge-base", "HEAD", ref], worktree, runner)
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def resolve_review_base(
    worktree: str | Path,
    base: str,
    *,
    remote: str = "origin",
    runner: Runner = subprocess.run,
) -> ResolvedBase:
    """Resolve ``base`` to the freshly-fetched remote-tracking ref when one
    exists, falling back to the local ref name ONLY when there is no usable
    remote — and always recording WHY (chief-wiggum#269).

    Diffing three-dot against ``<remote>/<base>`` after a fetch is equivalent
    to diffing against ``merge-base(HEAD, <remote>/<base>)`` directly (that is
    what ``A...B`` means); ``sha`` is the resolved merge-base, reported for
    the manifest even though ``capture_diff`` is handed the ref name.
    """
    remotes_result = _git(["remote"], worktree, runner)
    remotes = [r.strip() for r in (remotes_result.stdout or "").splitlines() if r.strip()]
    if remote not in remotes:
        return ResolvedBase(
            ref=base,
            sha=_merge_base_sha(worktree, base, runner),
            source="local-fallback",
            fallback_reason=f"no '{remote}' remote configured — using local ref '{base}'",
        )

    fetch_result = _git(["fetch", remote, base], worktree, runner)
    if fetch_result.returncode != 0:
        return ResolvedBase(
            ref=base,
            sha=_merge_base_sha(worktree, base, runner),
            source="local-fallback",
            fallback_reason=(
                f"git fetch {remote} {base} failed "
                f"({(fetch_result.stderr or '').strip() or 'no output'}) — using local ref '{base}'"
            ),
        )

    remote_ref = f"{remote}/{base}"
    verify_result = _git(["rev-parse", "--verify", remote_ref], worktree, runner)
    if verify_result.returncode != 0:
        return ResolvedBase(
            ref=base,
            sha=_merge_base_sha(worktree, base, runner),
            source="local-fallback",
            fallback_reason=(
                f"{remote_ref} does not resolve after fetch — using local ref '{base}'"
            ),
        )

    return ResolvedBase(
        ref=remote_ref,
        sha=_merge_base_sha(worktree, remote_ref, runner),
        source="remote-tracking",
        fallback_reason=None,
    )


_SHORTSTAT_FILES_RE = re.compile(r"(\d+) files? changed")
_SHORTSTAT_INSERTIONS_RE = re.compile(r"(\d+) insertions?\(\+\)")
_SHORTSTAT_DELETIONS_RE = re.compile(r"(\d+) deletions?\(-\)")


def parse_diff_shortstat(text: str) -> dict:
    """Parse a ``git diff --shortstat`` line into files/insertions/deletions.

    Missing components (e.g. a deletions-only or insertions-only diff, or an
    empty diff) default to 0 rather than raising — this feeds a visibility
    signal (chief-wiggum#269), not a hard gate."""
    files_m = _SHORTSTAT_FILES_RE.search(text)
    ins_m = _SHORTSTAT_INSERTIONS_RE.search(text)
    del_m = _SHORTSTAT_DELETIONS_RE.search(text)
    files_changed = int(files_m.group(1)) if files_m else 0
    insertions = int(ins_m.group(1)) if ins_m else 0
    deletions = int(del_m.group(1)) if del_m else 0
    return {
        "files_changed": files_changed,
        "insertions": insertions,
        "deletions": deletions,
        "total_lines": insertions + deletions,
    }


def diff_shortstat(worktree: str | Path, base_ref: str, *, runner: Runner = subprocess.run) -> dict:
    """``git diff --shortstat <base_ref>...HEAD``, parsed. Computed from the
    UNTRUNCATED diff endpoints (not from ``capture_diff``'s possibly-truncated
    text) — the whole point is to see the true size of an oversized diff
    even once the captured text itself has been cut down (chief-wiggum#269)."""
    result = _git(["diff", "--shortstat", f"{base_ref}...HEAD"], worktree, runner)
    return parse_diff_shortstat(result.stdout or "")


# --- run --------------------------------------------------------------------


@dataclass
class ReviewManifest:
    ticket: int | None
    base: str
    role: str
    diff_path: str
    prompt_path: str
    provider_manifest: dict
    response_paths: list[str] = field(default_factory=list)
    # chief-wiggum#269: the base ACTUALLY diffed against (may differ from
    # `base` above, which is the raw caller-supplied ref name), how it was
    # resolved, and — when it fell back to the local ref — why. Never a
    # silent substitution: a reviewer's verdict can be traced to the exact
    # diff it saw.
    resolved_base_ref: str = ""
    resolved_base_sha: str | None = None
    base_source: str = ""
    base_fallback_reason: str | None = None
    # The diff's true size (files/insertions/deletions), computed from the
    # UNTRUNCATED diff so a wildly-oversized diff (#281: ~13,800 lines where
    # the true PR diff was ~900) is visible even once `impl-diff.txt` itself
    # has been truncated.
    diff_stat: dict = field(default_factory=dict)

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
    """Assemble the review prompt(s), run the reviewer quorum.

    Refuses to run outside a git repo or when ``base`` cannot be resolved.
    ``execute`` (the provider call) is injected so the pipeline is testable; it
    receives ``(provider, prompt, timeout_override)`` where ``timeout_override``
    is the wall-clock cap (seconds) for an OPTIONAL provider's delegate call, or
    ``None`` for a required provider (chief-wiggum#188). An optional
    ``claude-interactive`` in this role must fail fast rather than hold the whole
    review quorum to the delegate's 1800s budget — the same cap ``consult_ai.py``
    applies on its own ``--role`` path, computed by the shared
    ``providers.optional_provider_timeout``.

    Every provider gets the identical CONTENT (ticket, contracts, checklist,
    diff) and, if ``role`` maps it to a lens (``config/providers.json``
    role.lenses), that provider's charter appended (chief-wiggum#163) — but
    not necessarily the identical BYTES for the diff. A provider declared
    ``needs_inline_diff=False`` (real filesystem access — codex,
    claude-interactive) gets a pointer to the diff file/git command instead
    of the diff text itself (chief-wiggum#332); ``review-prompt.md`` on disk
    always carries the full inline version for a human to read.
    """
    assert_git_repo(worktree, runner=runner)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # chief-wiggum#269: resolve `base` against a freshly-fetched remote-tracking
    # ref rather than trusting the local ref name, which a fresh worktree
    # routinely leaves stale the moment anything else merges upstream.
    resolved = resolve_review_base(worktree, base, runner=runner)
    diff = capture_diff(worktree, resolved.ref, runner=runner, max_bytes=max_diff_bytes)
    diff_path = out / "impl-diff.txt"
    diff_path.write_text(diff)
    diff_stat = diff_shortstat(worktree, resolved.ref, runner=runner)

    # chief-wiggum#332: the diff is capped at max_diff_bytes and inlined into
    # EVERY provider's prompt — but only a provider with no real filesystem
    # access (Provider.needs_inline_diff=True, e.g. gemini-vertex's single
    # synchronous SDK call) actually needs that. A provider that runs with
    # real cwd access and its own tool loop (codex, claude, claude-interactive)
    # can open `impl-diff.txt` itself, or reproduce it with `git diff`; up to
    # ~100k avoidable input tokens per review for those. `prompt_inline` is
    # what review-prompt.md on disk always shows (the full, human-readable
    # prompt); `prompt_pointer` swaps the diff text for a pointer to the same
    # information, reachable via the file or the git command below.
    prompt_inline = assemble_review_prompt(
        template, ticket, diff, checklist=checklist, epic_sections=epic_sections
    )
    diff_pointer = (
        "[Not inlined here (chief-wiggum#332) — you have direct filesystem "
        "access; the diff below is identical to what every other reviewer sees.\n"
        f"Read it from: {diff_path.resolve()}\n"
        f"Or reproduce it yourself: git diff {resolved.ref}...HEAD   (run from {Path(worktree).resolve()})]"
    )
    prompt_pointer = assemble_review_prompt(
        template, ticket, diff_pointer, checklist=checklist, epic_sections=epic_sections
    )
    prompt_path = out / "review-prompt.md"
    prompt_path.write_text(prompt_inline)

    if config is None:
        config = providers.load_config()
    plan = providers.plan_role(role, config)
    if not plan.ok:
        raise ReviewError(
            f"role {role} missing required providers: {', '.join(plan.missing_required)}"
        )
    if execute is None:
        raise ReviewError("an execute callable is required to run the reviewer quorum")

    # Per-provider prompt body (chief-wiggum#332): only a provider that
    # declares it needs the diff inlined gets `prompt_inline`; everyone else
    # gets the smaller `prompt_pointer`. Keyed by provider name so
    # run_role_quorum's dict-shaped `prompt` support (both the
    # MIN_PROMPT_BYTES floor and the #319 blindness token-floor estimate)
    # measures each provider against ITS OWN body, not a mismatched one.
    provider_prompts: dict[str, str] = {
        p.name: prompt_inline if p.needs_inline_diff else prompt_pointer
        for p in plan.runnable
    }

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
    #
    # chief-wiggum#330 AC3: this wrapping lambda ALWAYS declares `attempt`/
    # `previous_failure_kind` (so providers._run_one_provider's retry-context
    # detection sees it and threads them in on every retry), but only
    # forwards them to the caller-supplied `execute` when THAT callable opts
    # in (providers.execute_accepts_retry_context) — every existing 3-arg
    # `execute(provider, prompt, timeout_override)` caller/test keeps
    # working completely unchanged; only one (scripts/run_review.py's real
    # execute) currently opts in, to reduce a required provider's retry
    # budget after a timeout rather than repeating its full first budget.
    execute_wants_retry_context = providers.execute_accepts_retry_context(execute)

    def _execute_for_quorum(p, attempt: int = 1, previous_failure_kind: str | None = None):
        # chief-wiggum#332: each provider's OWN body (inline or pointer),
        # falling back to the full inline prompt for a provider somehow
        # absent from plan.runnable (shouldn't happen — defensive only).
        shared_body = provider_prompts.get(p.name, prompt_inline)
        provider_prompt = providers.prompt_for_provider(plan.role, p.name, shared_body, lenses)
        timeout_override = providers.optional_provider_timeout(plan.role, p.name, optional_timeout_default)
        if execute_wants_retry_context:
            return execute(
                p, provider_prompt, timeout_override,
                attempt=attempt, previous_failure_kind=previous_failure_kind,
            )
        return execute(p, provider_prompt, timeout_override)

    quorum = providers.run_role_quorum(
        plan,
        _execute_for_quorum,
        out,
        # chief-wiggum#319/#332: the per-provider (pre-lens) prompt bodies +
        # lens map, so the quorum also runs the blindness check and surfaces
        # it in provider_manifest/review-manifest.json against the SAME body
        # each provider was actually rendered from via prompt_for_provider —
        # not a single shared value that would mismatch a pointer-prompt
        # provider against an inline-prompt one.
        prompt=provider_prompts,
        lenses=lenses,
    )
    response_paths = [r.path for r in quorum.results if r.path]

    # chief-wiggum#332: synthesis-prompt.md was dead weight — /implement Step 8
    # synthesizes reviews via scripts/synthesize_reviews.py over the
    # individual reviewer-<provider>.md files directly, never this artifact.

    manifest = ReviewManifest(
        ticket=ticket.number,
        base=base,
        role=role,
        diff_path=str(diff_path),
        prompt_path=str(prompt_path),
        provider_manifest=quorum.to_dict(),
        response_paths=response_paths,
        resolved_base_ref=resolved.ref,
        resolved_base_sha=resolved.sha,
        base_source=resolved.source,
        base_fallback_reason=resolved.fallback_reason,
        diff_stat=diff_stat,
    )
    (out / "review-manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2))
    return manifest
