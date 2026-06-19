"""PR body and Mermaid diagram scaffolding (P1-11).

`/ship` and `/implement` both require PR bodies with diagrams, verification
evidence, contract/model conformance, and UX results — and they duplicate the
Mermaid colour palette and section requirements in prose. This assembles the PR
body from the upstream manifests (review, verification, UX, model conformance,
issue context), enforces required sections, and provides reusable Mermaid theme
helpers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# The shared Mermaid palette (kept in one place instead of copied into prose).
PALETTE = {
    "theme": "base",
    "themeVariables": {
        "primaryColor": "#003f5c",
        "primaryTextColor": "#fff",
        "primaryBorderColor": "#2f4b7c",
        "secondaryColor": "#665191",
        "tertiaryColor": "#a05195",
        "lineColor": "#2f4b7c",
        "textColor": "#333",
    },
}
_SEQUENCE_EXTRA = {
    "actorTextColor": "#fff",
    "actorBkg": "#003f5c",
    "actorBorder": "#2f4b7c",
    "activationBorderColor": "#d45087",
    "activationBkgColor": "#f95d6a",
    "signalColor": "#2f4b7c",
}

REQUIRED_SECTIONS = ("Summary", "Changes", "Test Evidence")


def mermaid_theme_directive(*, sequence: bool = False) -> str:
    """Return the ``%%{init: ...}%%`` theme line for a diagram."""
    palette = {"theme": PALETTE["theme"], "themeVariables": dict(PALETTE["themeVariables"])}
    if sequence:
        palette["themeVariables"].update(_SEQUENCE_EXTRA)
    # Mermaid expects single-quoted JSON-ish. Escape any pre-existing single
    # quotes in values before swapping double->single so a value containing an
    # apostrophe can't break out of the init object.
    body = json.dumps(palette).replace("'", "\\'").replace('"', "'")
    return f"%%{{init: {body}}}%%"


def mermaid_block(diagram: str, *, sequence: bool = False) -> str:
    """Wrap a diagram body in a themed ```mermaid fenced block."""
    directive = mermaid_theme_directive(sequence=sequence)
    return f"```mermaid\n{directive}\n{diagram.strip()}\n```"


def _verification_evidence(verification: dict | None) -> str:
    if not verification:
        return "```\n<!-- paste test output -->\n```"
    lines = []
    for step in verification.get("steps", []):
        cmd = " ".join(step.get("command", []))
        if step.get("planned_only"):
            lines.append(f"- [plan] `{cmd}`")
        else:
            mark = "✓" if step.get("ok") else "✗"
            lines.append(f"- {mark} `{cmd}` — exit {step.get('exit_code')}")
    status = "all green" if verification.get("ok") else "FAILURES"
    return f"**Verification: {status}**\n" + "\n".join(lines)


def _review_summary(review: dict | None) -> str | None:
    if not review:
        return None
    pm = review.get("provider_manifest", review)
    providers = ", ".join(
        f"{r['name']} ({r['status']})" for r in pm.get("results", [])
    )
    state = "passed" if pm.get("ok") else "had failures"
    return f"Multi-AI review {state}: {providers}" if providers else f"Multi-AI review {state}."


@dataclass
class PRDraft:
    title: str
    body: str

    def to_dict(self) -> dict:
        return {"title": self.title, "body": self.body}


def build_pr_body(
    *,
    issue: int | None = None,
    summary: str = "",
    changes: list[str] | None = None,
    mermaid: str | None = None,
    mermaid_sequence: bool = False,
    verification: dict | None = None,
    review: dict | None = None,
    ux: dict | None = None,
    model_conformance: str | None = None,
) -> str:
    """Assemble a PR body from the upstream manifests + issue context."""
    parts: list[str] = ["## Summary", "", summary or "<!-- what was implemented and why -->"]
    if issue is not None:
        parts += ["", f"Closes #{issue}"]

    if mermaid:
        parts += ["", "## Architecture", "", mermaid_block(mermaid, sequence=mermaid_sequence)]

    parts += ["", "## Changes", ""]
    parts += [f"- {c}" for c in (changes or ["<!-- change -->"])]

    parts += ["", "## Test Evidence", "", _verification_evidence(verification)]

    if model_conformance:
        parts += ["", "## Model Conformance", "", model_conformance.strip()]

    if ux:
        parts += ["", "## UX / Design Fidelity", ""]
        status = ux.get("status") or ("ok" if ux.get("ok") else "see notes")
        parts.append(f"Design-fidelity gate: {status}")
        for shot in ux.get("screenshots", []):
            parts.append(f"- `{shot}`")

    review_line = _review_summary(review)
    if review_line:
        parts += ["", "## Review", "", review_line]

    parts += [
        "", "## Review Checklist", "",
        "- [ ] Tests pass locally",
        "- [ ] No new warnings or lint errors",
        "- [ ] Multi-AI review feedback addressed",
        "- [ ] No secrets or credentials committed",
    ]
    return "\n".join(parts) + "\n"


def validate_sections(body: str, required: tuple[str, ...] = REQUIRED_SECTIONS) -> list[str]:
    """Return the names of any required ``## `` sections missing from ``body``.

    Matches exact ``## <Name>`` heading lines (not substrings), so ``### Summary``
    or ``## Summary Details`` don't satisfy a required ``Summary`` section.
    """
    missing = []
    for name in required:
        pattern = re.compile(rf"^##\s+{re.escape(name)}\s*$", re.MULTILINE)
        if not pattern.search(body):
            missing.append(name)
    return missing


def suggest_title(issue_title: str | None, *, issue: int | None = None, prefix: str = "feat") -> str:
    base = (issue_title or "update").strip()
    if issue is not None and f"#{issue}" not in base:
        return f"{prefix}: {base} (#{issue})"
    return f"{prefix}: {base}"


def gh_pr_create_command(title: str, body_file: str | Path, *, base: str | None = None, draft: bool = False) -> list[str]:
    cmd = ["gh", "pr", "create", "--title", title, "--body-file", str(body_file)]
    if base:
        cmd += ["--base", base]
    if draft:
        cmd.append("--draft")
    return cmd
