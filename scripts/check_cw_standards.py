#!/usr/bin/env python3
"""Hold the factory to its own standards — a self-linter for the CW repo.

CW imposes discipline on the products it builds; it should meet its own bar. This
checks the CW-repo conventions that are cheaply and mechanically verifiable:

  1. **Scripts are Python** — no `.sh` scripts under `scripts/` (CLAUDE.md principle).
  2. **No dangling skill → script references** — every `scripts/<x>.py` a command
     adapter (`.claude/commands/*.md`) tells the operator to run must exist. A
     renamed/deleted helper leaving a command pointing at a ghost is a broken skill.
  3. **Gates are tested** — every gate (`scripts/check_*.py`) has a
     `tests/test_<name>.py`. Gates are load-bearing; an untested gate is a gate you
     can't trust.
  4. **Command adapters have a title** — every `.claude/commands/*.md` starts with
     an H1.

Report-only by default (prints findings, exits 0). `--gate` makes it block (exit 1
on any error), the way every CW gate is meant to graduate (see docs/gate-rollout.md).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRIPT_REF_RE = re.compile(r"scripts/([A-Za-z0-9_./-]+\.py)")


@dataclass
class Finding:
    rule: str
    message: str

    def __str__(self) -> str:
        return f"  [{self.rule}] {self.message}"


def _script_exists(rel: str, scripts_dir: Path) -> bool:
    """A referenced scripts/<rel> exists, by exact path or basename anywhere under scripts/."""
    if (scripts_dir / rel).is_file():
        return True
    base = Path(rel).name
    return any(p.name == base for p in scripts_dir.rglob("*.py"))


def check(root: Path = ROOT) -> list[Finding]:
    scripts = root / "scripts"
    commands = root / ".claude" / "commands"
    tests = root / "tests"
    findings: list[Finding] = []

    # 1. no bash scripts
    for sh in scripts.rglob("*.sh"):
        findings.append(Finding("no-bash-scripts",
            f"{sh.relative_to(root)} — scripts are Python (CLAUDE.md); port it"))

    # 2. no dangling skill -> script references
    if commands.is_dir():
        for md in sorted(commands.glob("*.md")):
            refs = set(SCRIPT_REF_RE.findall(md.read_text(errors="ignore")))
            for ref in sorted(refs):
                if not _script_exists(ref, scripts):
                    findings.append(Finding("dangling-script-ref",
                        f"{md.name} references scripts/{ref} which does not exist"))

    # 3. gates are tested
    if scripts.is_dir():
        for gate in sorted(scripts.glob("check_*.py")):
            expected = tests / f"test_{gate.stem}.py"
            if not expected.is_file():
                findings.append(Finding("gate-untested",
                    f"gate {gate.name} has no {expected.relative_to(root)}"))

    # 4. command adapters have a title
    if commands.is_dir():
        for md in sorted(commands.glob("*.md")):
            first = next((ln for ln in md.read_text(errors="ignore").splitlines() if ln.strip()), "")
            if not first.startswith("# "):
                findings.append(Finding("command-no-title",
                    f"{md.name} does not start with an H1 title"))

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint the CW repo against its own standards.")
    parser.add_argument("--gate", action="store_true", help="Exit 1 on any finding (blocking mode)")
    args = parser.parse_args()

    findings = check()
    if not findings:
        print("check_cw_standards: CW meets its own standards.")
        return 0
    print(f"check_cw_standards: {len(findings)} finding(s)"
          f"{' (report-only; pass --gate to block)' if not args.gate else ''}")
    for f in findings:
        print(f)
    return 1 if args.gate else 0


if __name__ == "__main__":
    sys.exit(main())
