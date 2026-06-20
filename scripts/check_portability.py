#!/usr/bin/env python3
"""Workflow portability conformance checker (#24).

Asserts that the Claude Code adapter prompts under ``.claude/commands/*.md`` keep
their worker launches harness-neutral. The invariant is deliberately stronger
than a keyword scan so it cannot be laundered by sprinkling an adapter tag:

1. **Markers only as bound adapter notes.** Any Claude-only execution/completion
   marker (``subagent_type``, generic ``sub-agent``, ``general-purpose``,
   ``model: opus|sonnet``, ``isolation: worktree``, ``run_in_background``,
   ``thoroughness``, and Claude completion language) may appear only on a line
   tagged ``Claude Code adapter`` that is **bound to a worker contract** — a
   ``docs/worker-contracts.md#<anchor>`` reference within the same/preceding
   lines.
2. **Referenced anchors exist.** Every ``worker-contracts.md#<anchor>`` named in
   a command resolves to a ``### <anchor>`` heading in the contract doc.
3. **Contracts are complete.** Every ``### *-worker`` section defines all six
   contract fields (role / inputs / output / write scope / isolation / stop).

This is the conformance gate for the Harness Generalization epic. Run:
``python3 scripts/check_portability.py`` (exit 1 on violations).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Claude-only markers (regex), each mapping to a portable concept inventoried in
# docs/harness-adapters.md. Keep in sync with that inventory.
MARKER_PATTERNS = (
    (re.compile(r"subagent_type"), "subagent_type"),
    (re.compile(r"\bsub-agent\b", re.IGNORECASE), "sub-agent"),
    (re.compile(r"general-purpose"), "general-purpose"),
    (re.compile(r"""model:\s*['"]?\s*(?:opus|sonnet)""", re.IGNORECASE), "model tier"),
    # Bare Claude model-tier names (capitalized proper nouns) in prose.
    (re.compile(r"\bOpus\b"), "Opus model tier"),
    (re.compile(r"\bSonnet\b"), "Sonnet model tier"),
    (re.compile(r"""isolation:\s*['"]?\s*worktree"""), 'isolation: "worktree"'),
    (re.compile(r"run_in_background"), "run_in_background"),
    (re.compile(r"\bthoroughness\b"), "thoroughness"),
    (re.compile(r"Agent tool"), "Agent tool"),
    (re.compile(r"\bwill notify\b", re.IGNORECASE), "completion notification"),
    (re.compile(r"Agent notification", re.IGNORECASE), "Agent notification"),
    (re.compile(r"Do not poll", re.IGNORECASE), "Do not poll"),
)

# A line that *launches* a worker (vs. merely describing one) must bind a contract.
LAUNCH_RE = re.compile(
    r"\b(?:launch(?:es|ing)?|send|spawn|run(?:s|ning)?\s+[a-z0-9 ,'`\"-]*?\b(?:in|inside))\b"
    r"[^.\n]*?\bworkers?\b",
    re.IGNORECASE,
)

ADAPTER_TAG = "Claude Code adapter"
CONTRACT_DOC_NAME = "worker-contracts.md"
ANCHOR_RE = re.compile(r"worker-contracts\.md#([a-z0-9-]+)")
ADAPTER_LOOKBACK = 3  # lines a contract anchor may precede the adapter note by

WORKER_LAUNCHING_COMMANDS = (
    "implement.md", "implement-wave.md", "architect.md",
    "design.md", "seed.md", "close-epic.md",
)
# Files that document the Claude-only surfaces, or are explicitly Claude-only.
EXEMPT_NAMES = {"harness-adapters.md", "worker-contracts.md", "keep-going.md"}

REQUIRED_CONTRACT_FIELDS = ("role", "inputs", "output", "write scope", "isolation", "stop")
_SECTION_RE = re.compile(r"^###\s+(?P<anchor>[a-z0-9-]+)\s*$", re.MULTILINE)


@dataclass
class Violation:
    file: str
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.detail}"


def _markers_on(line: str) -> list[str]:
    return [name for pat, name in MARKER_PATTERNS if pat.search(line)]


def check_command_markers(path: Path) -> tuple[list[Violation], set[str]]:
    """Layer 1: every marker line must be an adapter note bound to a contract.

    Returns (violations, anchors_referenced).
    """
    violations: list[Violation] = []
    anchors: set[str] = set()
    lines = path.read_text().splitlines()
    for a in (ANCHOR_RE.findall(line) for line in lines):
        anchors.update(a)
    for i, line in enumerate(lines):
        window = lines[max(0, i - ADAPTER_LOOKBACK): i + 1]
        anchored = any(ANCHOR_RE.search(w) for w in window)
        markers = _markers_on(line)
        if markers:
            if ADAPTER_TAG not in line:
                violations.append(
                    Violation(path.name, i + 1,
                              f"Claude-only marker(s) {markers} outside an adapter note")
                )
            elif not anchored:
                violations.append(
                    Violation(path.name, i + 1,
                              f"adapter note with marker(s) {markers} is not bound to a "
                              f"worker-contracts.md#<anchor> (launder guard)")
                )
        elif LAUNCH_RE.search(line) and not anchored:
            # A worker launch with no Claude marker still must bind a contract,
            # otherwise an unanchored "launch a worker" launders the gate.
            violations.append(
                Violation(path.name, i + 1,
                          "worker launch is not bound to a worker-contracts.md#<anchor>")
            )
    return violations, anchors


def check_contract_reference(path: Path) -> list[Violation]:
    """Layer 2: worker-launching commands must reference a contract anchor."""
    if path.name not in WORKER_LAUNCHING_COMMANDS:
        return []
    if ANCHOR_RE.search(path.read_text()):
        return []
    return [Violation(path.name, 0, f"does not reference a {CONTRACT_DOC_NAME}#<anchor>")]


def parse_contract_sections(doc_text: str) -> dict[str, str]:
    """Map each ``### <anchor>`` to its section body."""
    sections: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(doc_text))
    for idx, m in enumerate(matches):
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(doc_text)
        sections[m.group("anchor")] = doc_text[start:end]
    return sections


def check_contract_doc(doc: Path, referenced_anchors: set[str]) -> list[Violation]:
    """Validate the contract doc: anchors exist, worker sections are complete."""
    if not doc.is_file():
        return [Violation(doc.name, 0, "worker-contracts.md is missing")]
    sections = parse_contract_sections(doc.read_text())
    violations: list[Violation] = []
    # Every referenced anchor must exist.
    for anchor in sorted(referenced_anchors):
        if anchor not in sections:
            violations.append(Violation(doc.name, 0, f"referenced anchor #{anchor} not defined"))
    # Every *-worker section must define all required fields as labeled fields
    # (a bold label containing the field word, followed by ':'), not just mention
    # the words in prose.
    for anchor, body in sections.items():
        if not anchor.endswith("-worker"):
            continue
        missing = [
            f for f in REQUIRED_CONTRACT_FIELDS
            if not re.search(rf"\*\*[^*]*{f}[^*]*\*\*\s*:", body, re.IGNORECASE)
        ]
        if missing:
            violations.append(Violation(doc.name, 0, f"#{anchor} missing labeled fields: {missing}"))
    return violations


def check_repo(repo_root: str | Path) -> list[Violation]:
    root = Path(repo_root)
    commands_dir = root / ".claude" / "commands"
    violations: list[Violation] = []
    referenced_anchors: set[str] = set()
    for path in sorted(commands_dir.glob("*.md")):
        if path.name in EXEMPT_NAMES:
            continue
        v, anchors = check_command_markers(path)
        violations.extend(v)
        referenced_anchors.update(anchors)
        violations.extend(check_contract_reference(path))
    violations.extend(check_contract_doc(root / "docs" / "worker-contracts.md", referenced_anchors))
    return violations


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repo_root = argv[0] if argv else str(Path(__file__).resolve().parents[1])
    violations = check_repo(repo_root)
    if violations:
        print(f"Portability check FAILED ({len(violations)} violation(s)):", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print("Portability check passed: workflows are harness-neutral.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
