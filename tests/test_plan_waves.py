"""Tests for wave planning and gating (P0-3)."""

from __future__ import annotations

import json

import plan_waves
import pytest
from chief_wiggum import planning

# --- topology: independent / chains / diamonds ------------------------------


def test_independent_tickets_form_one_wave():
    plan = planning.plan_waves([1, 2, 3], {1: [], 2: [], 3: []})
    assert plan.waves == [[1, 2, 3]]
    assert plan.gated == []


def test_chain_forms_sequential_waves():
    plan = planning.plan_waves([1, 2, 3], {1: [], 2: [1], 3: [2]})
    assert plan.waves == [[1], [2], [3]]


def test_diamond_orders_correctly_and_flags_risk():
    # 1 -> {2,3} -> 4
    edges = {1: [], 2: [1], 3: [1], 4: [2, 3]}
    plan = planning.plan_waves([1, 2, 3, 4], edges)
    assert plan.waves == [[1], [2, 3], [4]]
    # Only #1 is a shared dependency (of #2 and #3); #4's deps each have one dependent.
    assert any("#1 is a shared dependency" in r for r in plan.integration_risks)
    assert all(not r.startswith("#4") for r in plan.integration_risks)


# --- cycles -----------------------------------------------------------------


def test_cycle_raises():
    with pytest.raises(planning.DependencyCycleError):
        planning.plan_waves([1, 2], {1: [2], 2: [1]})


def test_self_cycle_raises():
    with pytest.raises(planning.DependencyCycleError):
        planning.plan_waves([1], {1: [1]})


# --- closed dependencies ----------------------------------------------------


def test_closed_dependency_unblocks_dependent():
    # 2 depends on 1; 1 is already closed -> 2 builds in wave 1.
    plan = planning.plan_waves([1, 2], {1: [], 2: [1]}, closed=[1])
    assert plan.skipped == [1]
    assert plan.waves == [[2]]


def test_dependency_on_external_closed_issue_is_satisfied():
    # 5 depends on 99 (not in epic) but 99 is closed -> 5 is buildable.
    plan = planning.plan_waves([5], {5: [99]}, closed=[99])
    assert plan.waves == [[5]]
    assert plan.gated == []


# --- missing dependency references ------------------------------------------


def test_missing_dependency_reference_blocks_and_warns():
    # 5 depends on 99 which is neither in the epic nor closed.
    plan = planning.plan_waves([5], {5: [99]})
    assert plan.waves == []
    assert plan.gated == [5]
    assert any("missing/unknown #99" in w for w in plan.warnings)


# --- gating + transitive gates ----------------------------------------------


def test_gated_ticket_is_held_back():
    plan = planning.plan_waves([1, 2], {1: [], 2: []}, gated=[2])
    assert plan.waves == [[1]]
    assert plan.gated == [2]
    assert "gated" in plan.gate_reasons[2]


def test_transitive_gate_holds_back_dependents():
    # 1 gated; 2 depends on 1; 3 depends on 2 -> all held back.
    edges = {1: [], 2: [1], 3: [2], 4: []}
    plan = planning.plan_waves([1, 2, 3, 4], edges, gated=[1])
    assert plan.waves == [[4]]
    assert plan.gated == [1, 2, 3]
    assert "depends on blocked #1" in plan.gate_reasons[2]
    assert "depends on blocked #2" in plan.gate_reasons[3]


def test_partial_gate_still_builds_independent_branch():
    # Two independent chains; gate one root, the other still builds.
    edges = {1: [], 2: [1], 10: [], 20: [10]}
    plan = planning.plan_waves([1, 2, 10, 20], edges, gated=[1])
    assert plan.waves == [[10], [20]]
    assert plan.gated == [1, 2]


# --- serialization / rendering ----------------------------------------------


def test_to_dict_is_json_serializable():
    plan = planning.plan_waves([1, 2], {1: [], 2: [1]}, gated=[])
    blob = json.dumps(plan.to_dict())
    data = json.loads(blob)
    assert data["waves"] == [[1], [2]]
    assert set(data) == {
        "waves", "gated", "skipped", "warnings", "integration_risks", "gate_reasons"
    }


def test_render_markdown_includes_sections():
    edges = {1: [], 2: [1], 3: [1]}
    plan = planning.plan_waves([1, 2, 3], edges, gated=[3])
    md = planning.render_markdown(plan)
    assert "# Wave Plan" in md
    assert "Wave 1" in md
    assert "Gated / blocked" in md
    assert "Integration risks" in md


# --- CLI --------------------------------------------------------------------


def test_cli_emits_json(capsys):
    rc = plan_waves.main(["--issues", "1,2,3", "--edges", '{"1": [], "2": [1], "3": [2]}'])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["waves"] == [[1], [2], [3]]


def test_cli_cycle_exits_2(capsys):
    rc = plan_waves.main(["--issues", "1,2", "--edges", '{"1": [2], "2": [1]}'])
    assert rc == 2
    assert "cycle" in capsys.readouterr().err


def test_cli_reads_deps_json_shape(tmp_path, capsys):
    deps = {"edges": {"1": [], "2": [1]}, "warnings": [], "has_block": True}
    path = tmp_path / "deps.json"
    path.write_text(json.dumps(deps))
    rc = plan_waves.main(["--deps-json", str(path), "--issues", "1,2"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["waves"] == [[1], [2]]
