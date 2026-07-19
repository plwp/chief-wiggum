#!/usr/bin/env python3
"""Render ``config/languages.json`` into ``docs/languages.md`` (#162).

The declared language support matrix is authored once, as data
(``config/languages.json``); this script is the ONLY thing that turns it into
prose, so the doc can never silently drift from the artifact both
``check_deps.py`` and ``scripts/emitters/`` consume — the same
"mechanical, not trusted" discipline as ``scripts/extract_design.py`` for
design tokens.

CLI:
    python3 scripts/render_languages_doc.py            # write docs/languages.md
    python3 scripts/render_languages_doc.py --check    # verify it's up to date (CI gate)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chief_wiggum import languages as cw_languages  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "languages.md"


def render(path: Path | str = cw_languages.DEFAULT_PATH) -> str:
    """Render the full markdown doc from the matrix at ``path``."""
    langs = cw_languages.languages(path)
    generic = sorted(cw_languages.generic_tier_extensions(path))
    unsupported = sorted(cw_languages.unsupported_extensions(path))

    lines = [
        "# Language Support Matrix",
        "",
        "Generated from `config/languages.json` by `scripts/render_languages_doc.py` "
        "(#162) — do not hand-edit this file; edit the config and re-run the script "
        "(wired into `/update`).",
        "",
        "Consumed by `check_deps.py` (`--list-languages`, the `language-tier-1` "
        "dependency profile) and by `scripts/emitters/` (the per-language emitter "
        "fallback chain: language-specific emitter -> generic regex tier -> "
        "skip-with-warning). See `docs/single-writer.md` / `docs/traceability.md` "
        "for what the emitters feed.",
        "",
        "## Languages",
        "",
        "| Language | Tier | Status | Extensions | LSP | Emitters | Test parser | "
        "Extractor | func_regex |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, lang in langs.items():
        exts = ", ".join(f"`{e}`" for e in lang.extensions)
        lines.append(
            f"| {name} | {lang.tier} | {lang.status} | {exts} | {lang.lsp or '—'} | "
            f"{', '.join(lang.emitters) or '—'} | {lang.test_parser or '—'} | "
            f"{lang.extractor or '—'} | {'yes' if lang.func_regex else 'no'} |"
        )

    lines += [
        "",
        "## Generic regex tier",
        "",
        "Extensions with no dedicated per-language emitter module, scanned by the "
        "generic (language-agnostic) regex tier (`scripts/emitters/generic.py`) — "
        "the pre-#162 behavior of `check_single_writer.py` / `check_traceability.py`:",
        "",
        ", ".join(f"`{e}`" for e in generic) or "(none)",
    ]

    lines += [
        "",
        "## Recognized-but-unsupported extensions",
        "",
        "Encountering one of these during a full-repo scan is NEVER a silent skip — "
        "`check_single_writer.py` / `check_traceability.py` surface an explicit "
        "coverage warning (`unsupported_extension_counts`) in both `--gate` and "
        "plain (query) output:",
        "",
        ", ".join(f"`{e}`" for e in unsupported) or "(none)",
    ]

    designed = [lang for lang in langs.values() if not lang.built and lang.requires]
    if designed:
        lines += ["", "## Designed, unbuilt slots", ""]
        for lang in designed:
            lines += [
                f"### {lang.name.capitalize()}",
                "",
                f"Trigger: {lang.trigger}",
                "",
                "Requires when triggered:",
                "",
            ]
            lines += [f"- {r}" for r in lang.requires]
            if lang.note:
                lines += ["", lang.note]
            lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the output file matches the freshly rendered doc; exit 1 if stale "
        "(no write). For a CI/pre-commit staleness gate.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"Output path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--config",
        default=str(cw_languages.DEFAULT_PATH),
        help="Path to languages.json (default: config/languages.json)",
    )
    args = parser.parse_args(argv)

    rendered = render(args.config)
    out_path = Path(args.output)

    if args.check:
        current = out_path.read_text() if out_path.exists() else None
        if current != rendered:
            print(
                f"Error: {out_path} is stale — run `python3 scripts/render_languages_doc.py` "
                "to refresh it",
                file=sys.stderr,
            )
            return 1
        print(f"{out_path} is up to date.")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
