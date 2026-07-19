"""Tests for the budget tree checker (#164)."""

from __future__ import annotations

import json

import check_budget_tree as cbt


def _leaf(id_, bound, alpha=None, telemetry_ref=None, **extra):
    node = {"id": id_, "kind": "latency", "unit": "ms", "bound": bound}
    if alpha is not None:
        node["alpha"] = alpha
    if telemetry_ref is not None:
        node["telemetry_ref"] = telemetry_ref
    node.update(extra)
    return node


def _residual(id_, bound, alpha=None):
    return _leaf(id_, bound, alpha)


# --- union-bound arithmetic ---------------------------------------------------


def test_union_bound_consistent_tree_has_no_findings():
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-voice-001",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 800,
                    "alpha": 0.05,
                    "children": [
                        _leaf("BUD-voice-002", 300, 0.02),
                        _leaf("BUD-voice-003", 300, 0.02),
                    ],
                    "residual": _residual("BUD-voice-004", 150, 0.01),
                }
            }
        ]
    }
    report = cbt.check_static(doc)
    assert report.ok
    assert report.findings == []


def test_union_bound_alpha_oversubscribed_is_arithmetic_finding():
    # sum(bound) fits (300+300+150=750<=800) but sum(alpha) does NOT (0.03+0.03+0.03=0.09 > 0.05)
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-x-001",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 800,
                    "alpha": 0.05,
                    "children": [
                        _leaf("BUD-x-002", 300, 0.03),
                        _leaf("BUD-x-003", 300, 0.03),
                    ],
                    "residual": _residual("BUD-x-004", 150, 0.03),
                }
            }
        ]
    }
    report = cbt.check_static(doc)
    assert not report.ok
    assert any(f.category == "arithmetic" and "alpha" in f.message for f in report.findings)


def test_union_bound_bound_oversubscribed_is_arithmetic_finding():
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-y-001",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 500,
                    "alpha": 0.1,
                    "children": [
                        _leaf("BUD-y-002", 300, 0.02),
                        _leaf("BUD-y-003", 300, 0.02),
                    ],
                    "residual": _residual("BUD-y-004", 50, 0.02),
                }
            }
        ]
    }
    report = cbt.check_static(doc)
    assert not report.ok
    assert any(f.category == "arithmetic" and "bound" in f.message.lower() for f in report.findings)


def test_headroom_counts_against_parent_bound():
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-h-001",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 700,
                    "alpha": 0.1,
                    "headroom": 200,
                    "children": [_leaf("BUD-h-002", 300, 0.03)],
                    "residual": _residual("BUD-h-003", 150, 0.03),
                }
            }
        ]
    }
    # 300 + 150 + headroom(200) = 650 <= 700 -> fine
    report = cbt.check_static(doc)
    assert report.ok

    doc["trees"][0]["root"]["headroom"] = 300
    # 300 + 150 + headroom(300) = 750 > 700 -> violation
    report2 = cbt.check_static(doc)
    assert not report2.ok


# --- the correlated-tails counterexample (refuter finding #1) ----------------


def test_correlated_tails_counterexample_naive_passes_union_bound_flags():
    """Two children each p95=300ms, parent bound=700ms.

    Naive sum-of-bounds (300+300=600 <= 700) PASSES and looks safe. But each
    child's own tail-probability budget (alpha=0.05) sums to 0.10 against a
    parent alpha of 0.05 — the union bound shows the parent's own tail
    guarantee is oversubscribed, something the naive sum can never see because
    percentiles do not sum. This is the load-bearing counterexample from the
    issue: naive arithmetic is unsound for correlated tails.
    """
    root = {
        "id": "BUD-tail-001",
        "kind": "latency",
        "unit": "ms",
        "bound": 700,
        "alpha": 0.05,
        "children": [
            _leaf("BUD-tail-002", 300, 0.05),
            _leaf("BUD-tail-003", 300, 0.05),
        ],
        "residual": _residual("BUD-tail-004", 0, 0.0),
    }

    naive_doc = {"trees": [{"root": root, "arithmetic": "naive"}]}
    naive_report = cbt.check_static(naive_doc)
    assert naive_report.ok  # naive mode never gates -> reports as ok
    assert any(
        w.category == "naive-arithmetic" and "would PASS" in w.message for w in naive_report.warnings
    )

    union_doc = {"trees": [{"root": root, "arithmetic": "union-bound"}]}
    union_report = cbt.check_static(union_doc)
    assert not union_report.ok
    assert any(f.category == "arithmetic" and "alpha" in f.message for f in union_report.findings)


def test_naive_mode_warning_is_never_a_finding():
    doc = {
        "trees": [
            {
                "arithmetic": "naive",
                "root": {
                    "id": "BUD-n-001",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 100,
                    "children": [_leaf("BUD-n-002", 200)],
                    "residual": _residual("BUD-n-003", 0),
                },
            }
        ]
    }
    report = cbt.check_static(doc)
    # naive would-FAIL is reported, but only as a warning, never a finding, never blocking
    assert report.ok
    assert report.findings == []
    assert any("would FAIL" in w.message for w in report.warnings)


# --- residual enforcement -----------------------------------------------------


def test_missing_residual_on_nonleaf_is_structure_finding():
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-r-001",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 800,
                    "alpha": 0.05,
                    "children": [_leaf("BUD-r-002", 300, 0.02)],
                    # no residual!
                }
            }
        ]
    }
    report = cbt.check_static(doc)
    assert not report.ok
    assert any(f.category == "structure" and "residual" in f.message for f in report.findings)


def test_leaf_node_does_not_require_residual():
    doc = {
        "trees": [
            {
                "root": _leaf("BUD-l-001", 100, 0.05),
            }
        ]
    }
    report = cbt.check_static(doc)
    assert report.ok
    assert report.findings == []


# --- missing alpha must not silently degrade union-bound to a naive sum ------


def test_union_bound_missing_child_alpha_is_structure_finding():
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-ma-001",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 800,
                    "alpha": 0.05,
                    "children": [
                        _leaf("BUD-ma-002", 300, 0.02),
                        _leaf("BUD-ma-003", 300),  # NO alpha
                    ],
                    "residual": _residual("BUD-ma-004", 100, 0.01),
                }
            }
        ]
    }
    report = cbt.check_static(doc)
    assert not report.ok
    assert any(
        f.category == "structure" and "missing alpha" in f.message and "BUD-ma-003" in f.message
        for f in report.findings
    )


def test_union_bound_missing_parent_alpha_is_structure_finding():
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-ma-005",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 800,
                    # NO alpha on a non-leaf union-bound node
                    "children": [_leaf("BUD-ma-006", 300, 0.02)],
                    "residual": _residual("BUD-ma-007", 100, 0.01),
                }
            }
        ]
    }
    report = cbt.check_static(doc)
    assert not report.ok
    assert any(
        f.category == "structure" and "no alpha" in f.message and "BUD-ma-005" in f.message
        for f in report.findings
    )


def test_naive_mode_does_not_require_alphas():
    # Alphas are unused by naive arithmetic; their absence is not a finding there
    # (the mode itself already carries the unsound-and-never-gateable warning).
    doc = {
        "trees": [
            {
                "arithmetic": "naive",
                "root": {
                    "id": "BUD-ma-008",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 800,
                    "children": [_leaf("BUD-ma-009", 300)],
                    "residual": _residual("BUD-ma-010", 100),
                },
            }
        ]
    }
    report = cbt.check_static(doc)
    assert report.ok
    assert not any("alpha" in f.message for f in report.findings)


# --- kind/unit compatibility --------------------------------------------------


def test_mixed_unit_child_is_structure_finding():
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-mix-001",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 800,
                    "alpha": 0.05,
                    "children": [
                        _leaf("BUD-mix-002", 300, 0.02),
                        {"id": "BUD-mix-003", "kind": "latency", "unit": "s", "bound": 0.3, "alpha": 0.02},
                    ],
                    "residual": _residual("BUD-mix-004", 100, 0.01),
                }
            }
        ]
    }
    report = cbt.check_static(doc)
    assert not report.ok
    assert any(
        f.category == "structure" and "incompatible" in f.message and "BUD-mix-003" in f.message
        for f in report.findings
    )


def test_mixed_kind_child_is_structure_finding_and_arithmetic_is_skipped():
    # A ms parent must not sum usd children — and the meaningless mixed sum must
    # not produce a bogus arithmetic finding on top of the structure finding.
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-mix-005",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 100,
                    "alpha": 0.05,
                    "children": [
                        {"id": "BUD-mix-006", "kind": "spend", "unit": "usd", "bound": 5000, "alpha": 0.02},
                    ],
                    "residual": _residual("BUD-mix-007", 10, 0.01),
                }
            }
        ]
    }
    report = cbt.check_static(doc)
    assert not report.ok
    assert any(f.category == "structure" and "incompatible" in f.message for f in report.findings)
    assert not any(f.category == "arithmetic" for f in report.findings)


def test_matching_kind_and_unit_produces_no_compatibility_finding():
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-mix-008",
                    "kind": "spend",
                    "unit": "usd/day",
                    "bound": 100,
                    "alpha": 0.05,
                    "children": [{"id": "BUD-mix-009", "kind": "spend", "unit": "usd/day", "bound": 60, "alpha": 0.02}],
                    "residual": {"id": "BUD-mix-010", "kind": "spend", "unit": "usd/day", "bound": 30, "alpha": 0.02},
                }
            }
        ]
    }
    report = cbt.check_static(doc)
    assert report.ok


# --- schema enforcement --------------------------------------------------------


def test_schema_unknown_field_is_finding():
    doc = {"trees": [{"root": _leaf("BUD-sch-001", 100, 0.05, bogus_field=True)}]}
    report = cbt.check_static(doc)
    assert not report.ok
    assert any(f.category == "schema" and "bogus_field" in f.message for f in report.findings)


def test_schema_invalid_arithmetic_enum_is_finding():
    doc = {"trees": [{"arithmetic": "vibes", "root": _leaf("BUD-sch-002", 100, 0.05)}]}
    report = cbt.check_static(doc)
    assert not report.ok
    assert any(f.category == "schema" and "arithmetic" in f.id for f in report.findings)


def test_schema_bad_bud_id_pattern_is_finding():
    doc = {"trees": [{"root": _leaf("BUDGET-001", 100, 0.05)}]}
    report = cbt.check_static(doc)
    assert not report.ok
    assert any(f.category == "schema" and "pattern" in f.message for f in report.findings)


def test_schema_alpha_out_of_range_is_finding():
    doc = {"trees": [{"root": _leaf("BUD-sch-003", 100, 1.5)}]}
    report = cbt.check_static(doc)
    assert not report.ok
    assert any(f.category == "schema" and "maximum" in f.message for f in report.findings)


def test_schema_missing_required_asm_evidence_is_finding():
    doc = {
        "trees": [
            {"root": _leaf("BUD-sch-004", 100, 0.05, asm_refs=[{"id": "ASM-sch-001", "ref": "somewhere"}])}
        ]
    }
    report = cbt.check_static(doc)
    assert not report.ok
    assert any(
        f.category == "schema" and "evidence" in f.message and "asm_refs" in f.id for f in report.findings
    )


def test_schema_missing_required_node_fields_is_finding():
    doc = {"trees": [{"root": {"id": "BUD-sch-005"}}]}  # no kind/unit/bound
    report = cbt.check_static(doc)
    assert not report.ok
    msgs = [f.message for f in report.findings if f.category == "schema"]
    assert any("kind" in m for m in msgs)
    assert any("unit" in m for m in msgs)
    assert any("bound" in m for m in msgs)


def test_schema_valid_doc_has_no_schema_findings():
    doc = {
        "trees": [
            {
                "arithmetic": "union-bound",
                "root": {
                    "id": "BUD-sch-006",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 800,
                    "alpha": 0.05,
                    "telemetry_ref": "e2e_latency",
                    "children": [
                        _leaf(
                            "BUD-sch-007",
                            300,
                            0.02,
                            asm_refs=[{"id": "ASM-sch-002", "evidence": "sla-doc", "ref": "https://x.example/sla"}],
                        )
                    ],
                    "residual": _residual("BUD-sch-008", 100, 0.01),
                },
            }
        ],
        "chains": [{"id": "c1", "hops": [{"caller": "a", "callee": "b", "timeout_ms": 100}]}],
    }
    report = cbt.check_static(doc)
    assert not any(f.category == "schema" for f in report.findings)
    assert report.ok


def test_schema_findings_are_gateable_in_static_mode(tmp_path):
    doc = {"trees": [{"root": {"id": "BUD-sch-009", "kind": "latency", "unit": "ms", "bound": 100, "oops": 1}}]}
    p = tmp_path / "budget.json"
    p.write_text(json.dumps(doc))
    assert cbt.main([str(p)]) == 0  # report-only by default
    assert cbt.main([str(p), "--gate"]) == 1  # schema findings gate like structure findings


# --- timeout monotonicity -----------------------------------------------------


def test_monotonic_chain_has_no_violation_warning():
    doc = {
        "trees": [{"root": _leaf("BUD-m-001", 100)}],
        "chains": [
            {
                "id": "chain-1",
                "hops": [
                    {"caller": "gateway", "callee": "asr", "timeout_ms": 500},
                    {"caller": "asr", "callee": "llm", "timeout_ms": 300},
                    {"caller": "llm", "callee": "tts", "timeout_ms": 150},
                ],
            }
        ],
    }
    report = cbt.check_static(doc)
    assert report.ok
    violations = [w for w in report.warnings if w.category == "monotonicity" and "not greater than" in w.message]
    assert violations == []


def test_nonmonotonic_chain_is_warning_not_finding():
    doc = {
        "trees": [{"root": _leaf("BUD-m2-001", 100)}],
        "chains": [
            {
                "id": "chain-2",
                "hops": [
                    {"caller": "gateway", "callee": "asr", "timeout_ms": 200},
                    {"caller": "asr", "callee": "llm", "timeout_ms": 300},  # violation: 200 !> 300
                ],
            }
        ],
    }
    report = cbt.check_static(doc)
    assert report.ok  # WARN only, never gateable
    assert report.findings == []
    violations = [w for w in report.warnings if w.category == "monotonicity" and "not greater than" in w.message]
    assert len(violations) == 1
    assert "retries/hedging" in violations[0].message


# --- ASM evidence statuses -----------------------------------------------------


def test_asm_ref_sla_doc_is_covered():
    node = _leaf("BUD-a-001", 100, asm_refs=[{"id": "ASM-vendor-001", "evidence": "sla-doc", "ref": "https://vendor.example/sla"}])
    doc = {"trees": [{"root": node}]}
    report = cbt.check_static(doc)
    assert report.ok
    assert report.asm_statuses[0].status == "covered"


def test_asm_ref_live_probe_is_covered():
    node = _leaf("BUD-a-002", 100, asm_refs=[{"id": "ASM-vendor-002", "evidence": "live-probe", "ref": "probes/vendor.py"}])
    report = cbt.check_static({"trees": [{"root": node}]})
    assert report.asm_statuses[0].status == "covered"


def test_asm_ref_justified_renders_as_waiver_not_covered():
    node = _leaf(
        "BUD-a-003", 100, asm_refs=[{"id": "ASM-vendor-003", "evidence": "justified", "ref": "no SLA published; accepted risk per ADR-12"}]
    )
    report = cbt.check_static({"trees": [{"root": node}]})
    assert report.ok
    assert report.asm_statuses[0].status == "waived"
    assert report.asm_statuses[0].status != "covered"


def test_asm_ref_missing_evidence_is_finding():
    node = _leaf("BUD-a-004", 100, asm_refs=[{"id": "ASM-vendor-004", "ref": "somewhere"}])
    report = cbt.check_static({"trees": [{"root": node}]})
    assert not report.ok
    assert any(f.category == "structure" and "ASM-vendor-004" in f.message for f in report.findings)
    assert report.asm_statuses[0].status == "missing"


def test_asm_ref_invalid_evidence_value_is_finding():
    node = _leaf("BUD-a-005", 100, asm_refs=[{"id": "ASM-vendor-005", "evidence": "vibes", "ref": "somewhere"}])
    report = cbt.check_static({"trees": [{"root": node}]})
    assert not report.ok
    assert report.asm_statuses[0].status == "missing"


def test_asm_ref_missing_ref_is_finding():
    node = _leaf("BUD-a-006", 100, asm_refs=[{"id": "ASM-vendor-006", "evidence": "sla-doc"}])
    report = cbt.check_static({"trees": [{"root": node}]})
    assert not report.ok


# --- measured mode: binding statuses ------------------------------------------


def test_measured_held_when_observation_within_bound():
    doc = {"trees": [{"root": _leaf("BUD-meas-001", 300, telemetry_ref="asr_latency")}]}
    measured = {"asr_latency": {"p95": 250, "count": 500}}
    report = cbt.check_measured(doc, measured, source="k6-summary.json")
    assert report.ok  # measured mode never gates
    assert report.measured[0].status == "held"


def test_measured_breached_when_observation_exceeds_bound():
    doc = {"trees": [{"root": _leaf("BUD-meas-002", 300, telemetry_ref="asr_latency")}]}
    measured = {"asr_latency": {"p95": 450, "count": 500}}
    report = cbt.check_measured(doc, measured, source="k6-summary.json")
    assert report.ok  # still never gates, evidence-only
    assert report.measured[0].status == "breached"
    assert report.counts["measured_breached"] == 1


def test_measured_no_observations_when_zero_count_is_a_finding_signal():
    doc = {"trees": [{"root": _leaf("BUD-meas-003", 300, telemetry_ref="asr_latency")}]}
    measured = {"asr_latency": {"p95": None, "count": 0}}
    report = cbt.check_measured(doc, measured, source="k6-summary.json")
    assert report.measured[0].status == "no_observations"
    assert report.counts["measured_no_observations"] == 1
    # zero observations is never reported as a pass
    assert report.measured[0].status != "held"


def test_measured_no_observations_when_metric_absent_from_export():
    doc = {"trees": [{"root": _leaf("BUD-meas-004", 300, telemetry_ref="nonexistent_metric")}]}
    report = cbt.check_measured(doc, {}, source="k6-summary.json")
    assert report.measured[0].status == "no_observations"


def test_measured_unbound_when_node_has_no_telemetry_ref():
    # No telemetry_ref = the node is not bound to any metric: "unbound" — a spec
    # gap, DISTINCT from "no_observations" (a declared binding with no data — a
    # measurement gap). Neither is ever a pass.
    doc = {"trees": [{"root": _leaf("BUD-meas-005", 300)}]}  # no telemetry_ref at all
    report = cbt.check_measured(doc, {"asr_latency": {"p95": 100, "count": 10}}, source="k6-summary.json")
    assert report.measured[0].status == "unbound"
    assert report.counts["measured_unbound"] == 1


def test_measured_all_four_statuses_are_distinct():
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-meas-010",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 800,
                    "children": [
                        _leaf("BUD-meas-011", 300, telemetry_ref="m_held"),
                        _leaf("BUD-meas-012", 300, telemetry_ref="m_breached"),
                        _leaf("BUD-meas-013", 300, telemetry_ref="m_nodata"),
                    ],
                    "residual": _residual("BUD-meas-014", 100),  # no telemetry_ref
                }
            }
        ]
    }
    measured = {
        "m_held": {"p95": 250, "count": 100},
        "m_breached": {"p95": 999, "count": 100},
        "m_nodata": {"p95": None, "count": 0},
    }
    report = cbt.check_measured(doc, measured, source="k6-summary.json")
    statuses = {m.id: m.status for m in report.measured}
    assert statuses["BUD-meas-011"] == "held"
    assert statuses["BUD-meas-012"] == "breached"
    assert statuses["BUD-meas-013"] == "no_observations"
    assert statuses["BUD-meas-014"] == "unbound"  # residual: no binding declared
    assert statuses["BUD-meas-010"] == "unbound"  # root: no binding declared
    assert report.ok  # measured mode is evidence-only, always ok


def test_measured_recurses_into_children_and_residual():
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-meas-006",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 800,
                    "children": [_leaf("BUD-meas-007", 300, telemetry_ref="child_metric")],
                    "residual": _residual("BUD-meas-008", 100),
                }
            }
        ]
    }
    measured = {"child_metric": {"p95": 250, "count": 10}}
    report = cbt.check_measured(doc, measured, source="k6-summary.json")
    ids = {m.id: m.status for m in report.measured}
    assert ids["BUD-meas-006"] == "unbound"  # root has no telemetry_ref
    assert ids["BUD-meas-007"] == "held"
    assert ids["BUD-meas-008"] == "unbound"  # residual has no telemetry_ref


# --- load_measured: k6 summary + flat export shapes ---------------------------


def test_load_measured_flat_export(tmp_path):
    p = tmp_path / "flat.json"
    p.write_text(json.dumps({"asr_latency": {"p95": 250, "count": 10}}))
    data = cbt.load_measured(p)
    assert data["asr_latency"] == {"p95": 250, "count": 10}


def test_load_measured_k6_summary_export(tmp_path):
    p = tmp_path / "k6-summary.json"
    p.write_text(
        json.dumps(
            {
                "metrics": {
                    "http_req_duration": {"values": {"p(95)": 320.5, "count": 1000}},
                    "unused_metric": {"values": {"count": 0}},
                }
            }
        )
    )
    data = cbt.load_measured(p)
    assert data["http_req_duration"]["p95"] == 320.5
    assert data["http_req_duration"]["count"] == 1000
    assert data["unused_metric"]["count"] == 0
    assert data["unused_metric"]["p95"] is None


# --- authority line ------------------------------------------------------------


def test_static_authority_line():
    report = cbt.check_static({"trees": [{"root": _leaf("BUD-auth-001", 100)}]})
    assert report.authority == "static mode proves budget-declaration consistency, not runtime latency"
    assert "authority" in report.to_dict()


def test_measured_authority_line_includes_source():
    report = cbt.check_measured({"trees": [{"root": _leaf("BUD-auth-002", 100)}]}, {}, source="k6-summary.json")
    assert report.authority == "measured mode reports observations from k6-summary.json; not a proof of runtime behaviour"


def test_render_text_includes_authority_line():
    report = cbt.check_static({"trees": [{"root": _leaf("BUD-auth-003", 100)}]})
    text = cbt.render_text(report)
    assert "Authority:" in text
    assert report.authority in text


# --- report shape: counts/ok/to_dict -------------------------------------------


def test_report_json_serializable():
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-json-001",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 800,
                    "alpha": 0.05,
                    "children": [_leaf("BUD-json-002", 300, 0.02)],
                    "residual": _residual("BUD-json-003", 150, 0.01),
                }
            }
        ]
    }
    report = cbt.check_static(doc)
    json.dumps(report.to_dict())  # must not raise


# --- CLI: exit codes + gating ---------------------------------------------------


def _write(tmp_path, name, doc):
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return p


def test_cli_static_default_is_report_only_even_with_findings(tmp_path, capsys):
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-cli-001",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 800,
                    "children": [_leaf("BUD-cli-002", 300)],
                    # missing residual -> a finding
                }
            }
        ]
    }
    p = _write(tmp_path, "budget.json", doc)
    rc = cbt.main([str(p)])
    assert rc == 0  # report-only by default even with findings present
    assert "FINDINGS" in capsys.readouterr().out


def test_cli_static_gate_fails_on_findings(tmp_path, capsys):
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-cli-003",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 800,
                    "children": [_leaf("BUD-cli-004", 300)],
                }
            }
        ]
    }
    p = _write(tmp_path, "budget.json", doc)
    rc = cbt.main([str(p), "--gate"])
    assert rc == 1


def test_cli_static_gate_passes_clean_tree(tmp_path):
    doc = {"trees": [{"root": _leaf("BUD-cli-005", 100, 0.05)}]}
    p = _write(tmp_path, "budget.json", doc)
    rc = cbt.main([str(p), "--gate"])
    assert rc == 0


def test_cli_measured_never_exits_nonzero_even_with_gate(tmp_path, capsys):
    doc = {"trees": [{"root": _leaf("BUD-cli-006", 100, telemetry_ref="m1")}]}
    budget_path = _write(tmp_path, "budget.json", doc)
    measured_path = _write(tmp_path, "measured.json", {"m1": {"p95": 500, "count": 5}})  # breaches bound
    rc = cbt.main([str(budget_path), "--measured", str(measured_path), "--gate"])
    assert rc == 0  # measured mode is evidence-only, permanently
    out = capsys.readouterr().out
    assert "breached" in out


def test_cli_measured_with_schema_findings_still_exits_zero_even_with_gate(tmp_path):
    # Even a schema-invalid document cannot make measured mode exit non-zero:
    # findings are reported as evidence, --gate has no effect.
    doc = {"trees": [{"root": _leaf("BUD-cli-008", 100, telemetry_ref="m1", bogus_field=True)}]}
    budget_path = _write(tmp_path, "budget.json", doc)
    measured_path = _write(tmp_path, "measured.json", {"m1": {"p95": 50, "count": 5}})
    rc = cbt.main([str(budget_path), "--measured", str(measured_path), "--gate"])
    assert rc == 0


def test_cli_json_format_includes_authority_and_counts(tmp_path, capsys):
    doc = {"trees": [{"root": _leaf("BUD-cli-007", 100, 0.05)}]}
    p = _write(tmp_path, "budget.json", doc)
    rc = cbt.main([str(p), "--format", "json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["authority"] == cbt.STATIC_AUTHORITY
    assert "counts" in data and "findings" in data["counts"]


def test_cli_missing_file_is_usage_error(tmp_path, capsys):
    rc = cbt.main([str(tmp_path / "nope.json")])
    assert rc == 2
    assert "Error" in capsys.readouterr().err


def test_cli_malformed_json_is_usage_error(tmp_path, capsys):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    rc = cbt.main([str(p)])
    assert rc == 2
