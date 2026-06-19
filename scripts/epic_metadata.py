#!/usr/bin/env python3
"""CLI for GitHub issue / milestone / dependency metadata.

Replaces ad-hoc ``gh`` calls and brittle dependency-block parsing in the
command prompts with one tested helper that emits normalized JSON.

Examples:
    # All open issues in a repo, normalized
    python3 scripts/epic_metadata.py issues acme/app

    # Parse the dependency graph from a milestone description
    python3 scripts/epic_metadata.py deps acme/app --milestone "Epic: Name"

    # Parse a dependency block from a file/stdin (offline, no gh)
    python3 scripts/epic_metadata.py parse-deps --file milestone-body.md
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import github  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GitHub epic metadata helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_issues = sub.add_parser("issues", help="List normalized issues for a repo")
    p_issues.add_argument("repo")
    p_issues.add_argument("--state", default="open")
    p_issues.add_argument("--limit", type=int, default=200)

    p_ms = sub.add_parser("milestones", help="List milestones for a repo")
    p_ms.add_argument("repo")

    p_deps = sub.add_parser("deps", help="Parse a milestone's dependency graph")
    p_deps.add_argument("repo")
    p_deps.add_argument("--milestone", required=True)

    p_parse = sub.add_parser("parse-deps", help="Parse a dependency block offline")
    src = p_parse.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="Read milestone description from this file")
    src.add_argument("--stdin", action="store_true", help="Read description from stdin")

    p_fmt = sub.add_parser(
        "format-deps", help="Render a dependency block from a JSON adjacency map"
    )
    p_fmt.add_argument(
        "edges",
        help='JSON object mapping issue number -> list of blockers, e.g. \'{"43": [42]}\'',
    )

    args = parser.parse_args(argv)

    try:
        if args.command == "issues":
            issues = github.list_issues(args.repo, state=args.state, limit=args.limit)
            print(json.dumps(github.issues_as_dicts(issues), indent=2))
        elif args.command == "milestones":
            milestones = github.list_milestones(args.repo)
            print(json.dumps([m.to_dict() for m in milestones], indent=2))
        elif args.command == "deps":
            meta = github.dependency_graph(args.repo, args.milestone)
            print(json.dumps(meta.to_dict(), indent=2))
        elif args.command == "parse-deps":
            text = sys.stdin.read() if args.stdin else Path(args.file).read_text()
            meta = github.parse_dependency_block(text)
            print(json.dumps(meta.to_dict(), indent=2))
        elif args.command == "format-deps":
            raw = json.loads(args.edges)
            if not isinstance(raw, dict):
                raise ValueError("format-deps expects a JSON object mapping number -> [deps]")
            edges: dict[int, list[int]] = {}
            for k, v in raw.items():
                if not isinstance(v, list):
                    raise ValueError(
                        f"dependencies for #{k} must be a JSON array of issue numbers, got {v!r}"
                    )
                edges[int(k)] = [int(d) for d in v]
            print(github.format_dependency_block(edges))
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or str(exc)
        print(f"Error: gh command failed: {detail}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - surface transport/parse errors cleanly
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
