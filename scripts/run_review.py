#!/usr/bin/env python3
"""CLI for the review prompt assembly + review run (P1-7).

Captures the worktree diff, assembles a review prompt from the templates (plus
optional epic artifacts), runs the reviewer provider quorum, and writes the
synthesis inputs + a manifest. Replaces the bespoke Step 7 shell in /implement.

Example:
    python3 scripts/run_review.py \
      --ticket-context "$TICKET_TMP/ticket.json" \
      --worktree "$WORKTREE" --base "$DEFAULT_BRANCH" \
      --output-dir "$TICKET_TMP/reviews"
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import review  # noqa: E402
from consult_ai import consult_provider, reduced_retry_timeout  # noqa: E402

DEFAULT_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "review-prompt.md"
DEFAULT_CHECKLIST = Path(__file__).resolve().parents[1] / "templates" / "review-checklist.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble and run a code review")
    parser.add_argument("--ticket-context", required=True, help="JSON file with ticket title/body/AC")
    parser.add_argument("--worktree", required=True, help="Worktree to diff and review")
    parser.add_argument("--base", required=True, help="Base branch/ref for the diff")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--role", default="reviewer")
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    parser.add_argument("--checklist", default=str(DEFAULT_CHECKLIST))
    parser.add_argument(
        "--epic-artifact",
        action="append",
        default=[],
        metavar="TITLE=PATH",
        help="Optional epic artifact to include (e.g. Contracts=docs/epics/x/contracts.md)",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help=(
            "Force every provider to run fresh, ignoring any prior successful "
            "output whose prompt hash still matches (chief-wiggum#332). By "
            "default, re-running after a single provider's failure reuses "
            "every OTHER provider's unchanged output instead of re-paying it."
        ),
    )
    parser.add_argument(
        "--ticket",
        help=(
            "Issue number these review consults are for, so their spend "
            "attributes to the ticket (chief-wiggum#345). Defaults to the "
            "number in --ticket-context."
        ),
    )
    args = parser.parse_args(argv)

    # CTR-fh-002: a production ticket.json missing the `comments` key entirely
    # (as opposed to `"comments": []`) is the writer half of the #83 bug.
    # `from_dict` warns; make that warning visible on this CLI's stderr
    # regardless of the caller's warning filters — "explicit, never silent".
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", review.MissingCommentsWarning)
        ticket = review.TicketContext.from_dict(json.loads(Path(args.ticket_context).read_text()))
    for w in caught:
        if issubclass(w.category, review.MissingCommentsWarning):
            print(f"Warning: {w.message}", file=sys.stderr)

    # Every consult a workflow makes on behalf of ticket N must carry N. The
    # ticket.json already knows the number, so the flag is an override, not a
    # requirement — an un-passed flag must never silently drop the tag (#345).
    ticket_id = args.ticket or (str(ticket.number) if ticket.number is not None else None)

    template = Path(args.template).read_text()
    checklist = Path(args.checklist).read_text() if Path(args.checklist).exists() else None

    epic_sections: list[tuple[str, str]] = []
    epic_artifact_paths: list[str] = []
    for spec in args.epic_artifact:
        if "=" not in spec:
            continue
        title, path = spec.split("=", 1)
        p = Path(path)
        if p.exists():
            epic_sections.append((title, p.read_text()))
            epic_artifact_paths.append(path)

    # chief-wiggum#332 item 1: infer the epic slug from the docs/epics/<slug>/
    # convention --epic-artifact paths already follow, rather than adding a
    # new CLI flag that would just duplicate what's already encoded there.
    epic_slug = review._epic_slug_from_artifact_paths(epic_artifact_paths)

    def execute(provider, prompt, timeout_override=None, *, attempt=1, previous_failure_kind=None):
        # timeout_override caps an OPTIONAL claude-interactive delegate so it
        # fails fast instead of stalling the review quorum at 1800s (#188).
        #
        # chief-wiggum#330 AC3: declaring attempt/previous_failure_kind opts
        # this callable into providers.py's per-attempt retry context (see
        # providers.execute_accepts_retry_context, threaded here via
        # chief_wiggum.review.run_review's own opt-in wiring) — a retry that
        # follows a TIMEOUT-classified failure gets a reduced budget instead
        # of repeating the full one that just expired.
        if attempt > 1 and previous_failure_kind == "timeout":
            tool_name = provider.tool if provider.type == "tool" else "claude-interactive"
            timeout_override = reduced_retry_timeout(tool_name, timeout_override)
        return consult_provider(
            provider, prompt, None, args.worktree,
            ticket=ticket_id, timeout_override=timeout_override,
        )

    try:
        manifest = review.run_review(
            ticket,
            args.worktree,
            args.base,
            args.output_dir,
            template=template,
            checklist=checklist,
            epic_sections=epic_sections,
            role=args.role,
            execute=execute,
            force_fresh=args.fresh,
            epic_slug=epic_slug,
        )
    except review.ReviewError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # chief-wiggum#319: a reviewer quorum can report every required provider
    # "ok" while one of them never read anything beyond its own prompt —
    # surface that as loudly as any other quorum warning, independent of
    # whether the review itself passes.
    blindness = manifest.provider_manifest.get("blindness") or {}
    for finding in blindness.get("findings", []):
        print(f"Warning: {finding.get('message')}", file=sys.stderr)

    print(json.dumps(manifest.to_dict(), indent=2))
    return 0 if manifest.ok else 1


if __name__ == "__main__":
    sys.exit(main())
