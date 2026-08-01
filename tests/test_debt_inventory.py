"""Tests for scripts/debt_inventory.py (#214) and its three report-only
surfacing points (/code-metrics report section, quality_slop_gate signal
block, code_query orient measured facts).

The load-bearing property is the STABLE ID: the same finding keeps its
DEBT- id across runs and across SHA moves touching unrelated files, and a
fixed finding's id disappears — never renumbered, never ordinal.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import code_query
import debt_inventory
import pytest
import quality_slop_gate
from quality import report as quality_report

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _commit_all(repo: Path, msg: str = "seed") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg, "--no-verify")


@pytest.fixture()
def target(tmp_path, monkeypatch):
    """A synthetic target repo with one finding per pure-python engine, plus an
    isolated CW user dir so no test ever touches ~/.chief-wiggum."""
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-home"))
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Ada")
    _git(repo, "config", "user.email", "ada@example.com")
    (repo / "lib.py").write_text(
        "def used():\n    return 1\n\n"
        "def dead_helper():\n    return 2\n"
    )
    (repo / "app.py").write_text(
        "from lib import used\n"
        "# TODO: replace used() with the real call\n"
        "print(used())\n"
    )
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_gone.py").write_text("def test_hollow():\n    pass\n")
    _commit_all(repo)
    return repo


def _run_inventory(repo: Path, tmp_path: Path, out: Path | None = None) -> dict:
    out_dir = out or (tmp_path / "out")
    out_dir.mkdir(parents=True, exist_ok=True)
    envelope = debt_inventory.build_inventory(
        str(repo), str(tmp_path / "wd"), out_dir / "debt.json"
    )
    (out_dir / "debt.json").write_text(json.dumps(envelope, indent=2))
    return envelope


# --- envelope basics ----------------------------------------------------------


def test_envelope_carries_sha_authority_scope_and_engine_state(target, tmp_path):
    env = _run_inventory(target, tmp_path)
    assert env["schema"] == "debt/1"
    assert env["target_sha"], "target_sha is mandatory"
    assert "absence of a finding in an unscanned language is NOT evidence of health" in env["authority"]
    assert env["scope"].startswith("whole repo")
    assert set(env["engines"]) == {"dead_code", "clones", "test_health", "markers"}
    # findings never ride inside the engines sub-envelopes (they live in items)
    assert all("findings" not in e for e in env["engines"].values())


def test_items_cover_all_pure_python_engines(target, tmp_path):
    env = _run_inventory(target, tmp_path)
    engines = {i["engine"] for i in env["items"]}
    assert {"dead_code", "test_health", "markers"} <= engines
    dead = next(i for i in env["items"] if i["engine"] == "dead_code")
    assert dead["symbol"] == "dead_helper"
    assert dead["locations"] == ["lib.py:4"]
    assert dead["id"].startswith("DEBT-") and len(dead["id"]) == 5 + 10
    kinds = {i["kind"] for i in env["items"] if i["engine"] == "test_health"}
    assert {"orphaned_test", "assertion_free_test"} <= kinds
    todo = next(i for i in env["items"] if i["engine"] == "markers")
    assert todo["kind"] == "TODO" and todo["severity"] == "low"


def test_blast_radius_states_hotspot_absence(target, tmp_path):
    env = _run_inventory(target, tmp_path)
    assert env["hotspots_available"] is False
    assert "hotspots.json absent" in env["hotspots_note"]
    for item in env["items"]:
        assert item["blast_radius"]["hotspot_decile"] is None


# --- stable IDs (the INV-fh-007 departure) ------------------------------------


def test_same_finding_keeps_id_across_runs_and_sha_moves(target, tmp_path):
    env1 = _run_inventory(target, tmp_path)
    ids1 = {i["id"] for i in env1["items"]}

    # SHA-moving commit touching an UNRELATED file.
    (target / "unrelated.py").write_text("x = 1\n")
    _commit_all(target, "unrelated change")
    env2 = _run_inventory(target, tmp_path)
    ids2 = {i["id"] for i in env2["items"]}

    assert env1["target_sha"] != env2["target_sha"]
    assert ids1 <= ids2  # every original finding keeps its exact id


def test_fixed_finding_disappears_without_renumbering_the_rest(target, tmp_path):
    env1 = _run_inventory(target, tmp_path)
    dead_id = next(i["id"] for i in env1["items"] if i["engine"] == "dead_code")
    other_ids = {i["id"] for i in env1["items"]} - {dead_id}

    # Fix the dead symbol.
    (target / "lib.py").write_text("def used():\n    return 1\n")
    _commit_all(target, "remove dead helper")
    env2 = _run_inventory(target, tmp_path)
    ids2 = {i["id"] for i in env2["items"]}

    assert dead_id not in ids2  # resolved -> absent
    assert other_ids <= ids2  # survivors keep their ids (no ordinals anywhere)


def test_id_is_content_anchored_not_line_anchored(target, tmp_path):
    env1 = _run_inventory(target, tmp_path)
    todo_id = next(i["id"] for i in env1["items"] if i["engine"] == "markers")

    # Move the TODO to a different line in the same file: id must not change.
    (target / "app.py").write_text(
        "from lib import used\n"
        "print(used())\n"
        "# TODO: replace used() with the real call\n"
    )
    _commit_all(target, "shuffle lines")
    env2 = _run_inventory(target, tmp_path)
    todo2 = next(i for i in env2["items"] if i["engine"] == "markers")
    assert todo2["id"] == todo_id
    assert todo2["locations"] == ["app.py:3"]  # location updated, identity kept


def test_first_seen_preserved_last_seen_updated(target, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    env1 = debt_inventory.build_inventory(
        str(target), str(tmp_path / "wd"), out / "debt.json", now="2026-01-01T00:00:00+00:00")
    (out / "debt.json").write_text(json.dumps(env1))
    env2 = debt_inventory.build_inventory(
        str(target), str(tmp_path / "wd"), out / "debt.json", now="2026-02-01T00:00:00+00:00")
    for item in env2["items"]:
        assert item["first_seen"] == "2026-01-01T00:00:00+00:00"
        assert item["last_seen"] == "2026-02-01T00:00:00+00:00"


# --- severity rubric ----------------------------------------------------------


def test_clone_class_severity_scales_with_size():
    small = debt_inventory._clone_items({"clone_classes": [
        {"content_hash": "aa", "size": 2, "lines": 6, "tokens": 40,
         "members": [{"file": "a.py", "start_line": 1, "end_line": 6},
                     {"file": "b.py", "start_line": 1, "end_line": 6}]},
        {"content_hash": "bb", "size": 3, "lines": 6, "tokens": 40,
         "members": [{"file": "a.py", "start_line": 1, "end_line": 6},
                     {"file": "b.py", "start_line": 1, "end_line": 6},
                     {"file": "c.py", "start_line": 1, "end_line": 6}]},
    ]})
    by_hash = {i["symbol"]: i["severity"] for i in small}
    assert by_hash == {"aa": "medium", "bb": "high"}
    # clone-class ids have no path component — content hash IS the identity
    assert small[0]["id"] == debt_inventory.debt_id("clones", "", "aa")


def test_dead_code_severity_bumped_in_hotspot_decile_files():
    finding = {"file": "hot.py", "line": 3, "symbol": "dead", "kind": "function",
               "tier": "builtin-ast"}
    cold = debt_inventory._dead_code_items({"findings": [finding]}, {})
    hot = debt_inventory._dead_code_items({"findings": [finding]}, {"hot.py": 10})
    assert cold[0]["severity"] == "low"
    assert hot[0]["severity"] == "medium"
    assert cold[0]["id"] == hot[0]["id"]  # severity never feeds the id


def test_marker_severity_and_duplicate_text_grouping():
    findings = [
        {"file": "a.py", "line": 1, "kind": "HACK", "text": "same hack"},
        {"file": "a.py", "line": 9, "kind": "HACK", "text": "same hack"},
        {"file": "a.py", "line": 5, "kind": "TODO", "text": "later"},
    ]
    cold = debt_inventory._marker_items({"findings": findings}, {})
    hack = next(i for i in cold if i["kind"] == "HACK")
    assert hack["locations"] == ["a.py:1", "a.py:9"]  # one item, both locations
    assert hack["severity"] == "low"
    hot = debt_inventory._marker_items({"findings": findings}, {"a.py": 9})
    assert next(i for i in hot if i["kind"] == "HACK")["severity"] == "medium"
    assert next(i for i in hot if i["kind"] == "TODO")["severity"] == "low"  # TODO never bumps


def test_duplicate_anchor_findings_merge_into_one_item(target, tmp_path):
    """Live-caught on a real validation repo: two identical t.Skip lines in one
    file share a content anchor — they must become ONE item with both
    locations, never two rows with the same id."""
    (target / "pkg").mkdir()
    (target / "pkg" / "a.go").write_text("package pkg\n")
    (target / "pkg" / "a_test.go").write_text(
        "package pkg\nimport \"testing\"\n"
        "func TestX(t *testing.T) {\n\tt.Skip(\"flaky\")\n\tt.Fatal(\"x\")\n}\n"
        "func TestY(t *testing.T) {\n\tt.Skip(\"flaky\")\n\tt.Fatal(\"y\")\n}\n"
    )
    _commit_all(target, "add doubly-skipped suite")
    env = _run_inventory(target, tmp_path)
    ids = [i["id"] for i in env["items"]]
    assert len(ids) == len(set(ids)), "DEBT ids must be unique in the inventory"
    skip_items = [i for i in env["items"]
                  if i["kind"] == "skipped_test" and i["locations"][0].startswith("pkg/")]
    assert len(skip_items) == 1
    assert skip_items[0]["locations"] == ["pkg/a_test.go:4", "pkg/a_test.go:8"]


# --- CLI ----------------------------------------------------------------------


def test_cli_out_flag_writes_elsewhere_and_exits_zero(target, tmp_path, monkeypatch):
    out = tmp_path / "elsewhere"
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "debt_inventory.py"),
         "--repo", str(target), "--out", str(out), "--workdir", str(tmp_path / "wd")],
        capture_output=True, text=True,
        env={"CHIEF_WIGGUM_USER_DIR": str(tmp_path / "cw-home"),
             "PATH": __import__("os").environ["PATH"]},
    )
    assert proc.returncode == 0, proc.stderr
    assert (out / "debt.json").is_file()
    assert not (target / "docs").exists(), "--out must not write into the target repo"
    assert "Debt inventory (report-only)" in proc.stdout
    assert "NOT evidence of health" in proc.stdout  # authority printed verbatim


def test_cli_default_writes_resolver_quality_dir(target, tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "debt_inventory.py"),
         "--repo", str(target), "--workdir", str(tmp_path / "wd")],
        capture_output=True, text=True,
        env={"CHIEF_WIGGUM_USER_DIR": str(tmp_path / "cw-home"),
             "PATH": __import__("os").environ["PATH"]},
    )
    assert proc.returncode == 0, proc.stderr
    assert (target / "docs" / "quality" / "debt.json").is_file()  # embedded-mode default


# --- surfacing: /code-metrics report section ----------------------------------


def test_report_renders_debt_section_when_present(target, tmp_path):
    env = _run_inventory(target, tmp_path)
    engines = {"churn": {}, "complexity": {}, "process": {}, "survival": {"skipped": "x"},
               "duplication": {"skipped": "x"}, "trend": {}, "debt": env}
    combined = quality_report.build_combined(engines)
    md = quality_report.render_markdown(engines, combined, charts=[])
    assert "## Debt inventory (report-only, #214)" in md
    assert "DEBT-" in md
    assert f"`{env['target_sha']}`" in md


def test_report_has_no_debt_section_without_inventory():
    engines = {"churn": {}, "complexity": {}, "process": {}, "survival": {"skipped": "x"},
               "duplication": {"skipped": "x"}, "trend": {}}
    combined = quality_report.build_combined(engines)
    md = quality_report.render_markdown(engines, combined, charts=[])
    assert "Debt inventory" not in md


# --- surfacing: quality_slop_gate signal block --------------------------------


def test_slop_gate_debt_block_reports_counts_and_never_findings(target, tmp_path):
    _run_inventory(target, tmp_path, out=target / "docs" / "quality")
    debt = quality_slop_gate.load_debt(str(target))
    assert debt is not None
    block = quality_slop_gate.format_debt_block(debt)
    assert "report-only signal" in block
    assert "dead_code" in block
    # the debt block must NEVER reach the exit-code path
    sv = {"status": "skipped", "detail": "x"}
    dup = {"status": "skipped", "detail": "x"}
    assert quality_slop_gate.has_findings(sv, dup) == []


def test_slop_gate_debt_block_absent_inventory_is_stated(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-home"))
    repo = tmp_path / "bare"
    repo.mkdir()
    _git(repo, "init", "-q")
    block = quality_slop_gate.format_debt_block(quality_slop_gate.load_debt(str(repo)))
    assert "no debt.json" in block
    assert "not evidence of health" in block


# --- surfacing: code_query orient measured facts ------------------------------


def test_orient_surfaces_debt_items_as_measured_facts(target, tmp_path):
    _run_inventory(target, tmp_path, out=target / "docs" / "quality")
    envelope = code_query.cmd_orient(target, "lib.py", None)
    debt_facts = [f for f in envelope["facts"] if f["kind"] == "debt"]
    assert debt_facts, envelope
    fact = debt_facts[0]
    assert fact["id"].startswith("DEBT-")
    assert fact["relation"] == "measured"
    assert fact["source"] == "debt-inventory"
    assert fact["provenance"]["generating_sha"]
    # exact membership only: app.py's TODO must not leak onto lib.py
    assert all("lib.py" in ", ".join(f["locations"]) for f in debt_facts)


def test_orient_debt_facts_rank_below_direct_facts(target, tmp_path):
    _run_inventory(target, tmp_path, out=target / "docs" / "quality")
    envelope = code_query.cmd_orient(target, "lib.py", None)
    kinds = [f["kind"] for f in envelope["facts"]]
    # measured facts are last-tier: no debt fact may precede a non-debt fact
    if any(k != "debt" for k in kinds):
        first_debt = kinds.index("debt")
        assert all(k == "debt" for k in kinds[first_debt:])


def test_debt_handle_round_trips_through_show(target, tmp_path):
    _run_inventory(target, tmp_path, out=target / "docs" / "quality")
    envelope = code_query.cmd_orient(target, "lib.py", None)
    handle = next(f["handle"] for f in envelope["facts"] if f["kind"] == "debt")
    shown = code_query.cmd_show(target, handle, None)
    assert shown["facts"], shown["summary"]
    block = "\n".join(shown["facts"][0]["block"])  # extra keys flatten into the fact dict
    assert "dead_helper" in block


def test_orient_without_debt_json_has_no_debt_facts(target, tmp_path):
    envelope = code_query.cmd_orient(target, "lib.py", None)
    assert [f for f in envelope["facts"] if f["kind"] == "debt"] == []


# --- blast radius honors the scope predicate (F1) -----------------------------


def test_blast_radius_never_names_scope_excluded_partners(target, tmp_path):
    """Coupling is detected over the full history, but an out-of-scope partner
    must never appear in blast_radius — same predicate as the population."""
    (target / "excluded").mkdir()
    (target / "src").mkdir()
    for i in range(4):  # DEFAULT_MIN_CO co-changes: app.py + both partners
        (target / "app.py").write_text(
            "from lib import used\n"
            "# TODO: replace used() with the real call\n"
            f"print(used())  # rev {i}\n"
        )
        (target / "excluded" / "helper.py").write_text(f"h = {i}\n")
        (target / "src" / "partner.py").write_text(f"p = {i}\n")
        _commit_all(target, f"co-change {i}")
    scope_dir = target / "docs"
    scope_dir.mkdir(exist_ok=True)
    (scope_dir / "scope.json").write_text(json.dumps({"exclude": ["excluded/*"]}))

    env = _run_inventory(target, tmp_path)
    todo = next(i for i in env["items"] if i["engine"] == "markers")
    partner_files = {p["file"] for p in todo["blast_radius"]["coupling_partners"]}
    assert "src/partner.py" in partner_files, env["scope"]
    assert not any(f.startswith("excluded/") for f in partner_files)
    # and no item anywhere carries an out-of-scope partner
    for item in env["items"]:
        for p in item["blast_radius"]["coupling_partners"]:
            assert not p["file"].startswith("excluded/")


# --- engines sub-envelope carries counts, not payloads (F7) -------------------


def test_engine_envelope_strips_clone_payload_but_keeps_counts():
    res = {
        "engine": "clones", "clone_pairs_reported": 3,
        "clone_classes": [{"content_hash": "aa", "size": 2, "members": []}],
        "findings": [{"x": 1}],
    }
    out = debt_inventory._engine_envelope(res)
    assert "clone_classes" not in out and "findings" not in out
    assert out["clone_class_count"] == 1
    assert out["clone_pairs_reported"] == 3


def test_envelope_engines_never_carry_payload_keys(target, tmp_path):
    env = _run_inventory(target, tmp_path)
    for sub in env["engines"].values():
        assert "findings" not in sub
        assert "clone_classes" not in sub


# --- slop gate: malformed config is reported, never a crash (F4) --------------


def test_slop_gate_malformed_election_becomes_config_error_block(tmp_path, monkeypatch):
    import artifacts

    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-home"))
    repo = tmp_path / "bare"
    repo.mkdir()
    _git(repo, "init", "-q")
    ep = artifacts.election_path(artifacts.derive_target_id(repo))
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_text("{not json")

    debt = quality_slop_gate.load_debt(str(repo))  # must not raise
    assert debt and debt.get("config_error")
    block = quality_slop_gate.format_debt_block(debt)
    assert "debt inventory unavailable" in block
    assert "config needs repair" in block
    # the gate's exit stays a pure slop verdict — debt never reaches findings
    sv = {"status": "skipped", "detail": "x"}
    dup = {"status": "skipped", "detail": "x"}
    assert quality_slop_gate.has_findings(sv, dup) == []


# --- sidecar handles dereference through the resolver (F3) --------------------


def test_debt_handle_round_trips_through_show_under_sidecar_election(target, tmp_path):
    import artifacts

    artifacts.elect(target, "sidecar", backing="local")
    resolver = artifacts.Resolver.resolve(str(target))
    qdir = resolver.quality_dir()
    qdir.mkdir(parents=True, exist_ok=True)
    env = debt_inventory.build_inventory(
        str(target), str(tmp_path / "wd"), qdir / "debt.json", resolver=resolver)
    (qdir / "debt.json").write_text(json.dumps(env))
    assert not (target / "docs").exists(), "sidecar mode must not write the target"

    envelope = code_query.cmd_orient(target, "lib.py", None)
    handle = next(f["handle"] for f in envelope["facts"] if f["kind"] == "debt")
    shown = code_query.cmd_show(target, handle, None)
    assert shown["facts"], shown["summary"]
    assert "dead_helper" in "\n".join(shown["facts"][0]["block"])


def test_hotspot_handle_round_trips_through_show_under_sidecar_election(target, tmp_path):
    """Pre-existing gap fixed by the same shared helper: hotspot pseudo-handles
    must dereference under a sidecar election too."""
    import artifacts

    artifacts.elect(target, "sidecar", backing="local")
    qdir = artifacts.Resolver.resolve(str(target)).quality_dir()
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "hotspots.json").write_text(json.dumps({
        "hotspots": [{"file": "lib.py", "decile": 10, "score": 0.9,
                      "coupled_with": []}],
    }))
    shown = code_query.cmd_show(target, "docs/quality/hotspots.json#hotspots[lib.py]", None)
    assert shown["facts"], shown["summary"]
    assert '"decile": 10' in "\n".join(shown["facts"][0]["block"])


# --- assertion-scan gap prints on every surface (F8) --------------------------


def test_assertion_scan_gap_prints_in_all_three_debt_surfaces(target, tmp_path):
    (target / "web").mkdir()
    (target / "web" / "app.ts").write_text("export const x = 1;\n")
    (target / "web" / "app.test.ts").write_text("it('x', () => {});\n")
    _commit_all(target, "add unscanned-language test file")
    env = _run_inventory(target, tmp_path)

    report = debt_inventory.format_report(env)
    assert "assertion scan not run (test_health): typescript: 1 file(s)" in report

    block = quality_slop_gate.format_debt_block(env)
    assert "assertion scan not run (test_health)" in block

    engines = {"churn": {}, "complexity": {}, "process": {}, "survival": {"skipped": "x"},
               "duplication": {"skipped": "x"}, "trend": {}, "debt": env}
    md = quality_report.render_markdown(engines, quality_report.build_combined(engines), charts=[])
    assert "assertion scan not run (test_health)" in md


def test_items_carry_target_sha_and_unknown_languages_surface(target, tmp_path):
    """codex review (#214): per-item target_sha per the documented schema, and
    unknown-extension source files surface in dead_code unscanned counts."""
    (target / "widget.lua").write_text("-- lua is not a known language\nx = 1\n")
    _commit_all(target, "add lua")
    inv = _run_inventory(target, tmp_path)
    assert inv["items"], "expected findings from the seeded fixture"
    for item in inv["items"]:
        assert item["target_sha"] == inv["target_sha"]
    unscanned = inv["engines"]["dead_code"]["unscanned"]
    assert any("unknown-language (.lua)" in k for k in unscanned), unscanned
