"""Tests for the instrumentation-coverage checker (#170)."""

from __future__ import annotations

import json

import check_instrumentation as ci


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, (dict, list)):
        p.write_text(json.dumps(content))
    else:
        p.write_text(content)
    return p


# --- @cw-emits regex (chief_wiggum.annotations) ------------------------------


def test_emits_tag_matches_single_binding():
    from chief_wiggum.annotations import EMITS_TAG_RE, split_binding_names

    m = EMITS_TAG_RE.search("# @cw-emits endpointing_latency_ms")
    assert m
    assert split_binding_names(m.group("names")) == ["endpointing_latency_ms"]


def test_emits_tag_matches_dotted_and_slashed_names():
    from chief_wiggum.annotations import EMITS_TAG_RE

    assert EMITS_TAG_RE.search("// @cw-emits llm.ttft")
    assert EMITS_TAG_RE.search("// @cw-emits tts/ttfb")
    assert EMITS_TAG_RE.search("# @cw-emits transport-rtt-ms")


def test_emits_tag_supports_multiple_names_comma_separated():
    from chief_wiggum.annotations import EMITS_TAG_RE, split_binding_names

    m = EMITS_TAG_RE.search("# @cw-emits asr_latency, endpointing_latency_ms")
    assert split_binding_names(m.group("names")) == ["asr_latency", "endpointing_latency_ms"]
    # whitespace around the comma is tolerated
    m = EMITS_TAG_RE.search("# @cw-emits asr_latency ,endpointing_latency_ms")
    assert split_binding_names(m.group("names")) == ["asr_latency", "endpointing_latency_ms"]


def test_emits_tag_ignores_trailing_prose():
    # Codex review of PR #180: a space-separated token list is NOT a multi-
    # binding — the first token is the binding, prose after it is ignored.
    # Otherwise "records", "ASR", and "latency" would become phantom bindings
    # that could accidentally satisfy a missing-binding check.
    from chief_wiggum.annotations import EMITS_TAG_RE, split_binding_names

    m = EMITS_TAG_RE.search("# @cw-emits asr_latency records ASR latency")
    assert m
    assert split_binding_names(m.group("names")) == ["asr_latency"]


def test_prose_word_cannot_satisfy_a_missing_binding(tmp_path):
    # End-to-end: the budget doc binds "records"; a comment whose PROSE contains
    # the word "records" after an @cw-emits tag must NOT count as an emitter.
    budget = _write(
        tmp_path, "system-contracts.json", {"trees": [{"root": _bud_node("BUD-prose-001", "records")}]}
    )
    src = tmp_path / "repo"
    (src / "app").mkdir(parents=True)
    (src / "app" / "handler.py").write_text("# @cw-emits asr_latency records ASR latency\n")
    report = ci.check([budget], src)
    assert not report.ok
    assert report.findings[0].id == "records"
    assert report.emitted_bindings == ["asr_latency"]


def test_writes_and_emits_regex_do_not_cross_match():
    from chief_wiggum.annotations import EMITS_TAG_RE, WRITES_TAG_RE

    assert not EMITS_TAG_RE.search("<!-- @cw-writes INV-x-001 controls_field=a sanctioned_writers=b -->")
    assert not WRITES_TAG_RE.search("# @cw-emits asr_latency")


# --- collect_bindings: schema-agnostic telemetry_ref walk --------------------


def test_collect_bindings_from_budget_tree():
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-voice-001",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 800,
                    "children": [
                        {
                            "id": "BUD-voice-002",
                            "kind": "latency",
                            "unit": "ms",
                            "bound": 300,
                            "telemetry_ref": "asr_latency",
                        }
                    ],
                    "residual": {
                        "id": "BUD-voice-003",
                        "kind": "latency",
                        "unit": "ms",
                        "bound": 100,
                    },
                }
            }
        ]
    }
    bindings = ci.collect_bindings(doc, "system-contracts.json")
    assert len(bindings) == 1
    assert bindings[0].name == "asr_latency"
    assert bindings[0].nodes == ["BUD-voice-002"]


def test_collect_bindings_is_schema_agnostic_future_slo_section():
    # No BUD- schema involved at all — proves the walk isn't budget-tree-specific,
    # so a future SLO-/trace-conformance doc that reuses telemetry_ref is covered
    # without a checker update.
    doc = {"slos": [{"id": "SLO-voice-001", "telemetry_ref": "p95_latency"}]}
    bindings = ci.collect_bindings(doc, "slo.json")
    assert bindings[0].name == "p95_latency"
    assert bindings[0].nodes == ["SLO-voice-001"]


def test_collect_bindings_within_one_doc_merges_same_name_nodes():
    doc = {
        "trees": [
            {
                "root": {
                    "id": "BUD-a-001",
                    "kind": "latency",
                    "unit": "ms",
                    "bound": 1,
                    "children": [_bud_node("BUD-a-002", "shared")],
                    "residual": _bud_node("BUD-a-003", "shared"),
                }
            }
        ]
    }
    bindings = ci.collect_bindings(doc, "a.json")
    assert len(bindings) == 1
    assert set(bindings[0].nodes) == {"BUD-a-002", "BUD-a-003"}


def test_collect_bindings_from_files_merges_across_files(tmp_path):
    p1 = _write(tmp_path, "a.json", {"trees": [{"root": _bud_node("BUD-a-001", "shared")}]})
    p2 = _write(tmp_path, "b.json", {"trees": [{"root": _bud_node("BUD-b-001", "shared")}]})
    bindings, warnings = ci.collect_bindings_from_files([p1, p2])
    assert warnings == []
    assert len(bindings) == 1
    assert set(bindings[0].nodes) == {"BUD-a-001", "BUD-b-001"}


def test_collect_bindings_from_files_warns_on_bad_json(tmp_path):
    p = _write(tmp_path, "bad.json", "{not json")
    bindings, warnings = ci.collect_bindings_from_files([p])
    assert bindings == []
    assert warnings and "bad.json" in warnings[0]


def _bud_node(id_, telemetry_ref):
    return {"id": id_, "kind": "latency", "unit": "ms", "bound": 100, "telemetry_ref": telemetry_ref}


# --- scan_emit_sites: Go/Py/TS fixtures --------------------------------------


def test_scan_finds_emit_site_in_python_comment(tmp_path):
    _write(
        tmp_path,
        "app/pipeline.py",
        "# @cw-emits endpointing_latency_ms\ndef on_endpoint(ts):\n    pass\n",
    )
    sites = ci.scan_emit_sites(tmp_path)
    assert len(sites) == 1
    assert sites[0].name == "endpointing_latency_ms"
    assert sites[0].file == "app/pipeline.py"
    assert sites[0].line == 1


def test_scan_finds_emit_site_in_go_comment(tmp_path):
    _write(
        tmp_path,
        "internal/voice/transport.go",
        "// @cw-emits transport_rtt_ms\nfunc RecordRTT(d time.Duration) {}\n",
    )
    sites = ci.scan_emit_sites(tmp_path)
    assert len(sites) == 1
    assert sites[0].name == "transport_rtt_ms"


def test_scan_finds_emit_site_in_ts_comment(tmp_path):
    _write(
        tmp_path,
        "ui/src/tts.ts",
        "// @cw-emits tts_ttfb_ms\nexport function onTtsFirstByte() {}\n",
    )
    sites = ci.scan_emit_sites(tmp_path)
    assert len(sites) == 1
    assert sites[0].name == "tts_ttfb_ms"


def test_scan_ignores_non_source_extensions(tmp_path):
    _write(tmp_path, "notes.txt", "@cw-emits should_not_count\n")
    sites = ci.scan_emit_sites(tmp_path)
    assert sites == []


def test_scan_respects_exclude_globs(tmp_path):
    _write(tmp_path, "vendor_extra/gen.py", "# @cw-emits excluded_metric\n")
    _write(tmp_path, "app/real.py", "# @cw-emits real_metric\n")
    sites = ci.scan_emit_sites(tmp_path, exclude=["vendor_extra"])
    names = {s.name for s in sites}
    assert names == {"real_metric"}


def test_scan_missing_source_root_returns_empty(tmp_path):
    assert ci.scan_emit_sites(tmp_path / "does-not-exist") == []


# --- check(): missing bindings, gate semantics -------------------------------


def test_check_reports_missing_binding_when_no_emit_site(tmp_path):
    budget = _write(tmp_path, "system-contracts.json", {"trees": [{"root": _bud_node("BUD-x-001", "ghost_metric")}]})
    src = tmp_path / "repo"
    (src / "app").mkdir(parents=True)
    (src / "app" / "handler.py").write_text("def handler():\n    pass\n")  # no @cw-emits at all
    report = ci.check([budget], src)
    assert not report.ok
    assert len(report.findings) == 1
    assert report.findings[0].id == "ghost_metric"
    assert "ghost_metric" in report.findings[0].message


def test_check_passes_when_emit_site_exists(tmp_path):
    budget = _write(tmp_path, "system-contracts.json", {"trees": [{"root": _bud_node("BUD-x-002", "asr_latency")}]})
    src = tmp_path / "repo"
    (src / "app").mkdir(parents=True)
    (src / "app" / "handler.py").write_text("# @cw-emits asr_latency\ndef handler():\n    pass\n")
    report = ci.check([budget], src)
    assert report.ok
    assert report.findings == []
    assert report.emitted_bindings == ["asr_latency"]


def test_check_no_bindings_at_all_is_a_warning_not_a_finding(tmp_path):
    budget = _write(tmp_path, "system-contracts.json", {"trees": [{"root": _bud_node_no_ref("BUD-x-003")}]})
    src = tmp_path / "repo"
    src.mkdir()
    report = ci.check([budget], src)
    assert report.ok
    assert report.bindings == []
    assert any("nothing to check" in w for w in report.warnings)


def _bud_node_no_ref(id_):
    return {"id": id_, "kind": "latency", "unit": "ms", "bound": 100}


def test_check_without_source_degrades_gracefully_no_findings(tmp_path):
    budget = _write(tmp_path, "system-contracts.json", {"trees": [{"root": _bud_node("BUD-x-004", "asr_latency")}]})
    report = ci.check([budget], None)
    assert report.ok  # no repo scan happened -> can't claim "missing"
    assert report.findings == []
    assert any("no --source given" in w for w in report.warnings)


def test_check_missing_budget_file_is_a_warning(tmp_path):
    report = ci.check([tmp_path / "does-not-exist.json"], tmp_path)
    assert report.bindings == []
    assert any("does-not-exist.json" in w for w in report.warnings)


# --- the core fixture: deleting an @cw-emits site flips the binding to missing


def test_deleting_emits_site_flips_binding_to_missing(tmp_path):
    budget = _write(tmp_path, "system-contracts.json", {"trees": [{"root": _bud_node("BUD-x-005", "endpointing_ms")}]})
    src = tmp_path / "repo"
    (src / "app").mkdir(parents=True)
    handler = src / "app" / "handler.py"
    handler.write_text("# @cw-emits endpointing_ms\ndef on_endpoint():\n    pass\n")

    report_before = ci.check([budget], src)
    assert report_before.ok

    # Simulate the "instrumentation deleted" seed class: remove the @cw-emits
    # line while the runtime code is untouched.
    handler.write_text("def on_endpoint():\n    pass\n")

    report_after = ci.check([budget], src)
    assert not report_after.ok
    assert report_after.findings[0].id == "endpointing_ms"


# --- gate semantics -----------------------------------------------------------


def test_cli_default_is_report_only_even_with_missing_binding(tmp_path, capsys):
    budget = _write(tmp_path, "system-contracts.json", {"trees": [{"root": _bud_node("BUD-cli-001", "ghost")}]})
    src = tmp_path / "repo"
    src.mkdir()
    rc = ci.main([str(budget), "--source", str(src)])
    assert rc == 0
    assert "FINDINGS" in capsys.readouterr().out


def test_cli_gate_fails_on_missing_binding(tmp_path):
    budget = _write(tmp_path, "system-contracts.json", {"trees": [{"root": _bud_node("BUD-cli-002", "ghost")}]})
    src = tmp_path / "repo"
    src.mkdir()
    rc = ci.main([str(budget), "--source", str(src), "--gate"])
    assert rc == 1


def test_cli_gate_passes_when_bound(tmp_path):
    budget = _write(tmp_path, "system-contracts.json", {"trees": [{"root": _bud_node("BUD-cli-003", "asr_latency")}]})
    src = tmp_path / "repo"
    (src / "app").mkdir(parents=True)
    (src / "app" / "handler.py").write_text("# @cw-emits asr_latency\n")
    rc = ci.main([str(budget), "--source", str(src), "--gate"])
    assert rc == 0


def test_cli_missing_budget_file_is_usage_error(tmp_path, capsys):
    rc = ci.main([str(tmp_path / "nope.json")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_cli_malformed_budget_json_is_usage_error(tmp_path, capsys):
    # Codex review of PR #180: a corrupt contract file must NOT silently
    # degrade to "nothing to check" (exit 0) — that disables the gate while
    # appearing green. Unreadable/malformed budget docs are a usage error.
    bad = _write(tmp_path, "system-contracts.json", "{not json")
    rc = ci.main([str(bad), "--source", str(tmp_path)])
    assert rc == 2
    assert "cannot load budget file" in capsys.readouterr().err


def test_cli_malformed_budget_json_is_usage_error_even_with_gate(tmp_path, capsys):
    bad = _write(tmp_path, "system-contracts.json", "{not json")
    rc = ci.main([str(bad), "--source", str(tmp_path), "--gate"])
    assert rc == 2
    assert "cannot load budget file" in capsys.readouterr().err


def test_cli_one_malformed_file_among_many_is_usage_error(tmp_path):
    good = _write(tmp_path, "good.json", {"trees": [{"root": _bud_node("BUD-mix-001", "m1")}]})
    bad = _write(tmp_path, "bad.json", "{not json")
    rc = ci.main([str(good), str(bad), "--source", str(tmp_path)])
    assert rc == 2


def test_cli_json_format_includes_authority_and_counts(tmp_path, capsys):
    budget = _write(tmp_path, "system-contracts.json", {"trees": [{"root": _bud_node("BUD-cli-004", "asr_latency")}]})
    src = tmp_path / "repo"
    (src / "app").mkdir(parents=True)
    (src / "app" / "handler.py").write_text("# @cw-emits asr_latency\n")
    rc = ci.main([str(budget), "--source", str(src), "--format", "json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "authority" in data
    assert data["counts"]["bindings"] == 1
    assert data["emitted_bindings"] == ["asr_latency"]


# --- report shape -------------------------------------------------------------


def test_report_json_serializable(tmp_path):
    budget = _write(tmp_path, "system-contracts.json", {"trees": [{"root": _bud_node("BUD-json-001", "m1")}]})
    src = tmp_path / "repo"
    (src / "app").mkdir(parents=True)
    (src / "app" / "handler.py").write_text("# @cw-emits m1\n")
    report = ci.check([budget], src)
    json.dumps(report.to_dict())  # must not raise


def test_render_text_includes_authority_line(tmp_path):
    budget = _write(tmp_path, "system-contracts.json", {"trees": [{"root": _bud_node("BUD-txt-001", "m1")}]})
    report = ci.check([budget], tmp_path / "repo")
    text = ci.render_text(report)
    assert "Authority:" in text
    assert report.authority in text
