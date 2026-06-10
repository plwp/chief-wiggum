#!/usr/bin/env python3
"""
Resolve chief-wiggum paths and common shell values for command prompts.

The Claude command files are markdown, not shell scripts, so snippets cannot
rely on "$0" or Python's __file__ from the prompt context. This helper gives
those prompts a tested way to find the installed checkout and derive shared
values such as temp directories and epic slugs.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path


def _is_chief_wiggum_home(path: Path) -> bool:
    return (
        (path / "scripts" / "repo.py").is_file()
        and (path / ".claude" / "commands").is_dir()
    )


def find_home() -> Path:
    """Find the chief-wiggum checkout without relying on command prompt state."""
    env_home = os.environ.get("CHIEF_WIGGUM_HOME") or os.environ.get("CW_HOME")
    if env_home:
        candidate = Path(env_home).expanduser().resolve()
        if _is_chief_wiggum_home(candidate):
            return candidate

    search_roots = [
        Path.cwd(),
        *Path.cwd().parents,
        Path.home() / "repos" / "chief-wiggum",
        Path.home() / ".chief-wiggum" / "chief-wiggum",
    ]

    for root in search_roots:
        candidate = root.expanduser().resolve()
        if _is_chief_wiggum_home(candidate):
            return candidate

    raise RuntimeError(
        "Could not find chief-wiggum checkout. Set CHIEF_WIGGUM_HOME to its path."
    )


def create_tmp() -> Path:
    tmp = Path.home() / ".chief-wiggum" / "tmp" / str(uuid.uuid4())
    tmp.mkdir(parents=True, exist_ok=False)
    return tmp


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "epic"


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def main() -> int:
    parser = argparse.ArgumentParser(description="chief-wiggum environment helper")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("home", help="Print chief-wiggum checkout path")
    sub.add_parser("tmp", help="Create and print a session temp directory")

    slug = sub.add_parser("slug", help="Slugify text for docs/epics paths")
    slug.add_argument("value")

    exports = sub.add_parser("exports", help="Print shell exports for prompts")
    exports.add_argument("--epic", help="Optional epic/milestone name")

    args = parser.parse_args()

    try:
        if args.command == "home":
            print(find_home())
        elif args.command == "tmp":
            print(create_tmp())
        elif args.command == "slug":
            print(slugify(args.value))
        elif args.command == "exports":
            home = find_home()
            tmp = create_tmp()
            print(f"export CW_HOME={shell_quote(str(home))}")
            print(f"export CW_TMP={shell_quote(str(tmp))}")
            if args.epic:
                epic_slug = slugify(args.epic)
                print(f"export EPIC_SLUG={shell_quote(epic_slug)}")
                print(f'export EPIC_DIR="$TARGET_REPO/docs/epics/{epic_slug}"')
        else:
            parser.print_help()
            return 1
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except FileExistsError:
        print(
            f"Error: temp directory collision under {tempfile.gettempdir()}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
