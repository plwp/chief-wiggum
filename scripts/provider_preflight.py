#!/usr/bin/env python3
"""Check every configured provider at once, before a phase needs one.

Run this in /implement Step 1. A dead provider discovered mid-phase costs a
relaunch; discovered here it costs a few seconds and names its fallback.

Exit codes are distinct on purpose:
  0  every role can run
  1  a role is blocked by a down required provider
  2  a provider could not be verified at all (unknown, which is not ok)
  3  the config could not be read
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.preflight import (  # noqa: E402
    Health,
    Probes,
    RoleStatus,
    preflight,
    probe_command_alive,
)

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "providers.json"

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_UNVERIFIABLE = 2
EXIT_CONFIG = 3


def _render_human(result: dict) -> None:
    print("Providers")
    for name, report in result["providers"].items():
        mark = {"ok": "ok  ", "unavailable": "DOWN", "unknown": "????",
                "disabled": "off "}.get(report["health"], "????")
        suffix = f"  {report['detail']}" if report["detail"] else ""
        print(f"  [{mark}] {name}{suffix}")
    print("\nRoles")
    for name, report in result["roles"].items():
        mark = {"ok": "ok     ", "degraded": "degrade", "blocked": "BLOCKED"}.get(
            report["status"], "???"
        )
        suffix = f"  {report['detail']}" if report["detail"] else ""
        print(f"  [{mark}] {name}{suffix}")
    if result["unverifiable_providers"]:
        print(
            "\nCould not verify: "
            + ", ".join(result["unverifiable_providers"])
            + "\n  Unverified is not healthy. Fix the probe or treat these as down."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parallel provider health check")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--role", action="append", dest="roles",
                        help="Limit to these roles (repeatable)")
    parser.add_argument("--human", action="store_true", help="Human-readable output")
    parser.add_argument(
        "--deep", action="store_true",
        help="Run each CLI's --version instead of only checking PATH",
    )
    args = parser.parse_args(argv)

    try:
        config = json.loads(Path(args.config).read_text())
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"cannot read {args.config}: {exc}"}, indent=2))
        return EXIT_CONFIG

    probes = Probes(command=probe_command_alive) if args.deep else Probes()
    result = preflight(config, probes, roles=args.roles)

    if args.human:
        _render_human(result)
    else:
        print(json.dumps(result, indent=2))

    if any(report["status"] == RoleStatus.BLOCKED for report in result["roles"].values()):
        return EXIT_BLOCKED
    if any(report["health"] == Health.UNKNOWN for report in result["providers"].values()):
        return EXIT_UNVERIFIABLE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
