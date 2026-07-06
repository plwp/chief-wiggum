#!/usr/bin/env python3
"""``cw`` — a thin facade over the Chief Wiggum Python helpers (P3-17).

Each subcommand simply dispatches to the matching helper's ``main(argv)``; the
business logic stays in (and is tested in) the underlying modules. The
standalone script entrypoints remain valid — this only adds discoverability.

    python3 scripts/cw.py                      # list helpers
    python3 scripts/cw.py context acme/app#42  # == workflow_context.py acme/app#42
    python3 scripts/cw.py plan-waves --edges '{"1": []}'
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# subcommand -> (module, one-line description). Every target module exposes a
# main(argv) entrypoint, so dispatch is a single uniform call.
SUBCOMMANDS: dict[str, tuple[str, str]] = {
    "context": ("workflow_context", "Resolve shared workflow context (home/tmp/repo/branch/epic)"),
    "epic-metadata": ("epic_metadata", "GitHub issue/milestone/dependency metadata"),
    "plan-waves": ("plan_waves", "Dependency-ordered wave planning + gating"),
    "epic-inventory": ("epic_inventory", "Discover epic/model/design artifacts + gates"),
    "formal-artifacts": ("generate_formal_test_artifacts", "Generate model-derived test artifacts"),
    "run-review": ("run_review", "Assemble + run the code-review quorum"),
    "git-safety": ("git_safety", "Worktree/branch safety checks"),
    "run-verification": ("run_verification", "Detect + run project verification"),
    "ux-gate": ("ux_gate", "UX / design-fidelity gate setup"),
    "draft-pr": ("draft_pr", "Draft a PR body from manifests"),
    "install-epic": ("install_epic_artifacts", "Install epic architecture artifacts"),
    "traceability": ("traceability", "Traceability matrix parse/update/audit"),
    "close-epic-audit": ("close_epic_audit", "Close-epic audit orchestrator"),
    "install-design": ("install_design_artifacts", "Install product design artifacts"),
    "tutorial-video": ("tutorial_video", "Produce a narrated click-through tutorial video"),
}


def _print_help() -> None:
    print("cw — Chief Wiggum helper facade\n")
    print("Usage: cw <command> [args...]\n")
    print("Commands:")
    width = max(len(name) for name in SUBCOMMANDS)
    for name in SUBCOMMANDS:
        _module, desc = SUBCOMMANDS[name]
        print(f"  {name.ljust(width)}  {desc}")
    print("\nEach command forwards its args to the matching helper. The standalone")
    print("scripts/<helper>.py entrypoints remain valid.")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_help()
        return 0

    command = argv[0]
    if command not in SUBCOMMANDS:
        print(f"Error: unknown command {command!r}. Run `cw --help`.", file=sys.stderr)
        return 2

    module_name, _desc = SUBCOMMANDS[command]
    module = importlib.import_module(module_name)
    return module.main(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
