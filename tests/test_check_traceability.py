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
