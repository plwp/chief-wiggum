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


def test_generic_dirs_are_not_frontend():
    # app/web/client are ambiguous and must not trigger frontend on their own.
    impact = ux.detect_frontend_impact(
        ["app/models.py", "internal/web/server.go", "pkg/client/http.go"]
    )
    assert impact.is_frontend is False


def test_normalized_label_formats_match():
    for label in ["area/frontend", "type: ui", "frontend-impact"]:
        impact = ux.detect_frontend_impact(["server/api.go"], labels=[label])
        assert impact.is_frontend is True, label


# --- design token binding ---------------------------------------------------


def test_design_tokens_present():
    spec = {"design": {"tokens": {"colors": {"primary": "#000"}}, "component_library": {"name": "shadcn"}}}
    db = ux.check_design_tokens(spec)
    assert db.has_design_section and db.has_tokens and db.has_component_library
    assert db.component_library == "shadcn"
    assert db.missing == []


def test_design_tokens_missing_library():
    spec = {"design": {"tokens": {"colors": {"primary": "#000"}}}}
    db = ux.check_design_tokens(spec)
    assert "component_library" in db.missing


def test_hollow_tokens_are_not_concrete():
    # Empty token containers must not count as having tokens.
    db = ux.check_design_tokens({"design": {"tokens": {"colors": {}}, "component_library": "x"}})
    assert db.has_tokens is False
    assert "tokens" in db.missing


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


def test_discover_reference_screenshots_filters_by_asset_type():
    spec = {
        "design": {
            "assets": [
                {"type": "reference-screenshot", "path": "docs/design/home.png"},
                {"type": "logo", "path": "docs/design/logo.png"},  # not a screenshot
                {"path": "docs/design/untyped.png"},  # no type -> excluded
            ]
        }
    }
    found = ux.discover_reference_screenshots(None, spec)
    assert found == ["docs/design/home.png"]


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


# --- broken instrument: the gate was disarmed by a missing input (#289) -----
#
# The ONLY thing that can set `blocked` is a design contract, and the design
# contract comes from the ui-spec. A typo'd or wrong-root --ui-spec path was
# silently read as "this ticket has no design contract" — so the gate passed
# unconditionally, exactly when its input had gone missing.


def _spec(tmp_path):
    spec = tmp_path / "ui-spec.json"
    spec.write_text(json.dumps({"design": {"tokens": {"c": 1}, "component_library": "x"}}))
    return spec


def test_cli_missing_ui_spec_is_error_not_a_pass(tmp_path, capsys):
    rc = ux_gate.main(["--changed", "ui/App.tsx", "--label", "frontend",
                       "--ui-spec", str(tmp_path / "nope.json"), "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "error"
    assert payload["errors"]


def test_cli_missing_design_dir_is_error(tmp_path, capsys):
    rc = ux_gate.main(["--changed", "ui/App.tsx", "--label", "frontend",
                       "--ui-spec", str(_spec(tmp_path)),
                       "--design-dir", str(tmp_path / "no-design"), "--json"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "error"


def test_cli_missing_changed_files_is_a_usage_error(tmp_path, capsys):
    rc = ux_gate.main(["--changed-files", str(tmp_path / "nope.txt")])
    assert rc == 2
    assert "changed-files" in capsys.readouterr().err


def test_cli_present_inputs_report_the_measured_denominator(tmp_path, capsys):
    design = tmp_path / "design"
    design.mkdir()
    rc = ux_gate.main(["--changed", "ui/App.tsx", "--label", "frontend",
                       "--ui-spec", str(_spec(tmp_path)), "--design-dir", str(design),
                       "--have-playwright", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "pass"
    assert payload["measured"]["changed_files"] == 1
    assert payload["measured"]["ui_spec_loaded"] is True


def test_cli_non_frontend_is_inapplicable_not_pass(tmp_path, capsys):
    changed = tmp_path / "changed.txt"
    changed.write_text("server/api.go\n")
    rc = ux_gate.main(["--changed-files", str(changed), "--label", "backend", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "inapplicable"


def test_cli_blocked_is_findings(tmp_path, capsys):
    rc = ux_gate.main(["--changed", "ui/App.tsx", "--label", "frontend",
                       "--ui-spec", str(_spec(tmp_path)), "--json"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "findings"


def test_cli_no_ui_spec_argument_at_all_is_not_an_error(tmp_path, capsys):
    """Not passing --ui-spec is a caller that has no spec to offer — honest
    absence. Only a NAMED path that is not there is a broken instrument."""
    rc = ux_gate.main(["--changed", "ui/App.tsx", "--label", "frontend", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["outcome"] != "error"


def test_cli_backend_ticket_with_missing_ui_spec_is_not_an_error(tmp_path, capsys):
    """/implement passes --ui-spec and --design-dir unconditionally, and a
    backend-only epic legitimately has neither. Erroring there would be pure
    noise — the disarming only matters when the gate would otherwise run."""
    rc = ux_gate.main(["--changed", "server/api.go", "--label", "backend",
                       "--ui-spec", str(tmp_path / "nope.json"),
                       "--design-dir", str(tmp_path / "no-design"), "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "inapplicable"


def test_cli_frontend_without_design_contract_tolerates_missing_design_dir(tmp_path, capsys):
    """No design section means no reference-screenshot baseline was ever
    promised — an absent docs/design/ is honest absence, not a broken scan."""
    spec = tmp_path / "ui-spec.json"
    spec.write_text(json.dumps({"pages": []}))
    rc = ux_gate.main(["--changed", "ui/App.tsx", "--label", "frontend",
                       "--ui-spec", str(spec),
                       "--design-dir", str(tmp_path / "no-design"), "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "pass"


def test_markdown_output_shows_the_outcome(tmp_path, capsys):
    rc = ux_gate.main(["--changed", "ui/App.tsx", "--label", "frontend",
                       "--ui-spec", str(tmp_path / "nope.json"), "--markdown"])
    assert rc == 1
    assert "ERROR" in capsys.readouterr().out
