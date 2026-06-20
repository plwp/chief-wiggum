#!/usr/bin/env python3
"""Workflow portability conformance checker (#24).

Asserts that the Claude Code adapter prompts under ``.claude/commands/*.md`` keep
their worker launches harness-neutral: a Claude-only execution mechanism may
only appear as an explicit *adapter note*, and every worker-launching command
points at the harness-neutral worker-contract reference.

This is the conformance gate for the Harness Generalization epic — it fails if a
portable workflow reference starts *requiring* a Claude-only param again.

Run: ``python3 scripts/check_portability.py`` (exit 1 on violations).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# Claude-only execution markers. Each maps to a portable concept inventoried in
# docs/harness-adapters.md — keep this list in sync with that inventory.
CLAUDE_EXECUTION_MARKERS = (
    "subagent_type",          # -> worker
    "general-purpose",        # -> worker (Claude generic sub-agent)
    "Explore sub-agent",      # -> read-only explorer worker
    "Opus sub-agent",         # -> worker, model tier
    "Sonnet sub-agent",       # -> worker, model tier
    'model: "opus"',          # -> provider role / model tier
    'model: "sonnet"',        # -> provider role / model tier
    'isolation: "worktree"',  # -> required isolation behavior
    "run_in_background",      # -> async completion via status file
)

# Marker lines must carry this tag to be a legitimate adapter note.
ADAPTER_TAG = "Claude Code adapter"

# Worker-launching commands must point at the portable contract reference.
WORKER_CONTRACT_REF = "worker-contracts.md"
WORKER_LAUNCHING_COMMANDS = (
    "implement.md",
    "implement-wave.md",
    "architect.md",
    "design.md",
    "seed.md",
    "close-epic.md",
)

# Docs that legitimately name the Claude-only surfaces (they document the mapping).
EXEMPT_NAMES = {"harness-adapters.md", "worker-contracts.md"}

# Required contract fields each worker section in worker-contracts.md must define.
REQUIRED_CONTRACT_FIELDS = ("role", "inputs", "output", "write scope", "isolation", "stop")


@dataclass
class Violation:
    file: str
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.detail}"


def _markers_on(line: str) -> list[str]:
    return [m for m in CLAUDE_EXECUTION_MARKERS if m in line]


def check_command_file(path: Path) -> list[Violation]:
    """Layer 1: every marker line must be tagged as an adapter note."""
    violations: list[Violation] = []
    text = path.read_text()
    for i, line in enumerate(text.splitlines(), start=1):
        markers = _markers_on(line)
        if markers and ADAPTER_TAG not in line:
            violations.append(
                Violation(
                    path.name, i,
                    f"Claude-only marker(s) {markers} outside an adapter note; "
                    f"name a worker contract first and put params under '{ADAPTER_TAG}:'",
                )
            )
    return violations


def check_contract_reference(path: Path) -> list[Violation]:
    """Layer 2: worker-launching commands must reference the contract doc."""
    if path.name not in WORKER_LAUNCHING_COMMANDS:
        return []
    if WORKER_CONTRACT_REF in path.read_text():
        return []
    return [Violation(path.name, 0, f"does not reference {WORKER_CONTRACT_REF}")]


def check_worker_contracts_doc(doc: Path) -> list[Violation]:
    """Structural: the contract doc must define the required fields."""
    if not doc.is_file():
        return [Violation(doc.name, 0, "worker-contracts.md is missing")]
    body = doc.read_text().lower()
    missing = [f for f in REQUIRED_CONTRACT_FIELDS if f not in body]
    if missing:
        return [Violation(doc.name, 0, f"missing required contract field labels: {missing}")]
    return []


def check_repo(repo_root: str | Path) -> list[Violation]:
    root = Path(repo_root)
    commands_dir = root / ".claude" / "commands"
    violations: list[Violation] = []
    for path in sorted(commands_dir.glob("*.md")):
        if path.name in EXEMPT_NAMES:
            continue
        violations.extend(check_command_file(path))
        violations.extend(check_contract_reference(path))
    violations.extend(check_worker_contracts_doc(root / "docs" / "worker-contracts.md"))
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
