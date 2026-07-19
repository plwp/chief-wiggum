"""Tests for the traceability graph checker (#36)."""

from __future__ import annotations

import json

import check_traceability as ct

SCHEMA = ct.load_schema()


# --- annotation grammar -----------------------------------------------------


def test_parse_single_annotation():
    assert ct.parse_annotations("# @cw-trace guards CTR-order-001") == [("guards", ["CTR-order-001"])]


def test_parse_multiple_ids():
    out = ct.parse_annotations("// @cw-trace ensures CTR-order-001 INV-order-003")
    assert out == [("ensures", ["CTR-order-001", "INV-order-003"])]


def test_namespaced_tag_avoids_collisions():
    # A bare verb (no @cw-trace) must NOT match — avoids JSDoc/decorator collisions.
    assert ct.parse_annotations("@ensures CTR-order-001 (jsdoc-ish)") == []
    assert ct.parse_annotations('@pytest.mark.contract("CTR-order-001")') == []


def test_parse_ignores_malformed_ids():
    assert ct.parse_annotations("@cw-trace guards CTR-order-1") == []  # not 3 digits


def test_suffixed_id_is_not_accepted():
    # CTR-order-001oops must not be parsed as CTR-order-001.
    assert ct.parse_annotations("@cw-trace guards CTR-order-001oops") == []


# --- defined-id extraction --------------------------------------------------


def test_extract_ids_from_markdown_and_json(tmp_path):
    epic = tmp_path / "epic"
    (epic / "models").mkdir(parents=True)
    (epic / "contracts.md").write_text("### CTR-order-001 — valid range\n- realizes BR-order-001\n")
    (epic / "invariants.md").write_text("- **INV-order-003**: status never regresses\n")
    (epic / "models" / "contracts.json").write_text(json.dumps({"id": "CTR-order-002"}))
    defined = ct.extract_defined_ids(epic)
    assert defined["CTR-order-001"] == "CTR"
    assert defined["INV-order-003"] == "INV"
    assert defined["CTR-order-002"] == "CTR"


# --- source scan: code vs test ----------------------------------------------


def test_scan_source_classifies_code_vs_test(tmp_path):
    (tmp_path / "order.py").write_text("# @cw-trace guards CTR-order-001\n")
    (tmp_path / "test_order.py").write_text("# @cw-trace verifies CTR-order-001\n")
    anns = ct.scan_source(tmp_path)
    kinds = {(a.verb, a.source_kind) for a in anns}
    assert ("guards", "code") in kinds
    assert ("verifies", "test") in kinds


# --- report: the four findings ----------------------------------------------


def _report(defined, annotations):
    return ct.build_report(defined, annotations, SCHEMA)


def _ann(verb, target, kind):
    return ct.Annotation(verb, target, "f", 1, kind)


def test_orphan_business_rule():
    r = _report({"BR-x-001": "BR", "CTR-x-001": "CTR"}, [_ann("realizes", "BR-x-001", "code")])
    # realizes must come from CTR/INV, not code -> invalid link AND BR stays orphan
    assert "BR-x-001" in r.orphan_business_rules


def test_business_rule_realized_is_not_orphan():
    # CTR.md realizing the BR: realizes originates from CTR (a defined-doc concept).
    # In code annotations, realizes is code->BR which is invalid; we model realizes
    # via the contract doc using a CTR source — represent that with source_kind CTR.
    r = _report({"BR-x-001": "BR", "CTR-x-001": "CTR"},
                [ct.Annotation("realizes", "BR-x-001", "f", 1, "CTR", source_id="CTR-x-001")])
    assert r.orphan_business_rules == []


def test_uncovered_and_untested_contract():
    r = _report({"CTR-x-001": "CTR"}, [])
    assert r.uncovered_contracts == ["CTR-x-001"]
    assert r.untested_contracts == ["CTR-x-001"]


def test_covered_and_tested_contract():
    anns = [_ann("guards", "CTR-x-001", "code"), _ann("verifies", "CTR-x-001", "test")]
    r = _report({"CTR-x-001": "CTR"}, anns)
    assert r.uncovered_contracts == [] and r.untested_contracts == []


def test_dangling_annotation():
    r = _report({"CTR-x-001": "CTR"}, [_ann("guards", "CTR-ghost-999", "code")])
    assert r.dangling and r.dangling[0]["target"] == "CTR-ghost-999"


def test_invalid_link_verb_node_mismatch():
    # 'verifies' from code (should be test) is an invalid link per the TIM schema.
    r = _report({"CTR-x-001": "CTR"}, [_ann("verifies", "CTR-x-001", "code")])
    assert r.invalid_links and "cannot originate from code" in r.invalid_links[0]["reason"]


# --- gates + graceful -------------------------------------------------------


def test_soundness_and_coverage_flags():
    clean = _report({"BR-x-001": "BR", "CTR-x-001": "CTR"}, [
        ct.Annotation("realizes", "BR-x-001", "f", 1, "CTR", source_id="CTR-x-001"),
        _ann("guards", "CTR-x-001", "code"),
        _ann("verifies", "CTR-x-001", "test"),
    ])
    assert clean.soundness_ok and clean.coverage_ok


def test_realizes_link_from_epic_docs_clears_orphan(tmp_path):
    # End-to-end: a contract doc declaring `@cw-trace realizes BR-x-001` marks the
    # BR realized (not orphan), and the source provides guard/test coverage.
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text(
        "### CTR-x-001 — valid range\n<!-- @cw-trace realizes BR-x-001 -->\n"
    )
    (epic / "invariants.md").write_text("- **BR-x-001**: orders must have a positive total\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "order.py").write_text("# @cw-trace guards CTR-x-001\n")
    (src / "test_order.py").write_text("# @cw-trace verifies CTR-x-001\n")
    r = ct.check(epic, src)
    assert r.orphan_business_rules == []
    assert r.uncovered_contracts == [] and r.untested_contracts == []
    assert r.soundness_ok and r.coverage_ok


def test_stray_realizes_without_contract_source_does_not_clear_orphan(tmp_path):
    # A realizes line with no contract/invariant declared above it must not clear
    # the orphan, and is flagged as an invalid link.
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "rules.md").write_text("**BR-x-001**: x\n<!-- @cw-trace realizes BR-x-001 -->\n")
    r = ct.check(epic)
    assert "BR-x-001" in r.orphan_business_rules
    assert any("no declaring contract" in d.get("reason", "") for d in r.invalid_links)


def test_markdown_docs_not_scanned_as_source(tmp_path):
    # A .md file under the source root (e.g. docs with @cw-trace EXAMPLES) must not
    # be treated as code annotations -> no false dangling/invalid links.
    src = tmp_path / "src"
    src.mkdir()
    (src / "guide.md").write_text("Example: `# @cw-trace guards CTR-ghost-001`\n")
    assert ct.scan_source(src) == []


def test_cli_missing_epic_dir_is_usage_error(tmp_path, capsys):
    rc = ct.main([str(tmp_path / "nope")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_graceful_when_no_annotations():
    r = ct.check("/nonexistent/epic")
    assert r.warnings  # reports absence, does not crash
    assert r.soundness_ok  # nothing defined -> no orphans/dangling


def test_report_json_serializable():
    r = _report({"CTR-x-001": "CTR"}, [])
    json.loads(json.dumps(r.to_dict()))


# --- CLI --------------------------------------------------------------------


def _write_epic(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("### CTR-order-001 — x\n")
    return epic


def test_cli_coverage_gate_fails_on_untested(tmp_path, capsys):
    epic = _write_epic(tmp_path)
    rc = ct.main([str(epic), "--gate", "coverage", "--format", "json"])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert "CTR-order-001" in data["untested_contracts"]


def test_cli_soundness_gate_passes_without_orphans(tmp_path, capsys):
    epic = _write_epic(tmp_path)
    rc = ct.main([str(epic), "--gate", "soundness"])
    assert rc == 0


def test_cli_text_output(tmp_path, capsys):
    epic = _write_epic(tmp_path)
    rc = ct.main([str(epic)])
    assert rc == 0
    assert "# Traceability Audit" in capsys.readouterr().out


# --- emission/claim seam (#160) ----------------------------------------------


def test_emit_source_annotations_is_pure_function_of_text():
    anns = ct.emit_source_annotations("order.py", "# @cw-trace guards CTR-order-001\n", ".py")
    assert len(anns) == 1
    a = anns[0]
    assert a.verb == "guards" and a.target == "CTR-order-001"
    assert a.file == "order.py" and a.line == 1 and a.source_kind == "code"


def test_emit_source_annotations_classifies_test_kind():
    anns = ct.emit_source_annotations("test_order.py", "# @cw-trace verifies CTR-order-001\n", ".py")
    assert anns[0].source_kind == "test"


def test_emit_epic_annotations_attributes_to_nearest_contract():
    text = "### CTR-x-001 — valid range\n<!-- @cw-trace realizes BR-x-001 -->\n"
    anns = ct.emit_epic_annotations("contracts.md", text)
    assert anns[0].verb == "realizes" and anns[0].source_id == "CTR-x-001"


def test_scan_source_uses_emit_source_annotations_per_file(tmp_path):
    (tmp_path / "a.py").write_text("# @cw-trace guards CTR-a-001\n")
    (tmp_path / "test_a.py").write_text("# @cw-trace verifies CTR-a-001\n")
    anns = ct.scan_source(tmp_path)
    assert {(a.file, a.source_kind) for a in anns} == {("a.py", "code"), ("test_a.py", "test")}


def test_scan_source_only_files_restricts_the_walk(tmp_path):
    (tmp_path / "a.py").write_text("# @cw-trace guards CTR-a-001\n")
    (tmp_path / "b.py").write_text("# @cw-trace guards CTR-b-001\n")
    anns = ct.scan_source(tmp_path, only_files={"a.py"})
    assert {a.target for a in anns} == {"CTR-a-001"}


# --- --scanner-version / --changed-since (#160) ------------------------------


def test_cli_scanner_version_prints_hex_digest(capsys):
    rc = ct.main(["--scanner-version"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert len(out) == 64
    int(out, 16)


def test_cli_requires_epic_dir_unless_scanner_version(capsys):
    rc = ct.main([])
    assert rc == 2
    assert "epic_dir is required" in capsys.readouterr().err


def test_changed_since_scopes_source_scan(tmp_path, capsys):
    import subprocess

    def _git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "T")
    (tmp_path / "a.py").write_text("# @cw-trace guards CTR-x-001\n")
    (tmp_path / "b.py").write_text("pass\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()

    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("### CTR-x-001 — x\n### CTR-y-001 — y\n")
    # A guard for CTR-y-001 lands in b.go AFTER base (dirty, uncommitted).
    (tmp_path / "b.py").write_text("# @cw-trace guards CTR-y-001\n")

    rc_full = ct.main([str(epic), "--source", str(tmp_path), "--format", "json"])
    full = json.loads(capsys.readouterr().out)
    rc_scoped = ct.main([str(epic), "--source", str(tmp_path), "--changed-since", base, "--format", "json"])
    scoped = json.loads(capsys.readouterr().out)

    assert rc_full == 0
    assert full["uncovered_contracts"] == []
    # Scoped scan only sees b.py (the changed file) — a.py's guard of CTR-x-001
    # is invisible to it, so CTR-x-001 looks uncovered. This is exactly why
    # --changed-since must never back /close-epic's authoritative coverage gate.
    assert rc_scoped == 0  # no --gate passed; report-only
    assert scoped["uncovered_contracts"] == ["CTR-x-001"]


def test_changed_since_whole_repo_default_is_unaffected(tmp_path, capsys):
    epic = _write_epic(tmp_path)
    rc = ct.main([str(epic), "--gate", "coverage"])
    assert rc == 1
