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
        try:
            raw = Path(args.changed_files).read_text()
        except OSError as exc:
            # #289: was an uncaught FileNotFoundError traceback — and under
            # /implement's `> manifest.md` redirect it left an EMPTY manifest
            # behind, which reads as "nothing to do".
            print(f"Error: --changed-files {args.changed_files}: {exc}", file=sys.stderr)
            return 2
        changed = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    else:
        changed = args.changed

    ui_spec = None
    ui_spec_missing = False
    if args.ui_spec:
        spec_path = Path(args.ui_spec)
        if not spec_path.exists():
            ui_spec_missing = True
        else:
            try:
                ui_spec = json.loads(spec_path.read_text())
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

    # #289 — a NAMED input that is not there is a broken instrument, not an
    # absent design contract. The only thing that can set `blocked` is the
    # ui-spec's design section, so a typo'd or wrong-root --ui-spec disarmed
    # the gate entirely: it passed unconditionally, exactly when its input had
    # gone missing.
    #
    # Scoped to FRONTEND tickets on purpose. /implement passes --ui-spec and
    # --design-dir unconditionally, and a backend-only epic legitimately has
    # neither; erroring there would be pure noise, and a noisy gate teaches the
    # operator to --force past every gate (docs/gate-rollout.md). The
    # disarming only matters when the gate would otherwise have run.
    errors: list[str] = []
    if manifest.should_run_gate:
        if ui_spec_missing:
            errors.append(
                f"--ui-spec {args.ui_spec}: not found on a FRONTEND ticket — the design "
                "contract could not be read, so this gate has nothing to hold the build "
                "against and cannot block"
            )
        # A missing design dir only breaks a measurement that was actually
        # expected: reference screenshots are the design contract's baseline,
        # so this is an error only when a design contract exists.
        if (args.design_dir and not Path(args.design_dir).is_dir()
                and manifest.design_binding.has_design_section):
            errors.append(
                f"--design-dir {args.design_dir}: not a directory while the ui-spec "
                "declares a design contract — the reference screenshots were looked for "
                "in a place that does not exist, so an empty list means nothing"
            )

    # The standard four-state outcome (#289). `inapplicable` is the honest
    # "this ticket does not touch the frontend"; `error` is "an input I was
    # pointed at is not there".
    if errors:
        applicability, outcome = "error", "error"
    elif not manifest.should_run_gate:
        applicability, outcome = "inapplicable", "inapplicable"
    else:
        applicability = "applicable"
        outcome = "findings" if manifest.blocked else "pass"

    payload = manifest.to_dict()
    payload["applicability"] = applicability
    payload["outcome"] = outcome
    payload["measured"] = {
        "changed_files": len(changed),
        "frontend_files": len(manifest.frontend.frontend_files),
        "reference_screenshots": len(manifest.reference_screenshots),
        "ui_spec_loaded": ui_spec is not None,
    }
    payload["errors"] = errors

    if args.markdown:
        print(manifest.render_markdown())
        print(f"- Measured: {len(changed)} changed file(s), "
              f"{len(manifest.frontend.frontend_files)} frontend, "
              f"{len(manifest.reference_screenshots)} reference screenshot(s), "
              f"ui-spec {'loaded' if ui_spec is not None else 'not loaded'}")
        print(f"- OUTCOME: {outcome.upper()}")
        for e in errors:
            print(f"  - ERROR: {e}")
    else:
        print(json.dumps(payload, indent=2))

    if errors:
        print(
            "ux_gate: ERROR — an input this gate was pointed at is missing; a green "
            "result here would be the absence of a measurement, not the absence of a "
            "problem (chief-wiggum#289):\n  " + "\n  ".join(errors),
            file=sys.stderr,
        )
        return 1

    # A frontend ticket with a design contract but no capture tooling is blocked.
    return 1 if manifest.blocked else 0


if __name__ == "__main__":
    sys.exit(main())
