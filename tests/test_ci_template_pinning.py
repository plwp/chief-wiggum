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


# --- parity with the gates CW's own loop applies (chief-wiggum#347) -----------

def _rendered(stacks):
    import sys
    sys.path.insert(0, str(REPO / "scripts"))
    from ci_scaffold import render_ci  # noqa: PLC0415
    return render_ci(stacks)


def test_every_rendered_workflow_is_valid_yaml():
    """A template that renders to broken YAML fails at the target's first push,
    after CW has told the operator CI is scaffolded."""
    yaml = pytest.importorskip("yaml")
    for stacks in (["go"], ["python"], ["node"], [], ["go", "python", "node"]):
        doc = yaml.safe_load(_rendered(stacks))
        assert doc.get("jobs"), f"render_ci({stacks}) produced no jobs"


def test_the_go_workflow_runs_the_linter_cw_requires():
    """/implement Step 7 and /implement-wave's integration check both require
    `golangci-lint run ./...`. A scaffolded CI that never runs it is out of
    parity with the gate the loop applies anyway — the operator then meets the
    findings at review time instead of at push time."""
    text = _rendered(["go"])
    assert "golangci-lint" in text, (
        "the scaffolded Go workflow does not lint, while CW's own loop requires "
        "golangci-lint (chief-wiggum#347)")


def test_the_scaffolded_linter_starts_report_only():
    """docs/gate-rollout.md applied to a scaffolded job: a NEW gate ships
    report-only. Scaffolding runs on repos that had no CI at all and may carry
    real lint debt; a hard-failing linter on day one blocks every PR and gets
    deleted rather than fixed."""
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_rendered(["go"]))
    steps = doc["jobs"]["go"]["steps"]
    lint = [s for s in steps if "golangci-lint-action" in str(s.get("uses", ""))]
    assert lint, "no golangci-lint step in the rendered Go workflow"
    # Parse the step rather than string-windowing around the first mention:
    # the first "golangci-lint" in the file is the COMMENT explaining why the
    # step exists, and a window before it proved nothing.
    assert lint[0].get("continue-on-error") is True, (
        "the scaffolded linter blocks from day one; it should ship report-only "
        "and be promoted once the repo is clean")


def test_dependency_caching_is_opt_in_not_defaulted():
    """`cache:` HARD-FAILS when the lockfile it hashes is absent, and these
    templates are stamped into repos whose lockfile situation is unknown. A
    default that breaks the workflow it scaffolds is worse than no cache."""
    for stacks in (["node"], ["python"]):
        text = _rendered(stacks)
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # the documented opt-in, which is the point
            assert not stripped.startswith("cache:"), (
                f"render_ci({stacks}) enables caching by default: {stripped}")


def test_the_opt_in_is_actually_documented():
    """Opt-in with no instructions is just a missing feature."""
    # Match the copy-pasteable directive, not the prose. `"cache:" in text`
    # is satisfied by the sentence explaining why caching is off, so deleting
    # the snippet passed it — caught by mutation testing.
    for stacks, directive in ((["node"], "#     cache: npm"),
                              (["python"], "#     cache: pip")):
        text = _rendered(stacks)
        assert directive in text, (
            f"render_ci({stacks}) neither enables caching nor shows how to: "
            f"expected the commented snippet {directive!r}")
