#!/usr/bin/env python3
"""
Stitch-audit extraction orchestrator.

Discovers files in a target repo, delegates extraction to pluggable
extractor modules, and outputs standardized Schema/Field JSON.

The orchestrator never mentions Go or TypeScript directly — it calls
get_extractors(repo), gets back whichever extractors match, and runs
them all. Adding a new stack = add a new file to extractors/, done.

Usage:
    python3 stitch_extract.py <repo_path> --trace <keyword> [-o output.json]
    python3 stitch_extract.py <repo_path> --patterns [path] [-o output.json]

Modes:
    --trace <keyword>   Trace a feature's data flow across all layers
    --patterns [path]   Scan for convention inconsistencies
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow importing extractors from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from extractors import get_extractors
from extractors.base import Schema, schemas_to_json


def trace(repo_path: Path, keyword: str) -> list[Schema]:
    """Trace a keyword across all layers using all matching extractors."""
    extractors = get_extractors(repo_path)
    if not extractors:
        print(
            f"Warning: no extractors matched repo at {repo_path}",
            file=sys.stderr,
        )
        return []

    print(
        f"Extractors active: {', '.join(e.name() for e in extractors)}",
        file=sys.stderr,
    )

    all_schemas: list[Schema] = []

    for extractor in extractors:
        # Discover files by layer
        discovered = extractor.discover(repo_path, keyword)
        file_count = sum(len(v) for v in discovered.values())
        print(
            f"  [{extractor.name()}] discovered {file_count} files "
            f"across {len(discovered)} layers",
            file=sys.stderr,
        )

        for layer, files in discovered.items():
            for file_path in files:
                schemas = extractor.extract(file_path, keyword=keyword)
                # Ensure relative paths in output
                for s in schemas:
                    try:
                        s.file = str(Path(s.file).relative_to(repo_path))
                    except ValueError:
                        pass  # Already relative
                    # Override layer from discover if extractor set it generically
                    if s.layer != layer and layer in (
                        "frontend_forms",
                        "api_handlers",
                        "database_ops",
                        "admin_views",
                    ):
                        s.layer = layer
                all_schemas.extend(schemas)

    return all_schemas


def scan_patterns(repo_path: Path, scan_path: str | None = None) -> dict:
    """Scan for convention inconsistencies using all matching extractors."""
    extractors = get_extractors(repo_path)
    if not extractors:
        print(
            f"Warning: no extractors matched repo at {repo_path}",
            file=sys.stderr,
        )
        return {"extractors": [], "patterns": []}

    results = []
    for extractor in extractors:
        result = extractor.scan_patterns(repo_path, scan_path)
        results.append(result)
        print(
            f"  [{extractor.name()}] scanned {result.get('total_fields', 0)} fields",
            file=sys.stderr,
        )

    return {
        "extractors": [e.name() for e in extractors],
        "patterns": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stitch-audit extraction orchestrator"
    )
    parser.add_argument("repo_path", help="Path to target repository")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--trace",
        metavar="KEYWORD",
        help="Trace a feature keyword across all layers",
    )
    mode.add_argument(
        "--patterns",
        nargs="?",
        const=None,
        default=False,
        metavar="PATH",
        help="Scan for convention inconsistencies (optional: sub-path)",
    )

    parser.add_argument("-o", "--output", help="Write output to file instead of stdout")
    args = parser.parse_args()

    repo = Path(args.repo_path).resolve()
    if not repo.is_dir():
        print(f"Error: {repo} is not a directory", file=sys.stderr)
        sys.exit(1)

    if args.trace:
        schemas = trace(repo, args.trace)
        output = schemas_to_json(schemas)
        print(f"Extracted {len(schemas)} schemas", file=sys.stderr)
    elif args.patterns is not False:
        result = scan_patterns(repo, args.patterns)
        output = json.dumps(result, indent=2)
    else:
        parser.print_help()
        sys.exit(1)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output)
        print(f"OK: output written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
