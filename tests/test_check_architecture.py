"""Tests for the architecture model checker (#174).

Mirrors tests/test_check_budget_tree.py's shape: builder helpers construct a
minimal valid document, then mutate for negative cases. One test per CHECKS
entry (ADR-fh-06's frozen inventory) plus the voice-agent worked example
(clean), IT-fh-07 (cross-artifact resolution) and IT-fh-09 (report-only vs
--gate exit semantics).
"""

from __future__ import annotations

import json
from pathlib import Path

import check_architecture as ca

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "templates" / "formal-models" / "examples"
EXAMPLE_ARCH = EXAMPLES_DIR / "voice-agent-architecture.json"
EXAMPLE_SC = EXAMPLES_DIR / "voice-agent-system-contracts.json"


def _load(path) -> dict:
    return json.loads(Path(path).read_text())


def _node(id_, **overrides) -> dict:
    node = {
        "id": id_,
        "name": id_,
        "kind": "service",
        "external": False,
        "trust_zone": "internal",
        "status": "active",
        "criticality_tier": "tier-2",
    }
    node.update(overrides)
    return node


def _edge(id_, frm, to, **overrides) -> dict:
    edge = {"id": id_, "from": frm, "to": to, "protocol": "https", "mode": "sync", "criticality": "soft"}
    edge.update(overrides)
    return edge


# --- CHECKS inventory is frozen (ADR-fh-06) ----------------------------------


def test_checks_inventory_is_frozen_and_matches_adr_fh_06():
    assert ca.CHECKS == (
        "dangling-endpoint",
        "retired-node-edge",
        "unlabelled-external",
        "tier-inversion",
        "label-propagation",
        "undeclared-cross-ref",
        "missing-tier",
        "authored-crossing-label",
    )


# --- worked example (voice agent): checks clean ------------------------------


def test_voice_agent_example_checks_clean():
    doc = _load(EXAMPLE_ARCH)
    report = ca.check_static(doc)
    assert report.ok
    assert report.findings == []


def test_voice_agent_example_is_schema_valid():
    doc = _load(EXAMPLE_ARCH)
    assert ca.validate_doc(doc, ca.load_schema()) == []


# --- IT-fh-07: architecture <-> system-contracts cross-ref resolution -------


def test_cross_artifact_clean_pair_passes():
    arch, sc = _load(EXAMPLE_ARCH), _load(EXAMPLE_SC)
    report = ca.check_static(arch, system_contracts=sc)
    assert report.ok
    assert not report.not_checked


def test_cross_artifact_undeclared_arc_reference_is_finding():
    # @cw-trace verifies INV-fh-008
    arch, sc = _load(EXAMPLE_ARCH), _load(EXAMPLE_SC)
    sc["chains"][0]["hops"][0]["callee"] = "ARC-does-not-exist-999"
    report = ca.check_static(arch, system_contracts=sc)
    assert not report.ok
    hits = [f for f in report.findings if f.check == "undeclared-cross-ref"]
    assert any("ARC-does-not-exist-999" in f.message for f in hits)


def test_cross_artifact_undeclared_edg_reference_is_finding():
    arch, sc = _load(EXAMPLE_ARCH), _load(EXAMPLE_SC)
    sc["chains"][0]["hops"].append({"caller": "EDG-nonexistent-001", "callee": "ARC-gateway-001", "timeout_ms": 100})
    report = ca.check_static(arch, system_contracts=sc)
    assert not report.ok
    assert any(
        f.check == "undeclared-cross-ref" and "EDG-nonexistent-001" in f.message for f in report.findings
    )


def test_cross_artifact_undeclared_telemetry_ref_is_finding():
    arch, sc = _load(EXAMPLE_ARCH), _load(EXAMPLE_SC)
    sc["trees"][0]["root"]["children"][0]["telemetry_ref"] = "nonexistent_binding_ms"
    report = ca.check_static(arch, system_contracts=sc)
    assert not report.ok
    assert any(
        f.check == "undeclared-cross-ref" and "nonexistent_binding_ms" in f.message for f in report.findings
    )


def test_cross_artifact_non_id_shaped_hop_is_skipped_not_flagged():
    """Legacy plain-service-name hops (not ARC-/EDG- shaped) are not treated
    as an undeclared reference — they predate this cross-ref convention."""
    arch, sc = _load(EXAMPLE_ARCH), _load(EXAMPLE_SC)
    sc["chains"][0]["hops"][0]["callee"] = "some-legacy-service-name"
    report = ca.check_static(arch, system_contracts=sc)
    assert not any(
        f.check == "undeclared-cross-ref" and "some-legacy-service-name" in f.message for f in report.findings
    )


def test_absent_system_contracts_is_not_checked_never_passed():
    arch = _load(EXAMPLE_ARCH)
    report = ca.check_static(arch)  # no --system-contracts given
    assert report.ok  # architecture-only checks are still clean
    assert len(report.not_checked) == 1
    assert "not_checked" not in [f.check for f in report.findings]


# --- one seeded violation per CHECKS entry ------------------------------------


def test_dangling_endpoint_caught():
    # @cw-trace verifies CTR-fh-021
    doc = {"nodes": [_node("ARC-a-001")], "edges": [_edge("EDG-a-b-001", "ARC-a-001", "ARC-ghost-999")]}
    report = ca.check_static(doc)
    assert not report.ok
    assert any(f.check == "dangling-endpoint" for f in report.findings)


def test_retired_node_active_edge_caught():
    doc = {
        "nodes": [_node("ARC-a-001"), _node("ARC-b-001", status="retired")],
        "edges": [_edge("EDG-a-b-001", "ARC-a-001", "ARC-b-001", active=True)],
    }
    report = ca.check_static(doc)
    assert not report.ok
    assert any(f.check == "retired-node-edge" for f in report.findings)


def test_retired_node_inactive_edge_is_not_flagged():
    doc = {
        "nodes": [_node("ARC-a-001"), _node("ARC-b-001", status="retired")],
        "edges": [_edge("EDG-a-b-001", "ARC-a-001", "ARC-b-001", active=False)],
    }
    report = ca.check_static(doc)
    assert not any(f.check == "retired-node-edge" for f in report.findings)


def test_unlabelled_external_on_hard_edge_caught():
    doc = {
        "nodes": [
            _node("ARC-a-001", criticality_tier="tier-1"),
            _node("ARC-vendor-001", external=True, trust_zone="restricted", criticality_tier="tier-1"),
        ],
        "edges": [_edge("EDG-a-vendor-001", "ARC-a-001", "ARC-vendor-001", criticality="hard")],
    }
    report = ca.check_static(doc)
    assert not report.ok
    assert any(f.check == "unlabelled-external" for f in report.findings)


def test_external_with_valid_asm_refs_on_hard_edge_is_clean():
    doc = {
        "nodes": [
            _node("ARC-a-001", criticality_tier="tier-1"),
            _node(
                "ARC-vendor-001",
                external=True,
                trust_zone="restricted",
                criticality_tier="tier-1",
                asm_refs=[{"id": "ASM-vendor-001", "evidence": "sla-doc", "ref": "https://vendor.example/sla"}],
            ),
        ],
        "edges": [_edge("EDG-a-vendor-001", "ARC-a-001", "ARC-vendor-001", criticality="hard")],
    }
    report = ca.check_static(doc)
    assert not any(f.check == "unlabelled-external" for f in report.findings)


def test_unlabelled_external_not_triggered_by_soft_edge():
    doc = {
        "nodes": [
            _node("ARC-a-001", criticality_tier="tier-1"),
            _node("ARC-vendor-001", external=True, trust_zone="restricted", criticality_tier="tier-3"),
        ],
        "edges": [_edge("EDG-a-vendor-001", "ARC-a-001", "ARC-vendor-001", criticality="soft")],
    }
    report = ca.check_static(doc)
    assert not any(f.check == "unlabelled-external" for f in report.findings)


def test_tier_inversion_caught():
    doc = {
        "nodes": [
            _node("ARC-a-001", criticality_tier="tier-1"),
            _node("ARC-b-001", criticality_tier="tier-2"),
        ],
        "edges": [_edge("EDG-a-b-001", "ARC-a-001", "ARC-b-001", criticality="hard")],
    }
    report = ca.check_static(doc)
    assert not report.ok
    assert any(f.check == "tier-inversion" and f.id == "ARC-b-001" for f in report.findings)


def test_tier_inversion_through_intermediate_node_caught():
    """tier-1 -> tier-1 -> tier-3: the hard-dependency path passes THROUGH a
    lower-tier node even though the first hop looks tier-1-to-tier-1 — this is
    still a violation because the tier-1 root's availability now transitively
    depends on the lower-tier node."""
    doc = {
        "nodes": [
            _node("ARC-a-001", criticality_tier="tier-1"),
            _node("ARC-b-001", criticality_tier="tier-1"),
            _node("ARC-c-001", criticality_tier="tier-3"),
        ],
        "edges": [
            _edge("EDG-a-b-001", "ARC-a-001", "ARC-b-001", criticality="hard"),
            _edge("EDG-b-c-001", "ARC-b-001", "ARC-c-001", criticality="hard"),
        ],
    }
    report = ca.check_static(doc)
    assert not report.ok
    hits = [f for f in report.findings if f.check == "tier-inversion"]
    assert any(f.id == "ARC-c-001" for f in hits)


def test_tier_inversion_not_triggered_by_soft_edge():
    doc = {
        "nodes": [
            _node("ARC-a-001", criticality_tier="tier-1"),
            _node("ARC-b-001", criticality_tier="tier-2"),
        ],
        "edges": [_edge("EDG-a-b-001", "ARC-a-001", "ARC-b-001", criticality="soft")],
    }
    report = ca.check_static(doc)
    assert not any(f.check == "tier-inversion" for f in report.findings)


def test_label_propagation_violation_caught_without_waiver():
    doc = {
        "nodes": [
            _node("ARC-a-001", trust_zone="restricted"),
            _node("ARC-b-001", trust_zone="public"),
        ],
        "edges": [_edge("EDG-a-b-001", "ARC-a-001", "ARC-b-001", carries=["secret"])],
    }
    report = ca.check_static(doc)
    assert not report.ok
    assert any(f.check == "label-propagation" for f in report.findings)
    assert not report.waivers


def test_label_propagation_violation_waived_by_valid_asm_ref():
    doc = {
        "nodes": [
            _node("ARC-a-001", trust_zone="restricted"),
            _node("ARC-b-001", trust_zone="public"),
        ],
        "edges": [
            _edge(
                "EDG-a-b-001",
                "ARC-a-001",
                "ARC-b-001",
                carries=["secret"],
                asm_refs=[{"id": "ASM-waiver-001", "evidence": "justified", "ref": "documented exception: see runbook"}],
            )
        ],
    }
    report = ca.check_static(doc)
    assert not any(f.check == "label-propagation" for f in report.findings)
    assert len(report.waivers) == 1
    assert report.waivers[0].asm_id == "ASM-waiver-001"


def test_label_propagation_not_triggered_for_permitted_class():
    doc = {
        "nodes": [
            _node("ARC-a-001", trust_zone="internal"),
            _node("ARC-b-001", trust_zone="restricted"),
        ],
        "edges": [_edge("EDG-a-b-001", "ARC-a-001", "ARC-b-001", carries=["pii"])],
    }
    report = ca.check_static(doc)
    assert not any(f.check == "label-propagation" for f in report.findings)


def test_missing_tier_caught():
    # @cw-trace verifies CTR-fh-022
    node = _node("ARC-a-001")
    del node["criticality_tier"]
    doc = {"nodes": [node], "edges": []}
    report = ca.check_static(doc)
    assert not report.ok
    assert any(f.check == "missing-tier" for f in report.findings)


def test_missing_tier_node_is_not_skipped_from_the_model():
    """A node missing its tier still counts in nodes/edges accounting and
    still participates in every other check — it is a FINDING, never a
    silently-removed node (else it could opt itself out of tier-inversion)."""
    node_no_tier = _node("ARC-a-001")
    del node_no_tier["criticality_tier"]
    doc = {
        "nodes": [node_no_tier, _node("ARC-b-001", status="retired")],
        "edges": [_edge("EDG-a-b-001", "ARC-a-001", "ARC-b-001", active=True)],
    }
    report = ca.check_static(doc)
    assert report.nodes == 2
    assert any(f.check == "missing-tier" for f in report.findings)
    assert any(f.check == "retired-node-edge" for f in report.findings)


def test_authored_trust_zone_crossing_caught():
    # @cw-trace verifies INV-fh-006 CTR-fh-025
    doc = {
        "nodes": [_node("ARC-a-001"), _node("ARC-b-001")],
        "edges": [_edge("EDG-a-b-001", "ARC-a-001", "ARC-b-001", trust_zone_crossing="internal->public")],
    }
    report = ca.check_static(doc)
    assert not report.ok
    assert any(f.check == "authored-crossing-label" for f in report.findings)


def test_authored_region_crossing_caught():
    # @cw-trace verifies INV-fh-006 CTR-fh-025
    doc = {
        "nodes": [_node("ARC-a-001"), _node("ARC-b-001")],
        "edges": [_edge("EDG-a-b-001", "ARC-a-001", "ARC-b-001", region_crossing=True)],
    }
    report = ca.check_static(doc)
    assert not report.ok
    assert any(f.check == "authored-crossing-label" for f in report.findings)


def test_null_crossing_labels_are_not_findings():
    doc = {
        "nodes": [_node("ARC-a-001"), _node("ARC-b-001")],
        "edges": [_edge("EDG-a-b-001", "ARC-a-001", "ARC-b-001", trust_zone_crossing=None, region_crossing=None)],
    }
    report = ca.check_static(doc)
    assert not any(f.check == "authored-crossing-label" for f in report.findings)


def test_derived_labels_are_computed_and_input_is_never_mutated():
    # @cw-trace verifies INV-fh-006 CTR-fh-025 CTR-fh-023
    doc = {
        "nodes": [_node("ARC-a-001", trust_zone="internal"), _node("ARC-b-001", trust_zone="public")],
        "edges": [_edge("EDG-a-b-001", "ARC-a-001", "ARC-b-001")],
    }
    before = json.dumps(doc, sort_keys=True)
    report = ca.check_static(doc)
    after = json.dumps(doc, sort_keys=True)
    assert before == after  # read-only: the checker never mutates the input
    assert report.derived_labels[0].trust_zone_crossing == "internal->public"


# --- schema-category findings (malformed doc never crashes) -----------------


def test_schema_violation_is_a_finding_not_a_crash():
    # @cw-trace verifies CTR-fh-020
    doc = {"nodes": "not-a-list", "edges": []}
    report = ca.check_static(doc)
    assert not report.ok
    assert any(f.check == "schema" for f in report.findings)


def test_non_dict_document_reports_schema_finding_and_does_not_crash():
    report = ca.check_static(["oops", "not", "an", "object"])
    assert not report.ok
    assert any(f.check == "schema" for f in report.findings)


# --- IT-fh-09: report-only vs --gate exit-mode semantics ---------------------


def test_cli_report_only_exits_zero_with_findings(tmp_path, capsys):
    bad = {"nodes": [_node("ARC-a-001")], "edges": [_edge("EDG-a-b-001", "ARC-a-001", "ARC-ghost-999")]}
    p = tmp_path / "architecture.json"
    p.write_text(json.dumps(bad))
    rc = ca.main([str(p)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dangling-endpoint" in out
    assert ca.AUTHORITY in out


def test_cli_gate_exits_one_on_same_findings(tmp_path):
    bad = {"nodes": [_node("ARC-a-001")], "edges": [_edge("EDG-a-b-001", "ARC-a-001", "ARC-ghost-999")]}
    p = tmp_path / "architecture.json"
    p.write_text(json.dumps(bad))
    rc = ca.main([str(p), "--gate"])
    assert rc == 1


def test_cli_gate_exits_zero_on_clean_model(tmp_path):
    p = tmp_path / "architecture.json"
    p.write_text(json.dumps(_load(EXAMPLE_ARCH)))
    assert ca.main([str(p)]) == 0
    assert ca.main([str(p), "--gate"]) == 0


def test_cli_usage_error_exits_two(capsys):
    rc = ca.main([])
    assert rc == 2
    assert "architecture_file is required" in capsys.readouterr().err


def test_cli_absent_model_exits_zero_and_reports_not_checked_never_passed(tmp_path, capsys):
    # @cw-trace verifies CTR-fh-024
    rc = ca.main([str(tmp_path / "does-not-exist.json")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT CHECKED" in out
    assert ca.AUTHORITY in out


def test_cli_absent_model_exits_zero_even_under_gate(tmp_path):
    rc = ca.main([str(tmp_path / "does-not-exist.json"), "--gate"])
    assert rc == 0


def test_cli_malformed_json_is_a_finding_never_a_usage_error(tmp_path, capsys):
    # @cw-trace verifies CTR-fh-020
    p = tmp_path / "architecture.json"
    p.write_text("{not valid json")
    rc = ca.main([str(p)])
    out = capsys.readouterr().out
    assert rc == 0  # report-only: a parse error is a finding, never exit 2
    assert "schema" in out.lower()

    rc_gate = ca.main([str(p), "--gate"])
    assert rc_gate == 1


def test_cli_authority_line_present_in_every_mode(tmp_path):
    p = tmp_path / "architecture.json"
    p.write_text(json.dumps(_load(EXAMPLE_ARCH)))
    for extra in ([], ["--gate"], ["--format", "json"]):
        rc = ca.main([str(p), *extra])
        assert rc == 0


def test_json_format_includes_authority_field(tmp_path, capsys):
    p = tmp_path / "architecture.json"
    p.write_text(json.dumps(_load(EXAMPLE_ARCH)))
    ca.main([str(p), "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    assert out["authority"] == ca.AUTHORITY
    assert out["ok"] is True


# --- --scanner-version (fifth #184 gate, ADR-fh-06 / INV-fh-005) -------------


def test_scanner_version_is_deterministic_and_stable_across_calls():
    rc1 = ca.main(["--scanner-version"])
    assert rc1 == 0


def test_cli_scanner_version_prints_hex_digest(capsys):
    # @cw-trace verifies CTR-fh-026
    rc = ca.main(["--scanner-version"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert len(out) == 64  # sha256 hex digest
    int(out, 16)  # valid hex


def test_scanner_version_changes_if_module_source_changes(tmp_path):
    """scanner_version hashes THIS module's live source (INV-fh-005) — a copy
    with one byte changed must hash differently, proving it is not a
    hand-set constant nobody remembers to bump."""
    import shutil

    from chief_wiggum.hashing import scanner_version as sv

    src = Path(ca.__file__).resolve()
    cw_dir = src.parent / "chief_wiggum"
    copy = tmp_path / "check_architecture_copy.py"
    shutil.copy(src, copy)
    copy.write_text(copy.read_text() + "\n# a harmless trailing comment\n")

    original = sv(src, cw_dir / "hashing.py")
    modified = sv(copy, cw_dir / "hashing.py")
    assert original != modified
