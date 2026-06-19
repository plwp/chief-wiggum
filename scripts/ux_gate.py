#!/usr/bin/env python3
"""CLI for UX and design-fidelity mechanics (P1-10).

Does the cheap, mechanical setup for /implement Step 9 — frontend-impact
detection, ui-spec design-binding check, reference-screenshot discovery, and
screenshot-capture planning — and emits a UX manifest the agent consumes before
the judgment-heavy review.

Example:
    python3 scripts/ux_gate.py \
      --changed-files "$TICKET_TMP/changed.txt" \
      --label frontend --ui-spec "$MODELS_DIR/ui-spec.json" \
      --design-dir "$TARGET_REPO/docs/design" \
      --have-playwright --screenshot-dir "$TICKET_TMP/ux-screenshots" --markdown
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import ux  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UX / design-fidelity gate setup")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--changed-files", help="File with one changed path per line")
    src.add_argument("--changed", action="append", default=[], help="A changed path (repeatable)")
    parser.add_argument("--label", action="append", default=[], help="Issue label (repeatable)")
    parser.add_argument("--ui-spec", help="Path to ui-spec.json")
    parser.add_argument("--design-dir", help="Path to docs/design/")
    parser.add_argument("--have-browser-use", action="store_true")
    parser.add_argument("--have-playwright", action="store_true")
    parser.add_argument("--screenshot-dir")
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true")
    out.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)

    if args.changed_files:
        changed = [ln.strip() for ln in Path(args.changed_files).read_text().splitlines() if ln.strip()]
    else:
        changed = args.changed

    ui_spec = None
    if args.ui_spec and Path(args.ui_spec).exists():
        try:
            ui_spec = json.loads(Path(args.ui_spec).read_text())
        except json.JSONDecodeError as exc:
            print(f"Error: malformed ui-spec: {exc}", file=sys.stderr)
            return 1

    manifest = ux.build_ux_manifest(
        changed,
        labels=args.label,
        ui_spec=ui_spec,
        design_dir=args.design_dir,
        browser_use_available=args.have_browser_use,
        playwright_available=args.have_playwright,
        screenshot_dir=args.screenshot_dir,
    )

    if args.markdown:
        print(manifest.render_markdown())
    else:
        print(json.dumps(manifest.to_dict(), indent=2))

    # A frontend ticket with a design contract but no capture tooling is blocked.
    return 1 if manifest.blocked else 0


if __name__ == "__main__":
    sys.exit(main())
