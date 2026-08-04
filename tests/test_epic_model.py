"""Tests for chief_wiggum/epic_model.py (#326): one epic-tree walk shared by
check_traceability.py's five extractors, ratchet.py's load_contract_hashes/
contract_measurement, and code_query.py's _locate_definitions.

Golden parity for the individual extractors is already pinned by the existing
test_check_traceability.py/test_ratchet.py/test_code_query.py suites (they
exercise extract_defined_ids/find_id_bearing_artifacts/scan_malformed_ids/
extract_coverage_requirements/scan_epic_annotations/hash_epic_definitions/
_locate_definitions and pass unchanged after this refactor). This file adds:

1. Direct EpicModel unit coverage (the model itself, not just its five
   backward-compatible wrapper functions).
2. The "one epic-tree walk per invocation" proof the issue's acceptance
   criteria names explicitly, for both check_traceability.check() and
   ratchet.py's cmd_score.
3. Proof EpicModel is built FRESH per invocation — no cross-process
   memoization was introduced (the code_query.py no-cross-query-memoization
   doctrine).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import check_traceability as ct
import pytest
import ratchet
from chief_wiggum.epic_model import build_epic_model

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


# --- EpicModel: direct unit coverage -----------------------------------------


def test_build_epic_model_missing_root_is_empty(tmp_path):
    model = build_epic_model(tmp_path / "does-not-exist")
    assert model.files == {}
    assert model.defined_ids == {}
    assert model.raw_definitions == []
    assert model.id_bearing_artifacts == []
    assert model.malformed_ids == []
    assert model.coverage_requirements == {}
    assert model.epic_annotations == []
    assert model.definition_hashes == {}


def test_build_epic_model_a_file_instead_of_a_dir_degrades_gracefully(tmp_path):
    """chief_wiggum.hashing.hash_epic_definitions' original guard was
    `is_dir()` (never crashes on a path that exists but is a plain file);
    check_traceability's five original extractors used `exists()` (which
    would have gone on to call `.rglob()` on a non-directory). EpicModel
    adopts the stricter is_dir() guard for both cases — no test exercised the
    crash-prone path either way, so this is a documented safety improvement,
    not a parity break."""
    f = tmp_path / "not-a-dir"
    f.write_text("### CTR-x-001 — x\n")
    model = build_epic_model(f)
    assert model.files == {}


def test_build_epic_model_bundles_every_extractor_view(tmp_path):
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text(
        "### CTR-order-001 — valid range\n"
        "REQUIRES: start <= end\n"
        "@cw-trace realizes BR-order-001\n"
    )
    (epic / "invariants.md").write_text("### BR-order-001 — biz rule\n")
    (epic / "state-machines.json").write_text(json.dumps({
        "id": "CTR-order-002", "coverage_requires": ["unit-test", "probe"],
    }))
    # A malformed near-miss (two-segment INV-001) alongside a valid one.
    (epic / "extra.md").write_text("### INV-001 — malformed near miss\n")

    model = build_epic_model(epic)

    assert model.defined_ids["CTR-order-001"] == "CTR"
    assert model.defined_ids["BR-order-001"] == "BR"
    assert model.defined_ids["CTR-order-002"] == "CTR"
    assert "contracts.md" in model.id_bearing_artifacts
    assert "state-machines.json" in model.id_bearing_artifacts
    assert any(m["token"] == "INV-001" for m in model.malformed_ids)
    assert model.coverage_requirements["CTR-order-002"] == ["unit-test", "probe"]
    assert any(a.verb == "realizes" and a.target == "BR-order-001" for a in model.epic_annotations)
    assert "CTR-order-001" in model.definition_hashes
    assert "BR-order-001" in model.definition_hashes
    assert "CTR-order-002" in model.definition_hashes


def test_five_wrapper_functions_agree_with_the_model(tmp_path):
    """The five backward-compatible module-level functions
    (extract_defined_ids/find_id_bearing_artifacts/scan_malformed_ids/
    extract_coverage_requirements/scan_epic_annotations) must return EXACTLY
    what a directly-built EpicModel carries — they are thin wrappers now, not
    a second implementation."""
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.md").write_text(
        "### CTR-a-001 — a\nREQUIRES: x\n# @cw-trace guards CTR-a-001\n"
    )
    model = build_epic_model(epic)
    assert ct.extract_defined_ids(epic) == model.defined_ids
    assert ct.find_id_bearing_artifacts(epic) == model.id_bearing_artifacts
    assert ct.scan_malformed_ids(epic) == model.malformed_ids
    assert ct.extract_coverage_requirements(epic) == model.coverage_requirements
    assert ct.scan_epic_annotations(epic) == model.epic_annotations


def test_raw_definitions_includes_justifications_defined_ids_excludes_them(tmp_path):
    """The one deliberate divergence this refactor's docstring calls out:
    code_query._locate_definitions has ALWAYS included the justifications/
    subtree (a waiver's own "id" field is still a useful locator);
    check_traceability.extract_defined_ids has ALWAYS excluded it (a waiver
    must never phantom-define a new stable ID). EpicModel carries BOTH views
    from the SAME single read, rather than merging them into one and
    silently changing one consumer's behavior."""
    epic = tmp_path / "epic"
    (epic / "justifications").mkdir(parents=True)
    (epic / "contracts.md").write_text("### CTR-w-001 — real contract\nREQUIRES: x\n")
    (epic / "justifications" / "waiver.json").write_text(json.dumps({
        "id": "CTR-w-002", "reason": "not a real declaration", "ticket": "#1",
        "approver": "pat", "expiry": "2099-01-01",
    }))

    model = build_epic_model(epic)

    assert "CTR-w-001" in model.defined_ids
    assert "CTR-w-002" not in model.defined_ids  # excluded — extract_defined_ids parity

    raw_ids = {nid for nid, _rel, _line, _is_just in model.raw_definitions}
    assert "CTR-w-001" in raw_ids
    assert "CTR-w-002" in raw_ids  # INCLUDED — code_query._locate_definitions parity
    justified = {
        nid: is_just for nid, _rel, _line, is_just in model.raw_definitions
    }
    assert justified["CTR-w-002"] is True
    assert justified["CTR-w-001"] is False


# --- "one epic-tree walk per invocation" -------------------------------------


def _count_epic_reads(monkeypatch, epic_dir: Path) -> dict:
    """Instrument Path.read_text so every read of a file UNDER epic_dir is
    counted by its resolved path. Returns the live counts dict."""
    counts: dict[str, int] = {}
    orig_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        try:
            resolved = self.resolve()
        except OSError:
            resolved = self
        if epic_dir.resolve() in resolved.parents:
            key = str(resolved)
            counts[key] = counts.get(key, 0) + 1
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    return counts


def test_check_traceability_check_reads_each_epic_file_exactly_once(tmp_path, monkeypatch):
    """Acceptance criterion: check_traceability.py's full check() path used to
    walk+read the epic tree FIVE times (extract_defined_ids,
    find_id_bearing_artifacts, scan_malformed_ids,
    extract_coverage_requirements, scan_epic_annotations) — now exactly once."""
    epic = tmp_path / "docs" / "epics" / "exp"
    epic.mkdir(parents=True)
    (epic / "contracts.md").write_text(
        "### CTR-exp-001 — bound\nREQUIRES: x\n# @cw-trace realizes BR-exp-001\n"
    )
    (epic / "invariants.md").write_text("### BR-exp-001 — biz rule\n")

    counts = _count_epic_reads(monkeypatch, epic)
    ct.check(str(epic))

    assert counts, "no epic file reads were observed — instrumentation is broken"
    over_read = {k: v for k, v in counts.items() if v != 1}
    assert not over_read, f"epic file(s) read more than once: {over_read}"


def test_check_traceability_check_with_links_path_still_one_walk(tmp_path, monkeypatch):
    """The suspect-link branch (--links) used to call hash_epic_definitions a
    SECOND time on top of the five-extractor walk — also collapsed to the one
    shared EpicModel built at the top of check()."""
    epic = tmp_path / "docs" / "epics" / "exp"
    epic.mkdir(parents=True)
    (epic / "contracts.md").write_text("### CTR-exp-001 — bound\nREQUIRES: x\n")
    links_path = tmp_path / "docs" / "quality" / "trace-links.json"

    counts = _count_epic_reads(monkeypatch, epic)
    ct.check(str(epic), links_path=links_path)

    over_read = {k: v for k, v in counts.items() if v != 1}
    assert not over_read, f"epic file(s) read more than once: {over_read}"


def _score_ns(tmp_path, **overrides):
    base = dict(repo=str(tmp_path), no_tests=True, no_quality=True, venv=None, gobin=None)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_ratchet_score_reads_each_epic_file_exactly_once(tmp_path, monkeypatch):
    """Acceptance criterion: ratchet.py `score` used to walk cfg.epic_docs FOUR
    times (hash_epic_definitions' two rglob passes + find_id_bearing_artifacts
    + scan_malformed_ids) — now exactly once via one shared EpicModel."""
    epic_docs = tmp_path / "docs" / "epics"
    epic = epic_docs / "order-lifecycle"
    epic.mkdir(parents=True)
    (epic / "contracts.md").write_text(
        "### CTR-order-001 — valid date range\nREQUIRES: start_date <= end_date\n"
    )
    state = tmp_path / "docs" / "quality"
    state.mkdir(parents=True)
    (state / "ratchet.json").write_text(json.dumps({
        "suites": [], "epic_docs": "docs/epics",
        "protected_paths": ratchet.DEFAULT_PROTECTED,
    }))
    cfg = ratchet.load_config(tmp_path)

    counts = _count_epic_reads(monkeypatch, epic_docs)
    rc = ratchet.cmd_score(_score_ns(tmp_path))

    assert rc == 0
    assert counts, "no epic file reads were observed — instrumentation is broken"
    over_read = {k: v for k, v in counts.items() if v != 1}
    assert not over_read, f"epic file(s) read more than once: {over_read}"
    # sanity: the scorecard still measured the contract
    sc = json.loads(cfg.scorecard.read_text())
    assert "CTR-order-001" in sc["contract_hashes"]


# --- EpicModel is built fresh per invocation (no cross-process memoization) --


def test_code_query_reflects_epic_change_across_separate_invocations(tmp_path):
    """Two SEPARATE `code_query.py` subprocess invocations against a repo
    whose epic file changes between them must both see the CURRENT content —
    proving no on-disk/cross-process cache was introduced by this refactor
    (the code_query no-cross-query-memoization doctrine, extended to
    EpicModel: rebuilt on every process invocation, never persisted)."""
    epic = tmp_path / "docs" / "epics" / "exp"
    epic.mkdir(parents=True)
    (epic / "contracts.md").write_text(
        "### CTR-exp-001 — first version\nREQUIRES: v1 wording\n"
    )

    def run_contract_query():
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "code_query.py"), "--repo", str(tmp_path),
             "contract", "CTR-exp-001"],
            capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)

    first = run_contract_query()
    assert first["facts"], "first invocation found no fact for CTR-exp-001"
    first_statement = first["facts"][0]["statement"]

    (epic / "contracts.md").write_text(
        "### CTR-exp-001 — second version\nREQUIRES: v2 wording (changed)\n"
    )

    second = run_contract_query()
    assert second["facts"], "second invocation found no fact for CTR-exp-001"
    second_statement = second["facts"][0]["statement"]

    assert first_statement != second_statement, (
        "code_query.py's second invocation did not reflect the epic file "
        "change — EpicModel must be rebuilt fresh per process, never cached "
        "across invocations")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
