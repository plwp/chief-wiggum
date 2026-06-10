"""Tests for the ui-spec design contract: schema validation and human rendering."""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import formal_models as fm  # noqa: E402
import render_models as rm  # noqa: E402

EXAMPLE = REPO_ROOT / "docs" / "formal-methods" / "examples" / "kanban-app-ui-spec.json"


def _minimal_spec(**design) -> dict:
    spec = {
        "pages": {"home": {"route": "/", "layout": "centered"}},
        "navigation": {"initial": "home", "states": {"home": {"route": "/"}}},
    }
    if design:
        spec["design"] = design["design"]
    return spec


def test_example_with_design_section_validates():
    data = json.loads(EXAMPLE.read_text())
    assert "design" in data, "example should demonstrate the design contract"
    assert fm.validate(data, "ui-spec") == []


def test_design_requires_source_and_tokens():
    spec = _minimal_spec(design={"theme": {"modes": ["light"]}})
    errors = fm.validate(spec, "ui-spec")
    assert any("source" in e for e in errors)
    assert any("tokens" in e for e in errors)


def test_design_colors_require_primary():
    spec = _minimal_spec(design={
        "source": {"kind": "net-new"},
        "tokens": {"colors": {"accent": "#ff7c43"}},
    })
    errors = fm.validate(spec, "ui-spec")
    assert any("primary" in e for e in errors)


def test_valid_design_section_passes():
    spec = _minimal_spec(design={
        "source": {"kind": "brand-kit", "references": ["docs/brand.md"]},
        "tokens": {"colors": {"primary": "#0079bf"}},
        "component_library": {"name": "shadcn/ui", "usage": "adopt"},
    })
    assert fm.validate(spec, "ui-spec") == []


def test_invalid_source_kind_rejected():
    spec = _minimal_spec(design={
        "source": {"kind": "vibes"},
        "tokens": {"colors": {"primary": "#000"}},
    })
    assert fm.validate(spec, "ui-spec") != []


def test_human_render_includes_design_contract():
    data = json.loads(EXAMPLE.read_text())
    md = rm.render_ui_spec_human(data)
    assert "## Visual Design Contract" in md
    assert "`primary` | `#0079bf`" in md
    assert "reference-screenshot" in md
    assert "## Pages" in md
    assert "stateDiagram-v2" in md  # navigation graph


def test_human_render_without_design_section_still_works():
    spec = _minimal_spec()
    md = rm.render_ui_spec_human(spec)
    assert "Visual Design Contract" not in md
    assert "## Pages" in md
