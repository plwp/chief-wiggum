"""Tests for epic artifact discovery and context loading (P0-5)."""

from __future__ import annotations

import json
import subprocess

import artifacts as resolver_mod  # scripts/artifacts.py, the meta-location resolver (#213)
import epic_inventory
import pytest
from chief_wiggum import artifacts


@pytest.fixture(autouse=True)
def user_dir(tmp_path, monkeypatch):
    """Isolate every test from the real ~/.chief-wiggum — tests must never
    touch it (build_inventory now consults the resolver on every call)."""
    d = tmp_path / "cw-user"
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(d))
    return d


def _epic_dir(repo, slug="order-lifecycle"):
    d = repo / "docs" / "epics" / slug
    (d / "models").mkdir(parents=True)
    return d


def _git(repo, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def make_sidecar_target(tmp_path, remote="https://github.com/acme/app.git"):
    """A tmp git repo elected into sidecar mode — its epic/design artifacts
    live under the sidecar meta root, NOT under <repo>/docs."""
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", remote)
    resolver_mod.elect(repo, "sidecar", backing="local")
    return repo


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


# --- epic_status: distinguishing "no epic" from "epic requested but missing" (#286) ---


def test_epic_status_none_when_no_slug_requested(tmp_path):
    inv = artifacts.build_inventory(tmp_path)
    assert inv.epic_status == "none"


def test_epic_status_missing_when_slug_not_found(tmp_path):
    inv = artifacts.build_inventory(tmp_path, epic_slug="ghost")
    assert inv.epic_status == "missing"


def test_epic_status_present_when_slug_found(tmp_path):
    _epic_dir(tmp_path)
    inv = artifacts.build_inventory(tmp_path, epic_slug="order-lifecycle")
    assert inv.epic_status == "present"


# --- sidecar awareness (#286) -------------------------------------------------


def test_sidecar_epic_resolves_through_meta_location_resolver(tmp_path):
    """On a sidecar-elected target, build_inventory must find the epic under
    the sidecar meta root, not under <repo>/docs/epics — and nothing must be
    written into the target tree in the process."""
    repo = make_sidecar_target(tmp_path)
    resolver = resolver_mod.Resolver.resolve(repo)
    assert resolver.mode == "sidecar"
    epic = resolver.epic_dir("order-lifecycle")
    (epic / "models").mkdir(parents=True)
    (epic / "contracts.md").write_text("# Contracts")
    (epic / "invariants.md").write_text("# Invariants")
    (epic / "models" / "state-machines.json").write_text('{"states": []}')

    inv = artifacts.build_inventory(repo, epic_slug="order-lifecycle", issue=42)

    assert inv.flags["HAS_EPIC"] is True
    assert inv.flags["HAS_FORMAL_MODELS"] is True
    assert inv.epic_status == "present"
    assert inv.epic_dir == str(epic)
    assert inv.markdown_artifacts["contracts.md"] is True
    assert not (repo / "docs").exists()  # zero footprint in the target tree


def test_sidecar_epic_requested_but_missing_is_loud(tmp_path):
    repo = make_sidecar_target(tmp_path)
    inv = artifacts.build_inventory(repo, epic_slug="ghost")
    assert inv.epic_status == "missing"
    assert inv.flags["HAS_EPIC"] is False
    assert any("epic directory does not exist" in w for w in inv.warnings)
    # the warning must point at the SIDECAR path, not a bogus in-tree one
    assert str(resolver_mod.Resolver.resolve(repo).meta_root) in inv.epic_dir


def test_sidecar_design_artifacts_detected(tmp_path):
    repo = make_sidecar_target(tmp_path)
    resolver = resolver_mod.Resolver.resolve(repo)
    design = resolver.design_dir()
    design.mkdir(parents=True)
    (design / "design.json").write_text("{}")

    inv = artifacts.build_inventory(repo)
    assert inv.flags["HAS_DESIGN"] is True
    assert inv.design_artifacts["design.json"] is True
    assert not (repo / "docs").exists()


def test_embedded_mode_unchanged_by_resolver_adoption(tmp_path):
    """Embedded (no election) must resolve to the exact same paths as before
    the resolver was wired in."""
    epic = _epic_dir(tmp_path)
    (epic / "contracts.md").write_text("# Contracts")
    inv = artifacts.build_inventory(tmp_path, epic_slug="order-lifecycle")
    assert inv.epic_dir == str(tmp_path / "docs" / "epics" / "order-lifecycle")
    assert inv.flags["HAS_EPIC"] is True


# --- explicit epic_dir override (#286 proposal item 2) ------------------------


def test_explicit_epic_dir_override_is_used_instead_of_resolving(tmp_path):
    """A caller that has ALREADY resolved EPIC_DIR (as /implement Step 1 does)
    can pass it directly rather than have build_inventory re-derive it."""
    override = tmp_path / "elsewhere" / "order-lifecycle"
    (override / "models").mkdir(parents=True)
    (override / "contracts.md").write_text("# Contracts")

    inv = artifacts.build_inventory(
        tmp_path, epic_slug="order-lifecycle", epic_dir=override
    )
    assert inv.epic_dir == str(override)
    assert inv.epic_status == "present"
    assert inv.flags["HAS_EPIC"] is True
    assert inv.markdown_artifacts["contracts.md"] is True


def test_cli_accepts_epic_dir_override(tmp_path, capsys):
    override = tmp_path / "elsewhere" / "order-lifecycle"
    override.mkdir(parents=True)
    (override / "contracts.md").write_text("# Contracts")

    rc = epic_inventory.main(
        [str(tmp_path), "--epic-slug", "order-lifecycle", "--epic-dir", str(override)]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["epic_dir"] == str(override)
    assert data["flags"]["HAS_EPIC"] is True


# --- fail closed on resolver errors (malformed election) ----------------------


def test_malformed_election_propagates_instead_of_defaulting_to_embedded(tmp_path):
    """Never silently fall through to embedded on a resolver error — a
    malformed election must abort discovery loudly (matching
    Resolver.resolve's own fail-closed contract)."""
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")

    target_id = resolver_mod.derive_target_id(repo)
    election = resolver_mod.election_path(target_id)
    election.parent.mkdir(parents=True, exist_ok=True)
    election.write_text("{not json")

    with pytest.raises(ValueError, match="election"):
        artifacts.build_inventory(repo)


def test_cli_reports_error_on_malformed_election(tmp_path, capsys):
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")

    target_id = resolver_mod.derive_target_id(repo)
    election = resolver_mod.election_path(target_id)
    election.parent.mkdir(parents=True, exist_ok=True)
    election.write_text("{not json")

    rc = epic_inventory.main([str(repo)])
    assert rc == 1
    assert "election" in capsys.readouterr().err
