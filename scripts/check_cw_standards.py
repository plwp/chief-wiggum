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
  5. **AI Act posture is revisited, not stale** — `docs/ai-act-posture.md` (CW's own
     Art. 2(12)/Art. 50 determination, chief-wiggum#317) exists and carries a
     `last_reviewed: YYYY-MM-DD` date within the last 12 months. This asserts the
     determination has been REVISITED, never that it is correct — that stays a human
     sign-off, marked `TBD:` in the doc itself.

Report-only by default (prints findings, exits 0). `--gate` makes it block (exit 1
on any error), the way every CW gate is meant to graduate (see docs/gate-rollout.md).
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRIPT_REF_RE = re.compile(r"scripts/([A-Za-z0-9_./-]+\.py)")
LAST_REVIEWED_RE = re.compile(r"`?last_reviewed:\s*(\d{4}-\d{2}-\d{2})`?")
POSTURE_DOC_MAX_AGE_DAYS = 366  # ~12 months, one day of slack for leap years

# A `scripts/<x>.py` reference prefixed with a TARGET-repo path variable points at the
# *target* repo's own script (e.g. `"$TARGET_REPO/scripts/maintain_tutorials.py"` — the
# repo's own tutorial maintainer), NOT a chief-wiggum script. Those must not be flagged
# as dangling chief-wiggum refs. Match the path-variable segment immediately before the
# `scripts/` token.
TARGET_REPO_PREFIX_RE = re.compile(
    r"(?:TARGET_REPO|TARGET_DIR|repo_dir|REPO_DIR|target_repo|target_dir)/$"
)


@dataclass
class Finding:
    rule: str
    message: str

    def __str__(self) -> str:
        return f"  [{self.rule}] {self.message}"


def _script_basenames(scripts_dir: Path) -> set[str]:
    """Every ``*.py`` basename under ``scripts_dir`` — computed ONCE (#326) and
    reused across every reference ``check()`` resolves, instead of
    ``_script_exists`` re-globbing ``scripts/`` per reference (O(refs × files)
    where O(files + refs) suffices)."""
    return {p.name for p in scripts_dir.rglob("*.py")}


def _script_exists(rel: str, scripts_dir: Path, basenames: set[str] | None = None) -> bool:
    """A referenced scripts/<rel> exists, by exact path or basename anywhere
    under scripts/. ``basenames`` (optional, #326): a precomputed
    ``_script_basenames(scripts_dir)`` set, so a caller resolving many
    references doesn't re-glob per call; omitted, this globs its own (the
    still-correct, standalone behavior any direct caller relies on)."""
    if (scripts_dir / rel).is_file():
        return True
    if basenames is None:
        basenames = _script_basenames(scripts_dir)
    return Path(rel).name in basenames


def check(root: Path = ROOT, *, today: datetime.date | None = None) -> list[Finding]:
    scripts = root / "scripts"
    commands = root / ".claude" / "commands"
    tests = root / "tests"
    findings: list[Finding] = []
    today = today or datetime.date.today()

    # 1. no bash scripts
    for sh in scripts.rglob("*.sh"):
        findings.append(Finding("no-bash-scripts",
            f"{sh.relative_to(root)} — scripts are Python (CLAUDE.md); port it"))

    # 2. no dangling skill -> script references
    if commands.is_dir():
        basenames = _script_basenames(scripts)  # once, not per reference (#326)
        for md in sorted(commands.glob("*.md")):
            text = md.read_text(errors="ignore")
            refs: set[str] = set()
            for m in SCRIPT_REF_RE.finditer(text):
                # Skip target-repo-scoped refs (`$TARGET_REPO/scripts/...`) — those are the
                # target repo's own scripts, not chief-wiggum's, so their absence here is
                # expected, not a dangling ref.
                if TARGET_REPO_PREFIX_RE.search(text[max(0, m.start() - 16):m.start()]):
                    continue
                refs.add(m.group(1))
            for ref in sorted(refs):
                if not _script_exists(ref, scripts, basenames):
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

    # 5. AI Act posture doc exists and has been revisited within 12 months (#317)
    posture = root / "docs" / "ai-act-posture.md"
    if not posture.is_file():
        findings.append(Finding("ai-act-posture-missing",
            "docs/ai-act-posture.md does not exist — CW's own Art. 2(12)/Art. 50 "
            "determination (chief-wiggum#317) is not recorded"))
    else:
        text = posture.read_text(errors="ignore")
        match = LAST_REVIEWED_RE.search(text)
        if not match:
            findings.append(Finding("ai-act-posture-no-review-date",
                "docs/ai-act-posture.md has no `last_reviewed: YYYY-MM-DD` marker"))
        else:
            try:
                reviewed = datetime.date.fromisoformat(match.group(1))
            except ValueError:
                findings.append(Finding("ai-act-posture-no-review-date",
                    f"docs/ai-act-posture.md's last_reviewed date is unparseable: {match.group(1)!r}"))
            else:
                age = (today - reviewed).days
                if age > POSTURE_DOC_MAX_AGE_DAYS:
                    findings.append(Finding("ai-act-posture-stale",
                        f"docs/ai-act-posture.md last_reviewed {reviewed.isoformat()} "
                        f"is {age} days old (>{POSTURE_DOC_MAX_AGE_DAYS}) — revisit the determination"))

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint the CW repo against its own standards.")
    parser.add_argument("--gate", action="store_true", help="Exit 1 on any finding (blocking mode)")
    args = parser.parse_args()

    findings = check()
    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        from factory_log import emit_gate
        emit_gate("check_cw_standards", "fail" if findings else "pass",
                  caught=len(findings), repo="chief-wiggum")
    except Exception:
        pass
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
