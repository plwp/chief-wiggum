"""CI templates pin actions by SHA, not by mutable tag (chief-wiggum#347).

`actions/checkout@v4` resolves to whatever that tag points at TODAY. A tag can
be force-moved, and a compromised one then runs in every repo CW stamps a
workflow into. A commit SHA cannot move.

The trailing `# v4` comment is required too: without it the pin is unreadable
and Dependabot cannot propose a bump, which is how pinned repos end up frozen
on an action with a known CVE.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CI_TEMPLATES = REPO / "templates" / "ci"

USES_RE = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
SHA_PIN_RE = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def template_files() -> list[Path]:
    return sorted(CI_TEMPLATES.glob("*.yml"))


def uses_lines() -> list[tuple[Path, int, str, str]]:
    out = []
    for path in template_files():
        for lineno, line in enumerate(path.read_text().split("\n"), 1):
            m = USES_RE.match(line)
            if m:
                out.append((path, lineno, m.group("ref"), m.group("rest")))
    return out


def test_there_are_templates_and_action_references_to_check():
    """A denominator. An empty glob, or templates that stopped using actions,
    must not let this file pass by checking nothing."""
    assert len(template_files()) >= 4
    assert len(uses_lines()) >= 4


@pytest.mark.parametrize("path", template_files(), ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_sha(path: Path):
    offenders = []
    for lineno, line in enumerate(path.read_text().split("\n"), 1):
        m = USES_RE.match(line)
        if not m:
            continue
        ref = m.group("ref")
        if not SHA_PIN_RE.match(ref):
            offenders.append(f"{path.name}:{lineno}: {ref}")
    assert offenders == [], (
        "CI template actions must be pinned to a 40-char commit SHA, not a "
        "mutable tag (chief-wiggum#347):\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("path", template_files(), ids=lambda p: p.name)
def test_every_pin_carries_its_version_comment(path: Path):
    """`@<sha> # v4` — the SHA is what runs, the comment is what a human reads
    and what Dependabot bumps."""
    offenders = []
    for lineno, line in enumerate(path.read_text().split("\n"), 1):
        m = USES_RE.match(line)
        if not m or not SHA_PIN_RE.match(m.group("ref")):
            continue
        if not re.match(r"\s*#\s*v?\d", m.group("rest")):
            offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "a SHA pin needs a trailing `# <version>` comment so it stays readable "
        "and Dependabot-updatable:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("stacks", [
    ["go"], ["python"], ["node"], [], ["go", "python", "node"],
])
def test_the_scaffolded_workflow_carries_the_pins(stacks):
    """The pin has to survive whatever ci_scaffold does to the template. A
    template pinned on disk and a workflow emitted with mutable tags would be a
    rule that only holds where nobody runs it."""
    import sys
    sys.path.insert(0, str(REPO / "scripts"))
    from ci_scaffold import render_ci  # noqa: E402

    text = render_ci(stacks)
    seen = 0
    for line in text.split("\n"):
        m = USES_RE.match(line)
        if not m:
            continue
        seen += 1
        assert SHA_PIN_RE.match(m.group("ref")), (
            f"scaffolded workflow emits an unpinned action: {line.strip()}"
        )
    # Every stack (including the empty/generic one) checks out the repo, so a
    # render with zero `uses:` lines means the assertion above proved nothing.
    assert seen > 0, f"render_ci({stacks}) emitted no action references to check"
