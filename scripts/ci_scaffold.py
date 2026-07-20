#!/usr/bin/env python3
"""Detect and scaffold a minimal CI workflow in a target repo (#111).

The portfolio audit found target repos shipping with **no CI** — so red tests,
lint errors, and uninstallable deps sat unnoticed on `main`. This script is the
enforcement-layer guard: it detects whether a repo has any GitHub Actions
workflow, detects the language stack(s), and — on request — scaffolds a minimal,
real CI workflow (checkout -> setup toolchain -> install deps -> build -> test ->
lint) tailored to each detected stack.

Per `docs/gate-rollout.md`, this ships **report-only by default**:

    --report   (default) print CI presence + detected stack(s); exit 0 always.
    --scaffold write .github/workflows/ci.yml (idempotent; needs --force to
               overwrite an existing workflow).
    --gate     exit non-zero if CI is missing (the future blocking mode, off by
               default). A gate is only wired into a workflow with --gate after
               it has been validated report-only on real repos.

Target resolution mirrors the other scripts: `owner/repo` (via repo.py),
`--repo PATH`, or the current directory.

CLI:
    python3 scripts/ci_scaffold.py --repo . --report
    python3 scripts/ci_scaffold.py acme/app --scaffold
    python3 scripts/ci_scaffold.py --repo . --gate

Exit codes: 0 = ok (or report-only), 1 = CI missing under --gate, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.hashing import scanner_version  # noqa: E402

# Templates live under templates/ci/ next to this script's repo root.
CW_HOME = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = CW_HOME / "templates" / "ci"

WORKFLOW_DIR = ".github/workflows"
DEFAULT_WORKFLOW = "ci.yml"

# Detected stack -> template fragment (a `jobs:` entry, no header).
STACK_TEMPLATES = {
    "go": "ci-go.yml",
    "python": "ci-python.yml",
    "node": "ci-node.yml",
}


def detect_ci(repo: str | Path) -> tuple[bool, list[str]]:
    """Return (present, workflows) for GitHub Actions workflows in a repo.

    A workflow is any `.github/workflows/*.yml` or `*.yaml` file. Paths are
    returned repo-relative and sorted for stable output.
    """
    root = Path(repo)
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return False, []
    workflows = sorted(
        str(p.relative_to(root))
        for p in wf_dir.iterdir()
        if p.is_file() and p.suffix in (".yml", ".yaml")
    )
    return bool(workflows), workflows


def detect_stack(repo: str | Path) -> list[str]:
    """Detect language stack(s) from marker files. May return multiple."""
    root = Path(repo)
    stack: list[str] = []
    if (root / "go.mod").is_file():
        stack.append("go")
    if (root / "package.json").is_file():
        stack.append("node")
    if (
        (root / "pyproject.toml").is_file()
        or (root / "setup.py").is_file()
        or (root / "requirements.txt").is_file()
    ):
        stack.append("python")
    return stack


def _read_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text()


def render_ci(stacks: list[str]) -> str:
    """Render a full CI workflow YAML for the given detected stacks.

    Composes the shared header (name/on/jobs) with one job fragment per stack.
    With no recognized stack, emits a valid generic skeleton so the workflow
    still parses and prompts the author for real steps.
    """
    header = _read_template("ci-header.yml")
    job_templates = [STACK_TEMPLATES[s] for s in stacks if s in STACK_TEMPLATES]
    if not job_templates:
        job_templates = ["ci-generic.yml"]
    jobs = "\n".join(_read_template(t).rstrip("\n") for t in job_templates)
    return header.rstrip("\n") + "\n" + jobs + "\n"


def scaffold_ci(
    repo: str | Path, stacks: list[str], *, force: bool = False
) -> list[Path]:
    """Write .github/workflows/ci.yml for the detected stacks.

    Idempotent: if the workflow already exists and `force` is False, writes
    nothing and returns []. Returns the list of paths written.
    """
    root = Path(repo)
    wf_dir = root / ".github" / "workflows"
    target = wf_dir / DEFAULT_WORKFLOW
    if target.exists() and not force:
        return []
    wf_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(render_ci(stacks))
    return [target]


def resolve_target(args: argparse.Namespace) -> Path:
    """Resolve the target repo from --repo, an owner/repo positional, or cwd."""
    if args.owner_repo:
        # Import lazily so unit tests that only touch detect/scaffold don't need gh.
        sys.path.insert(0, str(CW_HOME / "scripts"))
        from repo import resolve_repo

        return resolve_repo(args.owner_repo)
    return Path(args.repo)


def build_report(repo: Path) -> dict:
    present, workflows = detect_ci(repo)
    stack = detect_stack(repo)
    return {
        "repo": str(repo),
        "ci_present": present,
        "workflows": workflows,
        "stack": stack,
    }


def render_text(report: dict) -> str:
    lines: list[str] = []
    stack = ", ".join(report["stack"]) or "unknown"
    if report["ci_present"]:
        lines.append(f"OK: CI present ({len(report['workflows'])} workflow(s))")
        for wf in report["workflows"]:
            lines.append(f"  {wf}")
        lines.append(f"Detected stack: {stack}")
    else:
        lines.append("MISSING: no CI workflow found (.github/workflows/*.yml)")
        lines.append(f"Detected stack: {stack}")
        lines.append(
            "  A minimal CI workflow would be scaffolded with --scaffold "
            "(checkout -> install deps -> build -> test -> lint)."
        )
    return "\n".join(lines)


def _scanner_version() -> str:
    """Hash-derived ``--scanner-version``: the source of this module plus its
    ``chief_wiggum`` dependencies. No hand-bumped constant to forget
    (INV-fh-005).
    @cw-trace guards CTR-fh-040 CTR-fh-041 CTR-fh-042 INV-fh-005"""
    here = Path(__file__).resolve()
    cw_dir = here.parent / "chief_wiggum"
    return scanner_version(here, cw_dir / "hashing.py")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect and scaffold a minimal CI workflow in a target repo"
    )
    parser.add_argument(
        "owner_repo", nargs="?", help="owner/repo to resolve via gh (optional)"
    )
    parser.add_argument("--repo", default=".", help="Path to target repo (default: cwd)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--report", action="store_true", help="Report CI presence + stack (default)"
    )
    mode.add_argument(
        "--scaffold", action="store_true", help="Write a minimal CI workflow"
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Exit non-zero if CI is missing (blocking mode; off by default)",
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing workflow on scaffold"
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument(
        "--scanner-version",
        action="store_true",
        help="Print the hash-derived scanner version (source hash of this module + its "
        "chief_wiggum deps) and exit",
    )
    args = parser.parse_args(argv)

    if args.scanner_version:
        print(_scanner_version())
        return 0

    try:
        repo = resolve_target(args)
    except SystemExit:
        raise
    if not repo.exists():
        print(f"ERROR: repo path does not exist: {repo}", file=sys.stderr)
        return 2

    report = build_report(repo)

    if args.scaffold:
        written = scaffold_ci(repo, report["stack"], force=args.force)
        report["scaffolded"] = [str(p) for p in written]
        if args.json:
            print(json.dumps(report, indent=2))
        elif written:
            print(f"Scaffolded {written[0].relative_to(repo)} for stack: "
                  f"{', '.join(report['stack']) or 'unknown'}")
        elif report["ci_present"]:
            print("CI already present; nothing written (use --force to overwrite).")
        else:
            print("Nothing written.")
        return 0

    # Report mode (default). --gate can turn a missing-CI finding into a failure.
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))

    if args.gate and not report["ci_present"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
