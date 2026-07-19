"""Cross-checks for the shared stable-ID grammar (#166).

The TIM schema, check_traceability.py, and ratchet.py must all build from
chief_wiggum.trace_ids — these tests fail if any copy drifts, which is the
silent-drop failure #166 exists to remove.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import check_traceability as ct
import ratchet
from chief_wiggum import trace_ids

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "templates" / "formal-models" / "tim-schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text())


# --- single source of truth: no copies may drift ---------------------------


def test_scanners_share_the_same_regex_objects():
    # Identity, not equality: a re-declared copy with the same pattern would
    # still be a second place to forget a kind.
    assert ct.ID_RE is trace_ids.ID_RE
    assert ct.DEFINE_RE is trace_ids.DEFINE_RE
    assert ct.TRACE_RE is trace_ids.TRACE_RE
    assert ratchet.ID_RE is trace_ids.ID_RE
    assert ratchet.DEFINE_RE is trace_ids.MD_DEFINE_RE


def test_schema_id_pattern_kinds_match_trace_ids():
    m = re.match(r"\^\((?P<kinds>[A-Z|]+)\)-", SCHEMA["id_pattern"])
    assert m, "tim-schema id_pattern must start with the kind alternation"
    assert tuple(m.group("kinds").split("|")) == trace_ids.ID_KINDS


def test_schema_node_types_cover_all_id_kinds_and_artifact_kinds():
    node_types = set(SCHEMA["node_types"])
    assert set(trace_ids.ID_KINDS) <= node_types
    assert {"code", "test", "probe", "policy", "telemetry"} <= node_types


def test_schema_verbs_match_trace_ids():
    assert set(SCHEMA["link_types"].keys()) == set(trace_ids.VERBS)


def test_schema_link_endpoints_are_declared_node_types():
    node_types = set(SCHEMA["node_types"])
    for verb, rule in SCHEMA["link_types"].items():
        assert set(rule["from"]) <= node_types, verb
        assert set(rule["to"]) <= node_types, verb


# --- new kinds parse everywhere --------------------------------------------


def test_new_kind_ids_match_and_terminate_correctly():
    assert trace_ids.ID_RE.search("BUD-voice-001")
    assert trace_ids.ID_RE.search("ASM-tts-042")
    assert trace_ids.ID_RE.search("EDG-billing-007")
    # must not run into more id chars
    assert not trace_ids.ID_RE.search("BUD-voice-001x")


def test_parse_annotations_accepts_new_verbs_and_kinds():
    assert ct.parse_annotations("# @cw-trace derive BUD-voice-002") == [
        ("derive", ["BUD-voice-002"])
    ]
    assert ct.parse_annotations("// @cw-trace verifies BUD-voice-001 ASM-tts-001") == [
        ("verifies", ["BUD-voice-001", "ASM-tts-001"])
    ]


def test_ratchet_hashes_new_kind_definition_blocks():
    hashes = ratchet._hash_markdown_defs(
        "### BUD-voice-001\n\nMouth-to-ear p95 <= 800ms.\n\n### CTR-order-001\n\nLegacy kind.\n"
    )
    assert set(hashes) == {"BUD-voice-001", "CTR-order-001"}


def test_ratchet_walks_new_kind_json_ids():
    out: dict[str, list[str]] = {}
    ratchet._walk_json_ids({"budgets": [{"id": "BUD-voice-001", "p95_ms": 800}]}, out)
    assert "BUD-voice-001" in out


# --- source-kind classification (#166 verifies targets) ---------------------


def test_classify_source_kind():
    assert ct.classify_source_kind("policies/writers.rego", ".rego") == "policy"
    assert ct.classify_source_kind("k6/latency.js", ".js") == "probe"
    assert ct.classify_source_kind("chaos/tts-down.yaml", ".yaml") == "probe"
    assert ct.classify_source_kind("slo/voice.yaml", ".yaml") == "telemetry"
    assert ct.classify_source_kind("app/handler.py", ".py") == "code"
    assert ct.classify_source_kind("tests/test_handler.py", ".py") == "test"
    assert ct.classify_source_kind("ui/e2e/setup.ts", ".ts") == "test"


# --- fixture round-trip (issue #166 acceptance criterion) -------------------


def test_fixture_epic_with_system_ids_round_trips(tmp_path):
    epic = tmp_path / "docs" / "epics" / "voice"
    epic.mkdir(parents=True)
    (epic / "system-contracts.json").write_text(
        json.dumps(
            {
                "budgets": [
                    {"id": "BUD-voice-001", "kind": "latency", "p95_ms": 800},
                    {"id": "BUD-voice-002", "kind": "latency", "p95_ms": 350},
                ],
                "assumptions": [{"id": "ASM-tts-001", "provider": "elevenlabs"}],
            }
        )
    )
    # derive: child budget -> parent, declared in the epic docs next to the child.
    (epic / "budgets.md").write_text(
        "### BUD-voice-002\n\nLLM TTFT child budget.\n\n@cw-trace derive BUD-voice-001\n"
    )

    src = tmp_path / "repo"
    (src / "app").mkdir(parents=True)
    (src / "k6").mkdir()
    (src / "app" / "pipeline.py").write_text(
        "# @cw-trace guards BUD-voice-001\ndef budget_guard():\n    pass\n"
    )
    (src / "k6" / "latency.js").write_text(
        "// @cw-trace verifies BUD-voice-001 ASM-tts-001\nexport default function () {}\n"
    )

    report = ct.check(epic, src)
    assert report.dangling == []
    assert report.invalid_links == []
    # BUD-/ASM- are not CTR/INV contracts: they must not pollute epic coverage.
    assert "BUD-voice-001" not in report.uncovered_contracts
    assert "ASM-tts-001" not in report.untested_contracts
    assert set(report.defined) == {"BUD-voice-001", "BUD-voice-002", "ASM-tts-001"}


def test_verifies_from_probe_is_valid_but_from_code_still_invalid(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("### CTR-order-001\n\nThe contract.\n")
    src = tmp_path / "repo"
    (src / "app").mkdir(parents=True)
    # verifies may not originate from product code — unchanged behaviour.
    (src / "app" / "handler.py").write_text("# @cw-trace verifies CTR-order-001\n")
    report = ct.check(epic, src)
    assert any(
        d["reason"] == "verifies cannot originate from code" for d in report.invalid_links
    )
