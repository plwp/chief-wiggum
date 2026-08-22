import json
from pathlib import Path

from chief_wiggum import github, planning
from chief_wiggum.dag import (
    dependency_block_to_intent_graph,
    project_legacy_waves,
    validate_record,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dag" / "v1" / "compatibility" / "factory-hardening.json"
SIX_FIELDS = ["waves", "gated", "skipped", "warnings", "integration_risks", "gate_reasons"]


def test_real_historical_epic_round_trips_to_exact_six_field_wave_plan():
    """@cw-trace verifies CTR-dag-007 CTR-dag-008 INV-dag-006"""
    fixture = json.loads(FIXTURE.read_text())
    metadata = github.parse_dependency_block(fixture["description"])
    oracle = planning.plan_waves(fixture["issues"], metadata.edges, gated=fixture["gated"])
    oracle.warnings = metadata.warnings + oracle.warnings

    intent = dependency_block_to_intent_graph(
        fixture["description"],
        graph_id="GRF-dag-compat",
        issues=fixture["issues"],
        source_ref=fixture["source_ref"],
    )
    result = project_legacy_waves(intent, gated=fixture["gated"])

    assert validate_record(intent, "intent_graph") == ()
    assert result.exit_code == 0
    assert list(result.plan) == SIX_FIELDS
    assert result.plan == oracle.to_dict()


def test_compatibility_projection_preserves_cycle_and_malformed_exit_semantics():
    """@cw-trace verifies CTR-dag-008 INV-dag-006"""
    cycle = "<!-- DEPENDENCIES\n#1: [#2]\n#2: [#1]\n-->"
    intent = dependency_block_to_intent_graph(cycle, graph_id="GRF-dag-cycle", issues=[1, 2], source_ref="fixture:cycle")
    assert project_legacy_waves(intent).exit_code == 2

    malformed = "<!-- DEPENDENCIES\n#1: []\n#2: [#1 typo]\n-->"
    intent = dependency_block_to_intent_graph(malformed, graph_id="GRF-dag-bad", issues=[1, 2], source_ref="fixture:bad")
    result = project_legacy_waves(intent)
    assert result.exit_code == 1
    assert "malformed dependency line" in result.error

    missing = dependency_block_to_intent_graph(
        "milestone description without dependency metadata",
        graph_id="GRF-dag-missing",
        issues=[1, 2],
        source_ref="fixture:missing",
    )
    assert missing["scan_status"] == "unscanned"
    assert project_legacy_waves(missing).exit_code == 1


def test_intent_graph_uses_depends_on_direction_source_after_target():
    intent = dependency_block_to_intent_graph(
        "<!-- DEPENDENCIES\n#43: [#42]\n-->",
        graph_id="GRF-dag-direction",
        issues=[42, 43],
        source_ref="fixture:direction",
    )
    assert [(edge["source_ticket"], edge["target_ticket"]) for edge in intent["edges"]] == [(43, 42)]


def test_schema_valid_non_ticket_intent_fails_closed_in_projection():
    intent = dependency_block_to_intent_graph(
        "<!-- DEPENDENCIES\n#43: []\n-->",
        graph_id="GRF-dag-nonticket",
        issues=[43],
        source_ref="fixture:nonticket",
    )
    del intent["nodes"][0]["source_ticket"]
    assert validate_record(intent, "intent_graph") == ()
    assert project_legacy_waves(intent).exit_code == 1
