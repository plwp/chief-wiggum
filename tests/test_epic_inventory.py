"""Tests for epic artifact discovery and context loading (P0-5)."""

from __future__ import annotations

import json

import epic_inventory
from chief_wiggum import artifacts


def _epic_dir(repo, slug="order-lifecycle"):
    d = repo / "docs" / "epics" / slug
    (d / "models").mkdir(parents=True)
    return d


# --- no epic / missing docs -------------------------------------------------


def test_no_epic_slug_reports_no_epic(tmp_path):
    inv = artifacts.build_inventory(tmp_path)
    assert inv.epic_dir is None
    assert inv.flags["HAS_EPIC"] is False
    assert all(v is False for v in inv.markdown_artifacts.values())


def test_epic_slug_without_dir_warns(tmp_path):
    inv = artifacts.build_inventory(tmp_path, epic_slug="ghost")
    assert inv.epic_dir_exists is False
    assert inv.flags["HAS_EPIC"] is False
    assert any("epic directory does not exist" in w for w in inv.warnings)


# --- full epic docs ---------------------------------------------------------


def test_full_epic_docs_set_flags(tmp_path):
    epic = _epic_dir(tmp_path)
    (epic / "contracts.md").write_text("# Contracts")
    (epic / "invariants.md").write_text("# Invariants")
    (epic / "models" / "state-machines.json").write_text('{"states": []}')
    (epic / "models" / "ui-spec.json").write_text('{"design": {}}')
    (epic / "models" / "transition-map.json").write_text('{"transitions": []}')

    inv = artifacts.build_inventory(tmp_path, epic_slug="order-lifecycle", issue=42)
    assert inv.flags["HAS_EPIC"] is True
    assert inv.flags["HAS_FORMAL_MODELS"] is True
    assert inv.flags["HAS_UI_SPEC"] is True
    assert inv.flags["HAS_TRANSITION_MAP"] is True
    assert inv.markdown_artifacts["contracts.md"] is True
    assert inv.markdown_artifacts["retrospective.md"] is False  # missing optional
    assert inv.issue == 42


def test_missing_optional_model_artifacts_flagged_false(tmp_path):
    epic = _epic_dir(tmp_path)
    (epic / "models" / "state-machines.json").write_text('{"states": []}')
    inv = artifacts.build_inventory(tmp_path, epic_slug="order-lifecycle")
    assert inv.flags["HAS_FORMAL_MODELS"] is True
    assert inv.flags["HAS_UI_SPEC"] is False
    assert inv.flags["HAS_TRANSITION_MAP"] is False


# --- malformed model JSON ---------------------------------------------------


def test_malformed_model_json_warns_but_does_not_crash(tmp_path):
    epic = _epic_dir(tmp_path)
    (epic / "models" / "state-machines.json").write_text("{not valid json")
    inv = artifacts.build_inventory(tmp_path, epic_slug="order-lifecycle")
    # Still discovered as present, but flagged.
    assert inv.model_artifacts["state-machines.json"] is True
    assert any("malformed model artifact state-machines.json" in w for w in inv.warnings)


def test_malformed_model_does_not_set_flag_true(tmp_path):
    # A broken model must not advertise HAS_FORMAL_MODELS — downstream steps
    # would try to read/generate from it and crash.
    epic = _epic_dir(tmp_path)
    (epic / "models" / "ui-spec.json").write_text("{not valid json")
    inv = artifacts.build_inventory(tmp_path, epic_slug="order-lifecycle")
    assert inv.flags["HAS_UI_SPEC"] is False


def test_mixed_blocked_refs_keep_numeric_and_warn_on_rest(tmp_path):
    epic = _epic_dir(tmp_path)

    def scanner(_targets):
        return []

    def blocked(_findings):
        return {"#43": 1, "AC-1": 1, "#7": 2}

    inv = artifacts.build_inventory(
        tmp_path, epic_slug="order-lifecycle", scanner=scanner, blocked_fn=blocked
    )
    assert inv.blocked_tickets == [7, 43]
    assert any("unparseable blocked ticket ref" in w for w in inv.warnings)


def test_directory_named_like_artifact_is_not_counted(tmp_path):
    # A directory named ui-spec.json must not register as the model file.
    epic = _epic_dir(tmp_path)
    (epic / "models" / "ui-spec.json").mkdir()
    inv = artifacts.build_inventory(tmp_path, epic_slug="order-lifecycle")
    assert inv.model_artifacts["ui-spec.json"] is False
    assert inv.flags["HAS_UI_SPEC"] is False


# --- unresolved marker propagation ------------------------------------------


def test_unresolved_markers_propagate_blocked_tickets(tmp_path):
    epic = _epic_dir(tmp_path)
    # A contract value carrying a TBD marker with ticket provenance.
    model = {
        "contracts": [
            {
                "expression": "x > 0 -- TBD: confirm against source",
                "derived_from": [{"type": "ticket", "ref": "#43"}],
            }
        ]
    }
    (epic / "models" / "contracts.json").write_text(json.dumps(model))
    inv = artifacts.build_inventory(tmp_path, epic_slug="order-lifecycle")
    assert inv.flags["HAS_UNRESOLVED"] is True
    assert inv.unresolved
    assert 43 in inv.blocked_tickets


def test_scan_failure_is_caught_and_warned(tmp_path):
    epic = _epic_dir(tmp_path)
    (epic / "contracts.md").write_text("# Contracts")

    def boom(_targets):
        raise RuntimeError("scanner exploded")

    inv = artifacts.build_inventory(
        tmp_path, epic_slug="order-lifecycle", scanner=boom
    )
    assert any("unresolved scan failed" in w for w in inv.warnings)
    # Discovery still produced flags/artifacts.
    assert inv.markdown_artifacts["contracts.md"] is True


# --- design artifacts -------------------------------------------------------


def test_design_artifacts_detected(tmp_path):
    design = tmp_path / "docs" / "design"
    design.mkdir(parents=True)
    (design / "design.json").write_text("{}")
    inv = artifacts.build_inventory(tmp_path)
    assert inv.flags["HAS_DESIGN"] is True
    assert inv.design_artifacts["design.json"] is True


# --- serialization / rendering / CLI ----------------------------------------


def test_inventory_is_json_serializable_and_renders_markdown(tmp_path):
    epic = _epic_dir(tmp_path)
    (epic / "contracts.md").write_text("# Contracts")
    inv = artifacts.build_inventory(tmp_path, epic_slug="order-lifecycle", issue=7)
    json.loads(inv.to_json())  # does not raise
    md = inv.render_markdown()
    assert "# Epic Artifact Inventory" in md
    assert "HAS_EPIC" in md
    assert "Ticket: #7" in md


def test_cli_emits_json(tmp_path, capsys):
    _epic_dir(tmp_path)
    rc = epic_inventory.main([str(tmp_path), "--epic-slug", "order-lifecycle"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["flags"]["HAS_EPIC"] is True
