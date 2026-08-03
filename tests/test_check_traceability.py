"""Tests for the traceability graph checker (#36)."""

from __future__ import annotations

import json
from datetime import date

import check_traceability as ct
from chief_wiggum.hashing import hash_epic_definitions
from chief_wiggum.trace_links import SIDECAR_RELPATH, build_sidecar, load_sidecar, write_sidecar

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


# --- language coverage metadata (#162) ---------------------------------------


def test_unsupported_extension_file_is_not_silently_skipped(tmp_path):
    epic = _write_epic(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "order.py").write_text("# @cw-trace guards CTR-order-001\n")
    (src / "test_order.py").write_text("# @cw-trace verifies CTR-order-001\n")
    (src / "legacy.php").write_text("<?php $x = 1;\n")
    report = ct.check(epic, src)
    assert any("no emitter coverage" in w and ".php" in w for w in report.warnings)


def test_unsupported_extension_counts_are_aggregated_per_extension(tmp_path):
    (tmp_path / "a.php").write_text("<?php\n")
    (tmp_path / "b.php").write_text("<?php\n")
    (tmp_path / "c.cpp").write_text("int main() {}\n")
    counts = ct.unsupported_extension_counts(tmp_path)
    assert counts == {".php": 2, ".cpp": 1}


def test_unsupported_extension_counts_empty_when_all_supported(tmp_path):
    (tmp_path / "a.go").write_text("func f() {}\n")
    (tmp_path / "b.py").write_text("def f(): pass\n")
    assert ct.unsupported_extension_counts(tmp_path) == {}


def test_unsupported_extension_counts_ignores_arbitrary_non_source_files(tmp_path):
    (tmp_path / "README.md").write_text("# hi\n")
    (tmp_path / "package-lock.json").write_text("{}\n")
    assert ct.unsupported_extension_counts(tmp_path) == {}


def test_scan_source_routes_language_files_through_emitter_registry(tmp_path, monkeypatch):
    """The gate consumes scripts/emitters' dispatch path for language files —
    not a private direct call to emit_source_annotations — so a per-language
    emitter can't drift from what the gate actually scans. Verification
    artifacts (.rego/.yaml/.yml — not a language in the matrix) keep the
    direct path. Regression: fails if scan_source reverts to calling
    emit_source_annotations directly for language files."""
    calls: list[str] = []
    real_emit = ct.emitters.emit

    def spy(path, content):
        calls.append(path)
        return real_emit(path, content)

    monkeypatch.setattr(ct.emitters, "emit", spy)
    (tmp_path / "order.py").write_text("# @cw-trace guards CTR-order-001\n")
    (tmp_path / "policy.rego").write_text("# @cw-trace verifies CTR-order-001\n")
    anns = ct.scan_source(tmp_path)
    assert calls == ["order.py"]  # language file via registry; .rego direct
    kinds = {(a.file, a.source_kind) for a in anns}
    assert ("order.py", "code") in kinds
    assert ("policy.rego", "policy") in kinds  # direct path still scanned


def test_changed_since_scoped_scan_still_warns_on_unsupported_extension(tmp_path, capsys):
    """A changed .php file must trigger the coverage warning even in
    --changed-since scoped mode — scoping must never make a coverage gap
    silent (the changed-path predicate is widened beyond SOURCE_EXTS)."""
    import subprocess

    def _git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "T")
    (tmp_path / "a.py").write_text("# @cw-trace guards CTR-x-001\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()

    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("### CTR-x-001 — x\n")
    # Added AFTER base: an unsupported-language file (and nothing else changed).
    (tmp_path / "legacy.php").write_text("<?php $x = 1;\n")

    rc = ct.main([str(epic), "--source", str(tmp_path), "--changed-since", base, "--format", "json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert any("no emitter coverage" in w and ".php" in w for w in data["warnings"])


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


def test_changed_since_non_git_source_is_usage_error(tmp_path, capsys):
    """--changed-since against a non-git --source must exit 2 with a concise
    message, never a traceback (#179 review)."""
    epic = _write_epic(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    rc = ct.main([str(epic), "--source", str(src), "--changed-since", "main"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Error" in err and "Traceback" not in err


def test_changed_since_bad_ref_is_usage_error(tmp_path, capsys):
    import subprocess

    def _git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "T")
    (tmp_path / "a.py").write_text("pass\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")
    epic = _write_epic(tmp_path)
    rc = ct.main([str(epic), "--source", str(tmp_path), "--changed-since", "no-such-ref"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Error" in err and "Traceback" not in err


def test_full_scan_skips_nested_git_checkout(tmp_path):
    """Submodules / vendored repos (a dir containing a .git entry) are excluded
    from the FULL scan, matching --changed-since (whose manifest never surfaces
    a submodule's files — a submodule is a single gitlink entry there)."""
    (tmp_path / "a.py").write_text("# @cw-trace guards CTR-a-001\n")
    sub = tmp_path / "vendor-app"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../.git/modules/vendor-app\n")
    (sub / "b.py").write_text("# @cw-trace guards CTR-b-001\n")
    anns = ct.scan_source(tmp_path)
    assert {a.target for a in anns} == {"CTR-a-001"}


# --- suspect-link propagation (#169) -----------------------------------------


def _epic_with_ctr(tmp_path, reworded=False):
    epic = tmp_path / "epic"
    epic.mkdir(exist_ok=True)
    condition = "True" if reworded else "start_date <= end_date"
    (epic / "contracts.md").write_text(
        f"### CTR-order-001 — valid date range\nREQUIRES: {condition}\n"
    )
    return epic


def _src_guarding_ctr(tmp_path):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "order.py").write_text("# @cw-trace guards CTR-order-001\n")
    (src / "test_order.py").write_text("# @cw-trace verifies CTR-order-001\n")
    return src


def test_reword_flips_recorded_links_to_suspect_then_revalidation_clears(tmp_path):
    epic = _epic_with_ctr(tmp_path)
    src = _src_guarding_ctr(tmp_path)
    links_path = tmp_path / "docs" / "quality" / "trace-links.json"

    # Initial validation: no prior sidecar -> nothing suspect yet; write it.
    r0 = ct.check(epic, src, links_path=links_path)
    assert r0.suspect_links == []
    ct.write_links_sidecar(epic, src, links_path)
    assert links_path.is_file()

    # Reword the contract -> its definition hash changes.
    _epic_with_ctr(tmp_path, reworded=True)

    r1 = ct.check(epic, src, links_path=links_path)
    assert len(r1.suspect_links) == 2  # the guards link AND the verifies link
    assert r1.suspect_contracts == ["CTR-order-001"]
    assert {d["verb"] for d in r1.suspect_links} == {"guards", "verifies"}

    # Re-validation: refresh the sidecar against the reworded contract -> clears.
    ct.write_links_sidecar(epic, src, links_path)
    r2 = ct.check(epic, src, links_path=links_path)
    assert r2.suspect_links == []
    assert r2.suspect_contracts == []


def test_suspect_links_do_not_affect_soundness_or_coverage_ok(tmp_path):
    """Suspect is report-only initially (docs/gate-rollout.md doctrine)."""
    epic = _epic_with_ctr(tmp_path)
    src = _src_guarding_ctr(tmp_path)
    links_path = tmp_path / "docs" / "quality" / "trace-links.json"
    ct.write_links_sidecar(epic, src, links_path)
    _epic_with_ctr(tmp_path, reworded=True)
    r = ct.check(epic, src, links_path=links_path)
    assert r.suspect_links  # something IS suspect
    assert r.soundness_ok and r.coverage_ok  # but the existing gates are unaffected


def test_no_sidecar_means_nothing_is_suspect(tmp_path):
    epic = _epic_with_ctr(tmp_path)
    src = _src_guarding_ctr(tmp_path)
    r = ct.check(epic, src, links_path=tmp_path / "nope" / "trace-links.json")
    assert r.suspect_links == []


def test_write_links_sidecar_records_current_definition_hashes(tmp_path):
    epic = _epic_with_ctr(tmp_path)
    src = _src_guarding_ctr(tmp_path)
    links_path = tmp_path / "trace-links.json"
    body = ct.write_links_sidecar(epic, src, links_path)
    hashes = hash_epic_definitions(epic)
    assert all(link["definition_hash"] == hashes["CTR-order-001"] for link in body["links"])
    reloaded = load_sidecar(links_path)
    assert reloaded == body


def test_cli_write_links_writes_sidecar_only_when_gate_passes(tmp_path):
    epic = _epic_with_ctr(tmp_path)
    src = _src_guarding_ctr(tmp_path)
    links_path = tmp_path / "docs" / "quality" / "trace-links.json"

    # coverage gate passes (guarded + verified) -> sidecar is written.
    rc = ct.main([
        str(epic), "--source", str(src), "--gate", "coverage",
        "--links", str(links_path), "--write-links", "--format", "json",
    ])
    assert rc == 0
    assert links_path.is_file()

    # Now break coverage (delete the verifying test) -> gate fails -> sidecar
    # must NOT be overwritten with the new (uncovered) state.
    before = links_path.read_text()
    (src / "test_order.py").unlink()
    rc2 = ct.main([
        str(epic), "--source", str(src), "--gate", "coverage",
        "--links", str(links_path), "--write-links", "--format", "json",
    ])
    assert rc2 == 1
    assert links_path.read_text() == before


def test_cli_default_links_path_is_under_source_docs_quality(tmp_path):
    epic = _epic_with_ctr(tmp_path)
    src = _src_guarding_ctr(tmp_path)
    rc = ct.main([str(epic), "--source", str(src), "--write-links", "--format", "json"])
    assert rc == 0
    assert (src / SIDECAR_RELPATH).is_file()


def test_uppercase_slug_id_records_a_link_and_goes_suspect(tmp_path):
    """Regression (PR #181 review): definition-hash keys and annotation targets
    must join on the SAME canonical form — an uppercase-slug ID like CTR-BIL-001
    previously recorded no sidecar link and could never go suspect."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text(
        "### CTR-BIL-001 — customer uniqueness\nREQUIRES: one customer per provider\n"
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "billing.py").write_text("# @cw-trace guards CTR-BIL-001\n")
    links_path = tmp_path / "trace-links.json"

    body = ct.write_links_sidecar(epic, src, links_path)
    assert len(body["links"]) == 1  # the uppercase-slug link IS recorded
    assert body["links"][0]["target"] == "CTR-bil-001"

    (epic / "contracts.md").write_text(
        "### CTR-BIL-001 — customer uniqueness\nREQUIRES: True\n"  # reworded
    )
    r = ct.check(epic, src, links_path=links_path)
    assert r.suspect_contracts == ["CTR-bil-001"]


def test_cli_write_links_with_changed_since_is_usage_error(tmp_path, capsys):
    """--write-links must always be a FULL scan (PR #181 review): rewriting the
    global sidecar from a --changed-since partial scan would silently drop
    validated links for unchanged files (false-negative suspects later)."""
    epic = _epic_with_ctr(tmp_path)
    src = _src_guarding_ctr(tmp_path)
    links_path = tmp_path / "trace-links.json"
    rc = ct.main([
        str(epic), "--source", str(src), "--write-links", "--changed-since", "main",
        "--links", str(links_path),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--write-links" in err and "--changed-since" in err and "FULL scan" in err
    assert not links_path.exists()  # nothing was written


# --- JUSTIFIED waivers (#169) -------------------------------------------------


def _epic_with_uncovered_ctr(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir(exist_ok=True)
    (epic / "contracts.md").write_text("### CTR-order-002 — idempotent creation\nNo code yet.\n")
    return epic


def _write_justification(epic, **overrides):
    jdir = epic / "justifications"
    jdir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": "CTR-order-002",
        "reason": "manual QA only for this release",
        "approver": "jane@example.com",
        "expiry": "2099-01-01",
        "ticket": "#170",
    }
    data.update(overrides)
    (jdir / "ctr-order-002.json").write_text(json.dumps(data))


def test_valid_justification_satisfies_coverage_and_renders_distinctly(tmp_path):
    epic = _epic_with_uncovered_ctr(tmp_path)
    _write_justification(epic)
    r = ct.check(epic)
    assert r.uncovered_contracts == [] and r.untested_contracts == []
    assert r.coverage_ok
    assert len(r.justified_contracts) == 1
    assert r.justified_contracts[0]["id"] == "CTR-order-002"
    assert r.justified_contracts[0]["ticket"] == "#170"
    assert r.expired_justifications == []
    assert r.invalid_justifications == []


def test_expired_justification_does_not_satisfy_coverage(tmp_path):
    epic = _epic_with_uncovered_ctr(tmp_path)
    _write_justification(epic, expiry="2000-01-01")
    r = ct.check(epic, today=date(2026, 1, 1))
    assert "CTR-order-002" in r.uncovered_contracts
    assert "CTR-order-002" in r.untested_contracts
    assert not r.coverage_ok
    assert r.justified_contracts == []
    assert len(r.expired_justifications) == 1
    assert r.expired_justifications[0]["id"] == "CTR-order-002"


def test_justification_without_ticket_ref_is_invalid_and_does_not_satisfy_coverage(tmp_path):
    epic = _epic_with_uncovered_ctr(tmp_path)
    _write_justification(epic, ticket="")
    r = ct.check(epic)
    assert "CTR-order-002" in r.uncovered_contracts
    assert not r.coverage_ok
    assert r.justified_contracts == []
    assert len(r.invalid_justifications) == 1
    assert "ticket" in r.invalid_justifications[0]["reason"]


def test_justification_for_undefined_id_is_reported_invalid(tmp_path):
    epic = _epic_with_uncovered_ctr(tmp_path)
    _write_justification(epic, id="CTR-ghost-999")
    r = ct.check(epic)
    assert r.justified_contracts == []
    assert any("undefined" in d["reason"] for d in r.invalid_justifications)


def test_justification_with_uppercase_slug_id_joins_canonical_contract(tmp_path):
    """A waiver written with the epic doc's raw casing (CTR-ORDER-002) must join
    the canonical uncovered/untested sets — same canonicalization rule as
    annotations and definition hashes (PR #181 review)."""
    epic = _epic_with_uncovered_ctr(tmp_path)
    _write_justification(epic, id="CTR-ORDER-002")
    r = ct.check(epic)
    assert r.coverage_ok
    assert r.justified_contracts[0]["id"] == "CTR-order-002"
    assert r.invalid_justifications == []


def test_justification_with_placeholder_ticket_ref_is_invalid(tmp_path):
    """Regression (PR #181 review): truthiness-only validation let placeholders
    like '  ', 'none', 'N/A' satisfy the ticket requirement."""
    for placeholder in ("  ", "none", "N/A", "TBD"):
        epic = _epic_with_uncovered_ctr(tmp_path)
        _write_justification(epic, ticket=placeholder)
        r = ct.check(epic)
        assert "CTR-order-002" in r.uncovered_contracts, placeholder
        assert not r.coverage_ok, placeholder
        assert r.justified_contracts == [], placeholder
        assert len(r.invalid_justifications) == 1, placeholder
        assert "ticket" in r.invalid_justifications[0]["reason"], placeholder


def test_justification_for_already_covered_contract_has_no_effect(tmp_path):
    epic = _epic_with_ctr(tmp_path)
    src = _src_guarding_ctr(tmp_path)
    _write_justification(epic, id="CTR-order-001")
    r = ct.check(epic, src)
    assert r.uncovered_contracts == [] and r.untested_contracts == []
    assert r.justified_contracts == []  # nothing to waive; not reported as JUSTIFIED


def test_justified_renders_in_markdown_and_json(tmp_path, capsys):
    epic = _epic_with_uncovered_ctr(tmp_path)
    _write_justification(epic)
    rc_text = ct.main([str(epic)])
    assert rc_text == 0
    text_out = capsys.readouterr().out
    assert "Justified" in text_out
    assert "#170" in text_out

    rc_json = ct.main([str(epic), "--format", "json"])
    assert rc_json == 0
    data = json.loads(capsys.readouterr().out)
    assert data["justified_contracts"][0]["id"] == "CTR-order-002"
    assert data["coverage_ok"] is True


# --- coverage-requirement alternatives (#169) --------------------------------


def test_coverage_requires_alternatives_satisfied_by_any_declared_kind(tmp_path):
    epic = tmp_path / "epic"
    models = epic / "models"
    models.mkdir(parents=True)
    (epic / "contracts.md").write_text("### CTR-order-005 — refund idempotency\nx\n")
    (models / "contracts.json").write_text(json.dumps({
        "id": "CTR-order-005",
        "coverage_requires": ["test", "probe"],
    }))
    src = tmp_path / "src"
    src.mkdir()
    # Only a telemetry verifies exists -- NOT in the declared alternatives.
    (src / "slo.yaml").write_text("# @cw-trace verifies CTR-order-005\n")
    r = ct.check(epic, src)
    assert "CTR-order-005" in r.untested_contracts

    # A probe verifies IS in the declared alternatives -> satisfied.
    (src / "k6" / "latency.js").parent.mkdir(exist_ok=True)
    (src / "k6" / "latency.js").write_text("// @cw-trace verifies CTR-order-005\n")
    r2 = ct.check(epic, src)
    assert "CTR-order-005" not in r2.untested_contracts


def test_coverage_requires_absent_falls_back_to_any_verifies_kind(tmp_path):
    """No coverage_requires declared -> unchanged behavior: ANY verifying kind
    (test/probe/policy/telemetry) satisfies coverage."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("### CTR-order-006 — x\ny\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "slo.yaml").write_text("# @cw-trace verifies CTR-order-006\n")
    r = ct.check(epic, src)
    assert "CTR-order-006" not in r.untested_contracts


def test_extract_coverage_requirements_from_json_model(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text(json.dumps({
        "contracts": [{"id": "CTR-x-001", "coverage_requires": ["unit-test", "integration-spec"]}]
    }))
    reqs = ct.extract_coverage_requirements(epic)
    assert reqs == {"CTR-x-001": ["unit-test", "integration-spec"]}


# --- sidecar/report plumbing --------------------------------------------------


def test_build_sidecar_and_load_sidecar_are_reexported_and_compatible(tmp_path):
    """check_traceability composes chief_wiggum.trace_links directly — no
    parallel re-implementation of the sidecar format."""
    path = tmp_path / SIDECAR_RELPATH
    body = build_sidecar([], {})
    write_sidecar(path, body)
    assert load_sidecar(path) == {"scanner_version": None, "links": []}


def test_report_to_dict_includes_new_fields_and_is_json_serializable():
    r = _report({"CTR-x-001": "CTR"}, [])
    d = r.to_dict()
    for key in (
        "suspect_links", "suspect_contracts", "justified_contracts",
        "expired_justifications", "invalid_justifications",
    ):
        assert key in d
    json.loads(json.dumps(d))


def test_cli_default_links_path_routes_through_sidecar_election(tmp_path, monkeypatch):
    """#213: with a sidecar election for --source, the DEFAULT trace-links
    sidecar lands in the external quality dir — zero CW files in the target
    tree. Embedded (no election) stays <source>/docs/quality (covered by
    test_cli_default_links_path_is_under_source_docs_quality)."""
    import artifacts

    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))
    epic = _epic_with_ctr(tmp_path)
    src = _src_guarding_ctr(tmp_path)
    artifacts.elect(src, "sidecar")
    rc = ct.main([str(epic), "--source", str(src), "--write-links", "--format", "json"])
    assert rc == 0
    sidecar_links = artifacts.Resolver.resolve(src).quality_dir() / "trace-links.json"
    assert sidecar_links.is_file()
    assert load_sidecar(sidecar_links)["links"]
    assert not (src / "docs").exists()


# --- external trace-link store (#213 Phase C) ---------------------------------


_XL_CODE = (
    "def create_order(start_date, end_date):\n"
    "    assert start_date <= end_date\n"
    "    return True\n"
)
_XL_TEST = (
    "def test_create_order():\n"
    "    assert True\n"
)


def _sidecar_target_with_external_links(tmp_path, monkeypatch):
    """A sidecar-elected target with ZERO in-source annotations: the contract's
    only guards/verifies claims live in the external link store."""
    import artifacts
    from chief_wiggum import external_links

    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))
    epic = _epic_with_ctr(tmp_path)
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "order.py").write_text(_XL_CODE)
    (src / "test_order.py").write_text(_XL_TEST)
    artifacts.elect(src, "sidecar")
    store = artifacts.Resolver.resolve(src).quality_dir() / external_links.STORE_NAME
    external_links.add_link(store, src, "order.py", "create_order", "guards",
                            ["CTR-order-001"], use_lsp=False)
    external_links.add_link(store, src, "test_order.py", "test_create_order", "verifies",
                            ["CTR-order-001"], use_lsp=False)
    return epic, src, store


def test_external_store_alone_satisfies_coverage_in_sidecar_mode(tmp_path, monkeypatch, capsys):
    """A contract whose ONLY guards/verifies come from the external store
    passes coverage in sidecar mode — an external `verifies` counts exactly
    like an in-source `@cw-trace verifies`. No in-tree CW files needed."""
    epic, src, _store = _sidecar_target_with_external_links(tmp_path, monkeypatch)
    rc = ct.main([str(epic), "--source", str(src), "--gate", "coverage", "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["coverage_ok"] is True
    assert out["uncovered_contracts"] == [] and out["untested_contracts"] == []
    assert not (src / "docs").exists()


def test_suspect_external_link_stops_satisfying_coverage_and_is_reported(tmp_path, monkeypatch, capsys):
    """Editing the anchored symbol flips the store entry to suspect: it no
    longer satisfies coverage (hash drift => re-verify, not trust) and shows
    up in suspect_links with its source marked."""
    epic, src, _store = _sidecar_target_with_external_links(tmp_path, monkeypatch)
    (src / "order.py").write_text(_XL_CODE.replace("start_date <= end_date", "True"))
    rc = ct.main([str(epic), "--source", str(src), "--gate", "coverage", "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["coverage_ok"] is False
    assert out["uncovered_contracts"] == ["CTR-order-001"]  # guards link went suspect
    external_suspects = [s for s in out["suspect_links"]
                         if s.get("source") == "external-link-store"]
    assert external_suspects and external_suspects[0]["target"] == "CTR-order-001"
    assert external_suspects[0]["symbol"] == "create_order"
    assert out["suspect_contracts"] == ["CTR-order-001"]
    # The untouched verifies anchor still counts.
    assert out["untested_contracts"] == []


def test_unresolved_external_link_is_surfaced_never_dropped(tmp_path, monkeypatch, capsys):
    epic, src, _store = _sidecar_target_with_external_links(tmp_path, monkeypatch)
    (src / "order.py").unlink()
    rc = ct.main([str(epic), "--source", str(src), "--format", "json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["coverage_ok"] is False
    assert any("order.py::create_order" in w and "unresolved" in w for w in out["warnings"])


def test_explicit_external_links_flag_works_in_embedded_mode(tmp_path):
    """--external-links <path> reads the store even without a sidecar election."""
    from chief_wiggum import external_links

    epic = _epic_with_ctr(tmp_path)
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "order.py").write_text(_XL_CODE)
    (src / "test_order.py").write_text(_XL_TEST)
    store = tmp_path / "external-links.json"
    external_links.add_link(store, src, "order.py", "create_order", "guards",
                            ["CTR-order-001"], use_lsp=False)
    external_links.add_link(store, src, "test_order.py", "test_create_order", "verifies",
                            ["CTR-order-001"], use_lsp=False)
    r = ct.check(epic, src, external_links_path=store)
    assert r.coverage_ok is True

    # Without the store the same repo has zero annotations for the contract.
    r2 = ct.check(epic, src)
    assert r2.coverage_ok is False


def test_external_link_to_undefined_id_is_dangling(tmp_path):
    """Ok external entries face the same defined-ID join as in-source
    annotations — a link to an undeclared ID reports dangling."""
    from chief_wiggum import external_links

    epic = _epic_with_ctr(tmp_path)
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "order.py").write_text(_XL_CODE)
    store = tmp_path / "external-links.json"
    external_links.add_link(store, src, "order.py", "create_order", "guards",
                            ["CTR-ghost-999"], use_lsp=False)
    r = ct.check(epic, src, external_links_path=store)
    assert any(d["target"] == "CTR-ghost-999" for d in r.dangling)


# --- vacuous-pass fix (applicability, chief-wiggum#213 Phase E) ---------------


def test_empty_epic_and_source_is_inapplicable(tmp_path):
    """Zero defined IDs AND zero annotations = an empty graph: coverage_ok is
    (vacuously) true, but the report says INAPPLICABLE, never a plain green."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("# nothing declared here\n")
    src = tmp_path / "src"
    src.mkdir()
    report = ct.check(epic, src, schema=SCHEMA)
    assert report.applicability == "inapplicable"
    assert report.coverage_ok is True  # exit semantics unchanged
    assert report.soundness_ok is True


def test_defined_ids_make_the_report_applicable(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("**CTR-order-001**: valid range\n")
    report = ct.check(epic, schema=SCHEMA)
    assert report.applicability == "applicable"
    # ...and its gaps are REAL findings, not vacuous ones.
    assert report.uncovered_contracts == ["CTR-order-001"]


def test_annotations_without_definitions_are_inapplicable_but_dangle(tmp_path):
    """F9: with ZERO contracts defined there is nothing for coverage to be
    true of — inapplicable REGARDLESS of annotations. The annotations
    themselves remain a soundness matter: they are all dangling, and soundness
    still fails on them (exit codes unchanged)."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("# empty\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "svc.py").write_text("# @cw-trace guards CTR-order-001\n")
    report = ct.check(epic, src, schema=SCHEMA)
    assert report.applicability == "inapplicable"
    assert report.dangling  # soundness findings survive the classification
    assert report.soundness_ok is False
    assert report.coverage_ok is True  # vacuously — which is the point


def test_cli_gate_soundness_still_fails_on_dangling_when_inapplicable(tmp_path, capsys):
    """F9 keeps exit codes unchanged: annotations-without-definitions are
    dangling, so --gate soundness still exits 1 even though coverage is
    classified inapplicable."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("# empty\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "svc.py").write_text("# @cw-trace guards CTR-order-001\n")
    rc = ct.main([str(epic), "--source", str(src), "--gate", "soundness", "--format", "json"])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["applicability"] == "inapplicable"
    assert data["dangling"]


def test_cli_gate_coverage_annotations_without_definitions_banner(tmp_path, capsys):
    """F9: --gate coverage with annotations but zero definitions exits 0
    (coverage has nothing to hold over) but prints the inapplicable banner —
    never a silent identical green."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("# empty\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "svc.py").write_text("# @cw-trace guards CTR-order-001\n")
    rc = ct.main([str(epic), "--source", str(src), "--gate", "coverage", "--format", "json"])
    assert rc == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["applicability"] == "inapplicable"
    assert "inapplicable, not passing" in captured.err


def test_cli_gate_coverage_inapplicable_exits_zero_with_banner(tmp_path, capsys):
    """The pre-existing vacuous-pass bug this issue owns: --gate coverage on an
    epic with ZERO contracts/annotations used to exit 0 with a silent green.
    Exit code stays 0 (no existing pipeline breaks) but the banner prints and
    the JSON carries applicability explicitly."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("# nothing\n")
    src = tmp_path / "src"
    src.mkdir()
    rc = ct.main([str(epic), "--source", str(src), "--gate", "coverage", "--format", "json"])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["applicability"] == "inapplicable"
    assert "inapplicable, not passing" in captured.err


def test_cli_inapplicable_text_output_says_so(tmp_path, capsys):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("# nothing\n")
    src = tmp_path / "src"
    src.mkdir()
    rc = ct.main([str(epic), "--source", str(src)])
    assert rc == 0
    assert "INAPPLICABLE" in capsys.readouterr().out


def test_cli_applicable_gate_prints_no_banner(tmp_path, capsys):
    """A populated epic must not print the inapplicable banner."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text(
        "**CTR-order-001**: valid range\n"
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "svc.py").write_text(
        "# @cw-trace guards CTR-order-001\n"
        "def f():\n    pass\n"
    )
    (src / "test_svc.py").write_text(
        "# @cw-trace verifies CTR-order-001\n"
        "def test_f():\n    pass\n"
    )
    rc = ct.main([str(epic), "--source", str(src), "--gate", "coverage", "--format", "json"])
    assert rc == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["applicability"] == "applicable"
    assert "INAPPLICABLE" not in captured.err


# --- three-state measurement: inapplicable | applicable | error (#281) ------
#
# A measurement can fail in two structurally different ways: "there was
# nothing to measure" (inapplicable) vs "there WAS something to measure and
# the instrument saw none of it" (error — a broken instrument, never a pass).
# ID-bearing artifacts (contracts.md/.json, invariants.md,
# state-machines.md/.json, architecture.json) that exist WITH content but
# yield ZERO parseable stable IDs are the #281 case: the architect skill's own
# worked example modelled two-segment `INV-001` ids, which DEFINE_RE cannot
# see, so an epic authored from it measures nothing and used to render green.


def _epic_with_near_miss_invariant(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "invariants.md").write_text(
        "## Epic Invariants\n\n1. **INV-001 — X**: y\n"
    )
    return epic


def test_epic_with_only_non_id_bearing_artifacts_is_inapplicable(tmp_path):
    """adr.md/retrospective.md legitimately carry no declarations — their
    presence (with content) must NOT trip the new error state."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "adr.md").write_text("# ADR\n\nWe decided to use Postgres.\n")
    (epic / "retrospective.md").write_text("# Retro\n\nWent well.\n")
    report = ct.check(epic, schema=SCHEMA)
    assert report.applicability == "inapplicable"
    assert report.outcome == "inapplicable"
    assert report.soundness_ok is True
    assert report.unparsed_artifacts == []


def test_empty_epic_dir_is_inapplicable(tmp_path):
    """Regression: a genuinely empty epic dir (no files at all) stays
    inapplicable — the pre-#281 behavior must not change."""
    epic = tmp_path / "epic"
    epic.mkdir()
    report = ct.check(epic, schema=SCHEMA)
    assert report.applicability == "inapplicable"
    assert report.outcome == "inapplicable"


def test_zero_byte_id_bearing_artifact_is_inapplicable_not_error(tmp_path):
    """A zero-byte contracts.md has nothing to parse yet — that is 'nothing to
    measure' (inapplicable), NOT 'measured and saw nothing' (error)."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("")
    report = ct.check(epic, schema=SCHEMA)
    assert report.applicability == "inapplicable"
    assert report.outcome == "inapplicable"
    assert report.unparsed_artifacts == []


def test_whitespace_only_id_bearing_artifact_is_inapplicable(tmp_path):
    """Same as the zero-byte case: whitespace-only content is still 'nothing
    to measure'."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text("\n\n  \n")
    report = ct.check(epic, schema=SCHEMA)
    assert report.applicability == "inapplicable"
    assert report.outcome == "inapplicable"


def test_artifacts_with_two_segment_ids_are_error(tmp_path):
    """The #281 case itself: invariants.md is present, has content, and
    declares ONLY the two-segment INV-001 shape DEFINE_RE cannot see. The
    scanner parsed ZERO ids out of a real artifact — that is a BROKEN
    instrument (error), never a clean inapplicable pass."""
    epic = _epic_with_near_miss_invariant(tmp_path)
    report = ct.check(epic, schema=SCHEMA)
    assert report.applicability == "error"
    assert report.outcome == "error"
    assert report.soundness_ok is False
    assert report.unparsed_artifacts
    assert report.unparsed_artifacts[0]["file"] == "invariants.md"
    assert report.malformed_ids
    assert report.malformed_ids[0]["token"] == "INV-001"
    assert report.malformed_ids[0]["line"] == 3


def test_prose_only_artifact_with_no_id_shaped_tokens_is_still_error(tmp_path):
    """The error state must NOT depend on a near-miss token being present:
    an ID-bearing artifact with content and zero KIND- shaped tokens at all is
    just as broken a measurement as one with a near-miss."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text(
        "## Contracts\n\n- This document intentionally has no stable IDs yet.\n"
        "- Placeholder prose only.\n"
    )
    report = ct.check(epic, schema=SCHEMA)
    assert report.applicability == "error"
    assert report.unparsed_artifacts
    assert report.malformed_ids == []


def test_partial_drift_is_applicable_but_soundness_fails(tmp_path):
    """The realistic partial-drift shape: contracts.json is model-generated
    and fine, invariants.md was hand-written from the (buggy) skill example.
    The epic AS A WHOLE is applicable (something WAS measured), but the
    near-miss in invariants.md is a soundness finding — silently
    under-measuring must not render as clean."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text('{"id": "CTR-order-001"}')
    (epic / "invariants.md").write_text("**INV-001**: something\n")
    report = ct.check(epic, schema=SCHEMA)
    assert report.applicability == "applicable"
    assert report.outcome == "findings"
    assert report.malformed_ids
    assert report.soundness_ok is False
    assert report.unparsed_artifacts == []


def test_ent_prefixed_id_is_not_a_near_miss(tmp_path):
    """ENT- is not a stable-ID KIND (chief-wiggum#281 codebase-context §2B) —
    ENT-INV-001 must never be reported as a malformed near-miss."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text('{"id": "CTR-order-001"}')
    (epic / "invariants.md").write_text("**ENT-INV-001** is a foreign id, not ours\n")
    report = ct.check(epic, schema=SCHEMA)
    assert report.malformed_ids == []


def test_measured_denominator_is_reported(tmp_path):
    """#289 item 5: the denominator must be visible even when green, so a
    zero can never hide inside an otherwise-passing report."""
    epic = _write_epic(tmp_path)
    report = ct.check(epic, schema=SCHEMA)
    assert report.to_dict()["measured"] == {
        "id_bearing_artifacts": 1,
        "defined_ids": len(report.defined),
    }


def test_cli_gate_soundness_fails_on_error_state(tmp_path, capsys):
    epic = _epic_with_near_miss_invariant(tmp_path)
    rc = ct.main([str(epic), "--gate", "soundness"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ERROR" in err
    assert "ZERO stable IDs" in err


def test_cli_gate_coverage_fails_on_error_state(tmp_path, capsys):
    """The one that would be 0 under the old (inapplicable) semantics: a
    broken measurement must fail EITHER gate, not just soundness."""
    epic = _epic_with_near_miss_invariant(tmp_path)
    rc = ct.main([str(epic), "--gate", "coverage"])
    assert rc == 1


def test_cli_report_only_on_error_state_exits_zero(tmp_path, capsys):
    """The report-only doctrine holds even for a broken instrument: no
    --gate means exit 0, but the JSON must not silently say 'inapplicable'."""
    epic = _epic_with_near_miss_invariant(tmp_path)
    rc = ct.main([str(epic), "--format", "json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["applicability"] == "error"


def test_cli_error_text_output_says_failure_not_pass(tmp_path, capsys):
    epic = _epic_with_near_miss_invariant(tmp_path)
    rc = ct.main([str(epic), "--gate", "soundness", "--format", "text"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "FAILURE" in out
    assert "INAPPLICABLE" not in out


def test_cli_json_carries_outcome_and_measured(tmp_path, capsys):
    epic = _epic_with_near_miss_invariant(tmp_path)
    rc = ct.main([str(epic), "--format", "json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["outcome"] == "error"
    assert data["measured"]["defined_ids"] == 0


def test_write_links_skipped_on_error_state(tmp_path, capsys):
    """A broken measurement must never be recorded as a validated sidecar —
    the near-miss epic's --write-links run must leave the sidecar untouched."""
    epic = _epic_with_near_miss_invariant(tmp_path)
    links_path = tmp_path / "links.json"
    ct.main([str(epic), "--write-links", "--links", str(links_path), "--format", "json"])
    capsys.readouterr()
    assert not links_path.exists(), (
        "the trace-links sidecar must never be written from a broken/error-state "
        "scan (chief-wiggum#281)"
    )


def test_write_links_sidecar_stamps_target_sha(tmp_path):
    """#213 version binding: the sidecar carries the source repo's HEAD as an
    ADDITIVE target_sha key (None outside a git repo — unverifiable, never a
    crash); suspect detection stays hash-re-anchoring and ignores the stamp."""
    import subprocess

    epic = _epic_with_ctr(tmp_path)
    src = _src_guarding_ctr(tmp_path)

    # Non-git source root -> stamped None (additive; consumers tolerate it).
    body = ct.write_links_sidecar(epic, src, tmp_path / "links-nogit.json")
    assert "target_sha" in body and body["target_sha"] is None

    # Git source root -> the source repo's HEAD.
    for args in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", "-C", str(src), *args], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(src), "-c", "user.name=T", "-c", "user.email=t@e.co",
         "commit", "-q", "-m", "init"],
        check=True, capture_output=True,
    )
    head = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    links_path = tmp_path / "links-git.json"
    body = ct.write_links_sidecar(epic, src, links_path)
    assert body["target_sha"] == head
    # The stamp is additive: loading + suspect detection behave as before.
    reloaded = load_sidecar(links_path)
    assert reloaded["target_sha"] == head
    assert ct.check(epic, src, links_path=links_path).suspect_links == []
