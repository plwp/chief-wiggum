#!/usr/bin/env python3
"""Extract a visual design contract from an approved HTML mockup.

Used by /design Step 5. Mockups produced by /design declare every design
token as a CSS custom property on ``:root`` inside a <style> block, using
these prefixes:

    --color-<name>    -> design.tokens.colors        (e.g. --color-primary)
    --font-<role>     -> design.tokens.typography.fonts   (heading/body/mono)
    --text-<name>     -> design.tokens.typography.scale   (xs/sm/base/lg/...)
    --space-<name>    -> design.tokens.spacing
    --radius-<name>   -> design.tokens.radii
    --shadow-<name>   -> design.tokens.shadows

Extraction is mechanical so the committed design.json cannot drift from the
mock the human approved. ``var(--x)`` references are resolved before mapping.

Usage:
    python3 extract_design.py extract mock.html --source-kind net-new \
        [--reference path ...] [--notes "..."] [--out design.json]
    python3 extract_design.py validate design.json
    python3 extract_design.py styleguide design.json [--out styleguide.html]

``extract`` prints (or writes) a JSON object matching the ui-spec schema's
``design`` definition and exits 1 if the result fails validation (e.g. the
mock never declared --color-primary). ``validate`` checks a hand-edited
design.json — run it after adding component_library / assets / voice.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

import jsonschema

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_SPEC_SCHEMA = REPO_ROOT / "templates" / "formal-models" / "ui-spec-schema.json"

STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.DOTALL | re.IGNORECASE)
ROOT_RE = re.compile(r":root\s*\{([^}]*)\}", re.DOTALL)
DECL_RE = re.compile(r"--([a-zA-Z0-9][a-zA-Z0-9-]*)\s*:\s*([^;}]+)")
VAR_RE = re.compile(r"var\(\s*--([a-zA-Z0-9][a-zA-Z0-9-]*)\s*(?:,\s*([^)]+))?\)")

# prefix -> path into the design.tokens object
PREFIX_MAP = {
    "color": ("colors",),
    "font": ("typography", "fonts"),
    "text": ("typography", "scale"),
    "space": ("spacing",),
    "radius": ("radii",),
    "shadow": ("shadows",),
}


def parse_custom_properties(html_text: str) -> dict[str, str]:
    """Collect --custom-property declarations from :root blocks in <style> tags."""
    props: dict[str, str] = {}
    for style in STYLE_RE.findall(html_text):
        for body in ROOT_RE.findall(style):
            for name, value in DECL_RE.findall(body):
                props[name] = value.strip()
    return props


def resolve_vars(props: dict[str, str]) -> dict[str, str]:
    """Resolve var(--x) references against the collected properties (two passes)."""

    def substitute(value: str) -> str:
        def repl(m: re.Match) -> str:
            ref, fallback = m.group(1), m.group(2)
            if ref in props:
                return props[ref]
            return fallback.strip() if fallback else m.group(0)

        return VAR_RE.sub(repl, value)

    for _ in range(2):
        props = {name: substitute(value) for name, value in props.items()}
    return props


def tokens_from_properties(props: dict[str, str]) -> tuple[dict, list[str]]:
    """Map prefixed custom properties into the design.tokens shape.

    Returns (tokens, skipped) where skipped lists properties with no
    recognised prefix — surfaced so a typo like --colour-primary is loud.
    """
    tokens: dict = {}
    skipped: list[str] = []
    for name, value in props.items():
        prefix, _, rest = name.partition("-")
        path = PREFIX_MAP.get(prefix)
        if path is None or not rest:
            skipped.append(f"--{name}")
            continue
        bucket = tokens
        for key in path:
            bucket = bucket.setdefault(key, {})
        bucket[rest] = value
    return tokens, skipped


def design_subschema() -> dict:
    """The ui-spec schema's design definition, usable as a standalone schema."""
    full = json.loads(UI_SPEC_SCHEMA.read_text())
    schema = dict(full["$defs"]["design"])
    schema["$defs"] = full["$defs"]
    return schema


def validate_design(design: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(design_subschema())
    return [e.message for e in validator.iter_errors(design)]


def extract(
    mock_path: Path,
    source_kind: str,
    references: list[str],
    notes: str | None,
) -> tuple[dict, list[str]]:
    props = resolve_vars(parse_custom_properties(mock_path.read_text()))
    tokens, skipped = tokens_from_properties(props)
    source: dict = {"kind": source_kind}
    if references:
        source["references"] = references
    if notes:
        source["notes"] = notes
    return {"source": source, "tokens": tokens}, skipped


# ---------------------------------------------------------------------------
# Styleguide rendering
# ---------------------------------------------------------------------------


def _swatch_rows(colors: dict[str, str]) -> str:
    rows = []
    for name, value in colors.items():
        rows.append(
            f'<div class="swatch"><div class="chip" style="background:{html.escape(value, quote=True)}"></div>'
            f"<code>{html.escape(name)}</code><span>{html.escape(value)}</span></div>"
        )
    return "\n".join(rows)


def render_styleguide(design: dict) -> str:
    """Self-contained styleguide.html: token sheet rendered with the tokens themselves."""
    tokens = design.get("tokens", {})
    colors = tokens.get("colors", {})
    typography = tokens.get("typography", {})
    fonts = typography.get("fonts", {})
    scale = typography.get("scale", {})
    spacing = tokens.get("spacing", {})
    radii = tokens.get("radii", {})
    shadows = tokens.get("shadows", {})
    voice = design.get("voice", {})
    library = design.get("component_library", {})

    primary = colors.get("primary", "#333")
    body_font = fonts.get("body", "system-ui, sans-serif")
    heading_font = fonts.get("heading", body_font)

    sections = [f"<section><h2>Colors</h2>{_swatch_rows(colors)}</section>"]

    if fonts or scale:
        font_rows = "".join(
            f'<p style="font-family:{html.escape(stack, quote=True)}"><code>{html.escape(role)}</code> '
            f"— The quick brown fox jumps over the lazy dog <em>({html.escape(stack)})</em></p>"
            for role, stack in fonts.items()
        )
        scale_rows = "".join(
            f'<p style="font-size:{html.escape(size, quote=True)}"><code>{html.escape(name)}</code> {html.escape(size)}</p>'
            for name, size in scale.items()
        )
        sections.append(f"<section><h2>Typography</h2>{font_rows}{scale_rows}</section>")

    if spacing:
        rows = "".join(
            f'<div class="row"><code>{html.escape(name)}</code>'
            f'<div class="bar" style="width:{html.escape(value, quote=True)}"></div><span>{html.escape(value)}</span></div>'
            for name, value in spacing.items()
        )
        sections.append(f"<section><h2>Spacing</h2>{rows}</section>")

    if radii:
        boxes = "".join(
            f'<div class="box" style="border-radius:{html.escape(value, quote=True)}"><code>{html.escape(name)}</code><span>{html.escape(value)}</span></div>'
            for name, value in radii.items()
        )
        sections.append(f'<section><h2>Radii</h2><div class="boxes">{boxes}</div></section>')

    if shadows:
        boxes = "".join(
            f'<div class="box" style="box-shadow:{html.escape(value, quote=True)}"><code>{html.escape(name)}</code></div>'
            for name, value in shadows.items()
        )
        sections.append(f'<section><h2>Shadows</h2><div class="boxes">{boxes}</div></section>')

    if library:
        sections.append(
            f"<section><h2>Component library</h2><p><strong>{html.escape(library.get('name', ''))}</strong> "
            f"({html.escape(library.get('usage', ''))}) {html.escape(library.get('notes', ''))}</p></section>"
        )

    if voice:
        items = "".join(
            f"<li><strong>{html.escape(k)}</strong>: {html.escape(str(v))}</li>"
            for k, v in voice.items()
        )
        sections.append(f"<section><h2>Voice</h2><ul>{items}</ul></section>")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Style Guide</title>
<style>
  body {{ font-family: {body_font}; margin: 0; padding: 2rem 3rem; color: #1a1a1a; }}
  h1 {{ font-family: {heading_font}; border-bottom: 4px solid {primary}; padding-bottom: .5rem; }}
  h2 {{ font-family: {heading_font}; color: {primary}; margin-top: 2.5rem; }}
  code {{ background: #f4f4f4; padding: .1rem .35rem; border-radius: 4px; }}
  .swatch, .row {{ display: flex; align-items: center; gap: 1rem; margin: .4rem 0; }}
  .chip {{ width: 3.5rem; height: 2rem; border-radius: 6px; border: 1px solid #ddd; }}
  .bar {{ height: .75rem; background: {primary}; border-radius: 3px; }}
  .boxes {{ display: flex; gap: 1.5rem; flex-wrap: wrap; }}
  .box {{ width: 8rem; height: 5rem; border: 1px solid #ddd; background: #fff; display: flex;
          flex-direction: column; align-items: center; justify-content: center; gap: .3rem; }}
  span, em {{ color: #666; font-size: .85rem; }}
</style>
</head>
<body>
<h1>Style Guide</h1>
{"".join(sections)}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_extract(args: argparse.Namespace) -> int:
    mock = Path(args.mock)
    if not mock.is_file():
        print(f"error: mock not found: {mock}", file=sys.stderr)
        return 2
    design, skipped = extract(mock, args.source_kind, args.reference or [], args.notes)
    for prop in skipped:
        print(f"warning: skipped unrecognised custom property {prop}", file=sys.stderr)
    errors = validate_design(design)
    output = json.dumps(design, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(output)
    else:
        sys.stdout.write(output)
    if errors:
        for e in errors:
            print(f"invalid: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.design)
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2
    errors = validate_design(json.loads(path.read_text()))
    if errors:
        for e in errors:
            print(f"invalid: {e}", file=sys.stderr)
        return 1
    print("valid")
    return 0


def cmd_styleguide(args: argparse.Namespace) -> int:
    path = Path(args.design)
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2
    page = render_styleguide(json.loads(path.read_text()))
    if args.out:
        Path(args.out).write_text(page)
    else:
        sys.stdout.write(page)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_extract = sub.add_parser("extract", help="extract design.json from an approved HTML mock")
    p_extract.add_argument("mock", help="path to the approved mockup HTML")
    p_extract.add_argument(
        "--source-kind",
        required=True,
        choices=["existing-design-system", "reference-product", "brand-kit", "net-new"],
    )
    p_extract.add_argument(
        "--reference", action="append", help="design source reference (repeatable)"
    )
    p_extract.add_argument("--notes", help="source notes")
    p_extract.add_argument("--out", help="write design.json here instead of stdout")
    p_extract.set_defaults(func=cmd_extract)

    p_validate = sub.add_parser(
        "validate", help="validate a design.json against the ui-spec design schema"
    )
    p_validate.add_argument("design")
    p_validate.set_defaults(func=cmd_validate)

    p_style = sub.add_parser("styleguide", help="render styleguide.html from a design.json")
    p_style.add_argument("design")
    p_style.add_argument("--out", help="write styleguide.html here instead of stdout")
    p_style.set_defaults(func=cmd_styleguide)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
