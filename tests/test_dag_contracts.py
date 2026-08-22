import json
import subprocess
import sys
from pathlib import Path

import jsonschema
from chief_wiggum.dag import ErrorCode, load_authority_matrix, schema_catalog, validate_record

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "dag" / "v1"
CLI = ROOT / "scripts" / "dag_contract.py"


def _snapshot() -> dict:
    return json.loads((FIXTURES / "valid" / "full-snapshot.json").read_text())


def test_published_schema_catalog_is_offline_and_meta_valid():
    catalog = schema_catalog()
    expected = {
        "intent_node",
        "intent_edge",
        "intent_graph",
        "execution_node",
        "schedulable_edge",
        "relation",
        "evidence_record",
        "approval_record",
        "lease_record",
        "control_record",
        "mutation_envelope",
        "graph_snapshot",
        "graph_manifest",
    }
    assert set(catalog) == expected
    for schema in catalog.values():
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["$id"].startswith("https://chief-wiggum.dev/schemas/dag/v1/")


def test_every_record_in_representative_snapshot_validates_against_published_schema():
    snapshot = _snapshot()
    assert validate_record(snapshot, "graph_snapshot") == ()
    collections = {
        "intent_nodes": "intent_node",
        "intent_edges": "intent_edge",
        "execution_nodes": "execution_node",
        "schedulable_edges": "schedulable_edge",
        "relations": "relation",
        "evidence_records": "evidence_record",
        "approval_records": "approval_record",
        "lease_records": "lease_record",
        "control_records": "control_record",
        "mutations": "mutation_envelope",
    }
    for field, record_type in collections.items():
        for record in snapshot[field]:
            assert validate_record(record, record_type) == (), (field, record)
    manifest = json.loads((FIXTURES / "valid" / "graph-manifest.json").read_text())
    assert validate_record(manifest, "graph_manifest") == ()


def test_schema_version_mismatch_is_loud_typed_error_before_shape_validation():
    record = _snapshot()["intent_nodes"][0]
    record["schema_version"] = "2.0.0"
    errors = validate_record(record, "intent_node")
    assert [error.code for error in errors] == [ErrorCode.SCHEMA_VERSION_UNSUPPORTED]
    assert errors[0].path == "/schema_version"
    assert errors[0].phase == "version"


def test_record_type_mismatch_is_typed():
    errors = validate_record(_snapshot()["intent_nodes"][0], "execution_node")
    assert errors[0].code is ErrorCode.RECORD_TYPE_MISMATCH


def test_authority_matrix_is_machine_readable_and_complete():
    matrix = load_authority_matrix()
    assert matrix["schema_version"] == "1.0.0"
    assert matrix["operations"]
    assert all(set(row) == {"operation_type", "automatic", "approval_required"} for row in matrix["operations"])
    assert any(row["approval_required"] for row in matrix["operations"])


def test_schemas_and_fixtures_are_provider_and_model_name_free():
    forbidden = {"anthropic", "claude", "openai", "codex", "gemini", "deepseek", "openrouter", "ox-alpha"}
    paths = list((ROOT / "schemas" / "dag").rglob("*.json")) + list(FIXTURES.rglob("*.json"))
    text = "\n".join(path.read_text().lower() for path in paths)
    assert not (forbidden & set(text.replace('"', " ").replace(":", " ").split()))
    for token in forbidden:
        assert token not in text


def test_validation_cli_emits_typed_version_error(tmp_path):
    record = _snapshot()["intent_nodes"][0]
    record["schema_version"] = "9.0.0"
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record))
    proc = subprocess.run(
        [sys.executable, str(CLI), "validate", str(path), "--record-type", "intent_node"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["errors"][0]["code"] == "SCHEMA_VERSION_UNSUPPORTED"


def test_validation_cli_accepts_valid_snapshot():
    proc = subprocess.run(
        [sys.executable, str(CLI), "validate", str(FIXTURES / "valid" / "full-snapshot.json")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout) == {"ok": True, "record_type": "graph_snapshot", "schema_version": "1.0.0"}
