"""The skills' own stable-ID examples must be visible to the real scanner (#281).

An /architect run that follows the worked example verbatim must produce prose
the traceability scanner can parse. When the skill's examples and the ID
grammar drift apart, ``chief_wiggum.trace_ids.DEFINE_RE``/``ID_RE`` see zero
IDs and the gate "passes" vacuously — which is the bug this file exists to
prevent from returning.

Uses the REAL parsers (``hash_markdown_defs``, ``ID_RE``, ``MD_DEFINE_RE`` —
via ``trace_ids.ID_KINDS`` for the loose candidate finder below) so this test
can never pass by re-implementing the grammar it is checking against.
"""

from __future__ import annotations

import re
from pathlib import Path

from chief_wiggum import trace_ids
from chief_wiggum.hashing import hash_markdown_defs

REPO = Path(__file__).resolve().parents[1]
ARCHITECT_MD = REPO / ".claude" / "commands" / "architect.md"

# A LOOSE candidate finder: any token that *looks* like it starts a stable ID
# (a known KIND- prefix, at least one more id-char, ending in a digit, not
# glued to more id chars on either side). Deliberately looser than
# ``trace_ids.ID_RE`` — that is the whole point (a two-segment "INV-001" is
# invisible to ID_RE/DEFINE_RE *by construction*, so a check built only from
# those regexes could never find this bug). The KIND alternation itself is
# reused from ``trace_ids.ID_KINDS`` so this can never drift from the real
# kind list; only ``ID_RE.fullmatch`` decides pass/fail below.
CANDIDATE_ID_TOKEN = re.compile(
    rf"(?<![A-Za-z0-9-])(?:{'|'.join(trace_ids.ID_KINDS)})-[A-Za-z0-9][A-Za-z0-9-]*[0-9]"
    rf"(?![A-Za-z0-9])"
)

# Scope: the skill command bodies and the worked-example corpus /architect
# hands workers as "a concrete template to follow". Deliberately excludes the
# separate ASM ledger namespace (bets/, templates/assumptions-schema.json,
# scripts/assumption.py — chief-wiggum#281 codebase-context §2B) and anything
# under docs/formal-methods/examples/generated/ (machine-regenerated, not
# authored). ENT- prefixed ids (e.g. ENT-INV-001) are excluded structurally:
# the negative lookbehind refuses to match "INV-001" glued after "ENT-".
SCOPE = (
    ".claude/commands/*.md",
    "templates/formal-models/**/*.json",
    "docs/formal-methods/examples/*.json",
)


def _scope_files():
    for pattern in SCOPE:
        for path in sorted(REPO.glob(pattern)):
            if path.is_file():
                yield path


def _fenced_block_after(md_path: Path, heading: str, fence: str = "```markdown") -> str:
    """The first ``fence``-delimited block following ``heading`` in ``md_path``."""
    text = md_path.read_text()
    idx = text.index(heading)
    after = text[idx:]
    start = after.index(fence) + len(fence)
    start = after.index("\n", start) + 1
    end = after.index("```", start)
    return after[start:end]


def test_architect_invariants_example_declares_parseable_ids():
    """AC3: the skill's OWN documented invariants example must be non-empty
    and match the grammar via the REAL ``hash_markdown_defs`` parser — never a
    hand-rolled regex. Today the worked example under '#### 4e. Invariants'
    uses two-segment ids (INV-001, INV-002, INV-005, INV-007): DEFINE_RE
    cannot see them, so this fails until the example is fixed to the
    three-segment KIND-SLUG-NNN shape (chief-wiggum#281)."""
    block = _fenced_block_after(ARCHITECT_MD, "#### 4e. Invariants")
    defined = hash_markdown_defs(block)
    assert defined, (
        "the architect skill's own invariants worked example declares ZERO ids "
        "the traceability scanner can see — DEFINE_RE and the skill have "
        "drifted (chief-wiggum#281)"
    )
    assert set(defined) == {
        "INV-order-001",
        "INV-order-002",
        "INV-order-005",
        "INV-order-007",
    }


def test_no_stable_id_examples_drift_from_the_grammar():
    """AC1: every stable-ID-shaped example token across the skill command
    bodies and the worked-example corpus must fullmatch the REAL ID_RE — not
    a re-implementation of it. Collects every violation across the whole
    scope so the failure message names every offender at once (never
    ``assert`` inside the loop)."""
    violations = []
    for path in _scope_files():
        text = path.read_text()
        rel = path.relative_to(REPO)
        for m in CANDIDATE_ID_TOKEN.finditer(text):
            token = m.group(0)
            if not trace_ids.ID_RE.fullmatch(token):
                violations.append(f"{rel}: {token}")
    assert not violations, (
        "stable-ID examples that the real scanner cannot see (two-segment or "
        f"otherwise malformed) — expected KIND-SLUG-NNN everywhere: {violations}"
    )


def test_worked_example_corpus_ids_are_parseable():
    """The order-lifecycle JSON corpus /architect hands workers as a template
    must itself declare parseable three-segment ids — regression lock once
    #281's fix regenerates it (chief-wiggum#281)."""
    import check_traceability as ct

    corpus = REPO / "docs" / "formal-methods" / "examples"
    tmp_epic_files = {
        "order-lifecycle.state-machine.json": (corpus / "order-lifecycle.state-machine.json").read_text(),
        "order-lifecycle.contracts.json": (corpus / "order-lifecycle.contracts.json").read_text(),
    }
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        epic = Path(td) / "epic"
        epic.mkdir()
        for name, text in tmp_epic_files.items():
            (epic / name).write_text(text)
        defined = ct.extract_defined_ids(epic)
        expected = {f"INV-order-{n:03d}" for n in range(1, 8)}
        assert expected <= set(defined), (
            f"expected {sorted(expected)} to be parseable from the worked-example "
            f"corpus, got {sorted(defined)} (chief-wiggum#281)"
        )
