"""Tests for UX and design-fidelity mechanics (P1-10)."""

from __future__ import annotations

import json

import ux_gate
from chief_wiggum import ux

# --- frontend impact detection ----------------------------------------------


def test_frontend_detected_by_extension():
    impact = ux.detect_frontend_impact(["src/components/Button.tsx", "README.md"])
    assert impact.is_frontend is True
    assert impact.frontend_files == ["src/components/Button.tsx"]


def test_frontend_detected_by_dir_hint():
    impact = ux.detect_frontend_impact(["app/pages/home.py"])
    assert impact.is_frontend is True


def test_frontend_detected_by_label():
    impact = ux.detect_frontend_impact(["server/api.go"], labels=["backend", "UI"])
    assert impact.is_frontend is True
    assert any("label" in r for r in impact.reasons)


def test_no_frontend_impact():
    impact = ux.detect_frontend_impact(["server/api.go", "db/schema.sql"], labels=["backend"])
    assert impact.is_frontend is False
    assert impact.frontend_files == []


# --- design token binding ---------------------------------------------------


def test_design_tokens_present():
    spec = {"design": {"tokens": {"colors": {"primary": "#000"}}, "component_library": {"name": "shadcn"}}}
    db = ux.check_design_tokens(spec)
    assert db.has_design_section and db.has_tokens and db.has_component_library
    assert db.component_library == "shadcn"
    assert db.missing == []


def test_design_tokens_missing_library():
    spec = {"design": {"tokens": {"colors": {}}}}
    db = ux.check_design_tokens(spec)
    assert "component_library" in db.missing


def test_no_design_section():
    db = ux.check_design_tokens({"pages": []})
    assert db.has_design_section is False
    assert "design section" in db.missing


def test_component_library_as_plain_string():
    db = ux.check_design_tokens({"design": {"tokens": {"x": 1}, "component_library": "mui"}})
    assert db.component_library == "mui"


# --- reference screenshot discovery -----------------------------------------


def test_discover_reference_screenshots_from_dir(tmp_path):
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "home.png").write_bytes(b"x")
    (ref / "notes.txt").write_text("ignore")
    found = ux.discover_reference_screenshots(tmp_path)
    assert len(found) == 1 and found[0].endswith("home.png")


def test_discover_reference_screenshots_from_ui_spec_assets():
    spec = {"design": {"assets": [{"path": "docs/design/mock.png"}, {"path": "notes.md"}]}}
    found = ux.discover_reference_screenshots(None, spec)
    assert found == ["docs/design/mock.png"]


# --- capture planning -------------------------------------------------------


def test_capture_prefers_browser_use():
    plan = ux.plan_screenshot_capture(browser_use_available=True, playwright_available=True)
    assert plan.tool == "browser-use" and plan.available


def test_capture_falls_back_to_playwright():
    plan = ux.plan_screenshot_capture(playwright_available=True)
    assert plan.tool == "playwright"


def test_capture_blocker_when_contract_but_no_tooling():
    plan = ux.plan_screenshot_capture(has_design_contract=True)
    assert plan.available is False and plan.blocker


def test_capture_no_blocker_without_contract():
    plan = ux.plan_screenshot_capture(has_design_contract=False)
    assert plan.blocker is None


# --- manifest ---------------------------------------------------------------


def test_manifest_skips_gate_for_non_frontend():
    m = ux.build_ux_manifest(["server/api.go"], labels=["backend"])
    assert m.should_run_gate is False
    assert "skipped" in m.render_markdown()


def test_manifest_blocked_for_frontend_with_contract_no_tooling():
    spec = {"design": {"tokens": {"c": 1}, "component_library": "x"}}
    m = ux.build_ux_manifest(["ui/App.tsx"], labels=["frontend"], ui_spec=spec)
    assert m.should_run_gate is True
    assert m.blocked is True
    assert "BLOCKER" in m.render_markdown()


def test_manifest_serializable(tmp_path):
    m = ux.build_ux_manifest(["ui/App.tsx"], playwright_available=True)
    json.loads(json.dumps(m.to_dict()))
    assert m.to_dict()["capture_plan"]["tool"] == "playwright"


# --- CLI --------------------------------------------------------------------


def test_cli_json_non_frontend(tmp_path, capsys):
    changed = tmp_path / "changed.txt"
    changed.write_text("server/api.go\n")
    rc = ux_gate.main(["--changed-files", str(changed), "--label", "backend"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["should_run_gate"] is False


def test_cli_exit_1_when_blocked(tmp_path, capsys):
    spec = tmp_path / "ui-spec.json"
    spec.write_text(json.dumps({"design": {"tokens": {"c": 1}, "component_library": "x"}}))
    rc = ux_gate.main(["--changed", "ui/App.tsx", "--label", "frontend", "--ui-spec", str(spec)])
    assert rc == 1  # design contract present, no capture tooling
