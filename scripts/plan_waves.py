#!/usr/bin/env python3
"""CLI for dependency-ordered wave planning (P0-3).

Turns a dependency graph + ticket state into a wave plan. Designed to consume
the JSON emitted by ``epic_metadata.py deps`` plus the epic's issue/closed/gated
state, so ``/implement-wave`` Step 2 becomes one tested call.

Exit codes:
    0  plan produced
    1  bad input
    2  dependency cycle (no valid ordering)

Examples:
    python3 scripts/plan_waves.py \
      --issues 42,43,44 \
      --edges '{"42": [], "43": [42], "44": [43]}' \
      --closed 42 --gated 44 --markdown
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import planning  # noqa: E402


def _parse_int_list(value: str | None) -> list[int]:
    if not value:
        return []
    return [int(x) for x in value.replace(",", " ").split()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan implementation waves")
    parser.add_argument("--issues", help="Comma/space-separated epic issue numbers")
    parser.add_argument(
        "--edges",
        help='JSON adjacency map {"n": [deps]}; or use --deps-json for full deps output',
    )
    parser.add_argument(
        "--deps-json",
        help="Path to `epic_metadata.py deps` JSON (reads .edges); '-' for stdin",
    )
    parser.add_argument("--closed", help="Comma/space-separated closed issue numbers")
    parser.add_argument("--gated", help="Comma/space-separated gated issue numbers")
    parser.add_argument("--markdown", action="store_true", help="Emit markdown report")
    args = parser.parse_args(argv)

    if not args.deps_json and not args.edges:
        print("Error: one of --edges or --deps-json is required", file=sys.stderr)
        return 1

    deps_warnings: list[str] = []
    try:
        if args.deps_json:
            raw = sys.stdin.read() if args.deps_json == "-" else Path(args.deps_json).read_text()
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("--deps-json must be a JSON object with an 'edges' map")
            deps_warnings = list(parsed.get("warnings", []))
            edges = {int(k): v for k, v in parsed.get("edges", {}).items()}
        else:
            parsed = json.loads(args.edges)
            if not isinstance(parsed, dict):
                raise ValueError("--edges must be a JSON object mapping number -> [deps]")
            edges = {int(k): v for k, v in parsed.items()}

        # A malformed dependency line is dropped upstream by epic_metadata.py,
        # which would leave the affected ticket looking dependency-free. Refuse
        # to plan on a graph we know is corrupt rather than risk scheduling a
        # ticket before its real dependency lands.
        malformed = [w for w in deps_warnings if "malformed dependency line" in w]
        if malformed:
            print(
                "Error: refusing to plan on a corrupt dependency graph:\n  "
                + "\n  ".join(malformed),
                file=sys.stderr,
            )
            return 1

        issues = _parse_int_list(args.issues) or list(edges)
        plan = planning.plan_waves(
            issues,
            edges,
            closed=_parse_int_list(args.closed),
            gated=_parse_int_list(args.gated),
        )
        # Surface remaining (non-fatal) deps metadata warnings in the plan.
        plan.warnings = deps_warnings + plan.warnings
    except planning.DependencyCycleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.markdown:
        print(planning.render_markdown(plan))
    else:
        print(json.dumps(plan.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
