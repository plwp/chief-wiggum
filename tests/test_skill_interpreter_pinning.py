"""Every python invocation in a skill runs under the resolved interpreter.

chief-wiggum#374: the skills hardcoded `python3`, so CW's runtime interpreter
was whatever the shell resolved -- an unpinned, per-machine accident. Homebrew
bumping `python3` from 3.11 to 3.13 stranded keyring / jsonschema /
google-genai, and CW found out as a `ModuleNotFoundError` inside a backgrounded
consult, where the exit is instant and the output file never appears.

`env.py python` resolves a VALIDATED interpreter and nothing called it. This
test is what keeps it called: a new `python3 "$CW_HOME/scripts/..."` added to
any workflow file fails here rather than a year later on somebody's machine.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMMANDS = REPO / ".claude/commands"

BOOTSTRAP_HOME = 'CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)'
BOOTSTRAP_PY = 'CW_PY=$(python3 "$CW_HOME/scripts/env.py" python) || CW_PY=python3'

# The only bare `python3` invocations a skill may contain.
#
#   1-2. the bootstrap pair -- chicken-and-egg. `env.py` imports nothing
#        outside the stdlib, so it runs anywhere a python3 exists at all.
#   3.   the TARGET repo's own tooling, which belongs to the target's
#        interpreter and its dependencies, not CW's.
ALLOWED = (
    re.compile(re.escape(BOOTSTRAP_HOME)),
    re.compile(re.escape(BOOTSTRAP_PY)),
    re.compile(r'TARGET_REPO"?\s*&&\s*python3\b'),
    re.compile(r'python3 "\$TUT_STATUS_SCRIPT"'),
)

BARE_PYTHON3 = re.compile(r'(?<!\{)(?<!-)\bpython3\b')


def workflow_files() -> list[Path]:
    return sorted(COMMANDS.glob("*.md"))


def test_there_are_workflow_files_to_check():
    """A denominator, so an empty glob can never read as a clean pass."""
    assert len(workflow_files()) >= 20


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_every_python_invocation_is_pinned(path: Path):
    offenders = []
    for lineno, line in enumerate(path.read_text().split("\n"), 1):
        if "python3" not in line:
            continue
        if line.lstrip().startswith("#"):  # commentary about the rule itself
            continue
        stripped = line
        for allowed in ALLOWED:
            stripped = allowed.sub("", stripped)
        # `"${CW_PY:-python3}"` is the pinned form; its own literal fallback
        # must not read as an offender.
        stripped = stripped.replace('"${CW_PY:-python3}"', "")
        if BARE_PYTHON3.search(stripped):
            offenders.append(f"{path.name}:{lineno}: {line.strip()[:120]}")
    assert offenders == [], (
        "unpinned `python3` in a workflow file (chief-wiggum#374) — use "
        '`"${CW_PY:-python3}"`:\n' + "\n".join(offenders)
    )


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_a_file_that_calls_cw_scripts_resolves_the_interpreter(path: Path):
    text = path.read_text()
    if "$CW_HOME/scripts/" not in text:
        pytest.skip("no CW script calls")
    assert BOOTSTRAP_HOME in text, f"{path.name} calls CW scripts without resolving CW_HOME"
    assert BOOTSTRAP_PY in text, (
        f"{path.name} calls CW scripts without resolving CW_PY — its scripts would "
        f"run under whatever `python3` the shell happens to resolve (chief-wiggum#374)"
    )


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_the_interpreter_is_resolved_after_the_home_it_depends_on(path: Path):
    text = path.read_text()
    if BOOTSTRAP_PY not in text:
        pytest.skip("no CW_PY bootstrap")
    # CW_PY's own resolution reads $CW_HOME, so it cannot come first.
    assert text.index(BOOTSTRAP_HOME) < text.index(BOOTSTRAP_PY), (
        f"{path.name} resolves CW_PY before CW_HOME, so it would probe an empty path"
    )


def test_the_resolution_falls_back_rather_than_leaving_it_empty():
    """`|| CW_PY=python3` matters: without it a resolver failure leaves CW_PY
    unset and every later call becomes `"" script.py`, which is a worse failure
    than the unpinned interpreter this ticket set out to fix."""
    for path in workflow_files():
        text = path.read_text()
        if "CW_PY=$(" not in text:
            continue
        assert BOOTSTRAP_PY in text, f"{path.name} resolves CW_PY without a fallback"


def test_call_sites_use_the_defaulting_form():
    """Call sites use `${CW_PY:-python3}`, not a bare `$CW_PY`, so a bash block
    run in a fresh shell that never saw the bootstrap degrades to exactly
    today's behaviour instead of failing on an empty command."""
    offenders = []
    for path in workflow_files():
        for lineno, line in enumerate(path.read_text().split("\n"), 1):
            if re.search(r'"\$CW_PY"|\$CW_PY(?![:_}])', line) and "CW_PY=$(" not in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()[:120]}")
    assert offenders == [], (
        'use `"${CW_PY:-python3}"` at call sites, not a bare `$CW_PY`:\n'
        + "\n".join(offenders)
    )


def test_the_migration_actually_happened():
    """Guards against the whole rule passing vacuously because someone deleted
    the call sites rather than pinning them."""
    pinned = sum(
        path.read_text().count('"${CW_PY:-python3}"') for path in workflow_files()
    )
    assert pinned > 250, f"only {pinned} pinned invocations; expected the full migration"
