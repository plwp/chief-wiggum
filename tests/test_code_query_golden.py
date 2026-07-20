"""Golden-parity tests for code_query.py (#159) vs check_single_writer.py /
check_traceability.py, on the SAME fixture repo (tests/fixtures/code_query_repo).

code_query is a NEW locator on top of the two existing checkers' emission
functions — it must never invent a writer/annotation site or drop one the
checkers themselves would report. These tests recompute the checkers' facts
directly and assert code_query's facts are exactly consistent (same
file/line/field/sanctioned for writers; same file/line/verb for annotations).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import check_single_writer  # noqa: E402
import check_traceability  # noqa: E402
import code_query  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "code_query_repo"
EPIC_DIR = FIXTURE / "docs" / "epics" / "checkout"


def test_writers_verb_is_byte_parity_with_check_single_writer_report():
    expected = check_single_writer.check(EPIC_DIR, source_root=str(FIXTURE))
    assert expected.writers, "fixture sanity: expect writers to exist"
    assert expected.violations, "fixture sanity: expect at least one violation"

    env = code_query.cmd_writers(FIXTURE, "INV-checkout-001", "checkout", limit=100)
    got = {(f["file"], f["line"], f["field"], f["sanctioned"], f["symbol"], f["text"]) for f in env["facts"]}
    want = {(w["file"], w["line"], w["field"], w["sanctioned"], w["symbol"], w["text"]) for w in expected.writers}
    assert got == want


def test_writers_violations_match_check_single_writer_violations():
    expected = check_single_writer.check(EPIC_DIR, source_root=str(FIXTURE))
    env = code_query.cmd_writers(FIXTURE, "INV-checkout-001", "checkout", limit=100)
    got_violations = {(f["file"], f["line"]) for f in env["facts"] if not f["sanctioned"]}
    want_violations = {(v["file"], v["line"]) for v in expected.violations}
    assert got_violations == want_violations


def test_governs_field_mode_writers_match_check_single_writer():
    expected = check_single_writer.check(EPIC_DIR, source_root=str(FIXTURE))
    env = code_query.cmd_governs(FIXTURE, "order.status", "checkout", limit=100)
    got = {(f["file"], f["line"], f["sanctioned"]) for f in env["facts"] if f["kind"] == "writer"}
    want = {(w["file"], w["line"], w["sanctioned"]) for w in expected.writers}
    assert got == want


def test_guards_verb_matches_check_traceability_source_scan():
    all_anns = check_traceability.scan_source(str(FIXTURE))
    expected = {(a.file, a.line) for a in all_anns if a.verb in ("guards", "ensures") and a.target == "CTR-order-confirm-001"}
    assert expected, "fixture sanity"

    env = code_query.cmd_guards(FIXTURE, "CTR-order-confirm-001", "checkout")
    got = {tuple(f["handle"].rsplit(":", 1)) for f in env["facts"]}
    got = {(file, int(line)) for file, line in got}
    assert got == expected


def test_verifies_verb_matches_check_traceability_source_scan():
    all_anns = check_traceability.scan_source(str(FIXTURE))
    expected = {(a.file, a.line) for a in all_anns if a.verb == "verifies" and a.target == "INV-checkout-001"}
    assert expected, "fixture sanity"

    env = code_query.cmd_verifies(FIXTURE, "INV-checkout-001", "checkout")
    got = {tuple(f["handle"].rsplit(":", 1)) for f in env["facts"]}
    got = {(file, int(line)) for file, line in got}
    assert got == expected


def test_annotations_verb_union_matches_check_traceability_full_scan():
    """`annotations <ID>` with no --verb must return the UNION of every
    verb targeting that ID across BOTH epic docs (realizes) and source
    (guards/ensures/verifies) — exactly what check_traceability.check() joins
    internally to compute coverage."""
    epic_anns = check_traceability.scan_epic_annotations(str(EPIC_DIR))
    source_anns = check_traceability.scan_source(str(FIXTURE))
    expected_count = sum(1 for a in (epic_anns + source_anns) if a.target == "CTR-order-confirm-001")

    env = code_query.cmd_annotations(FIXTURE, "CTR-order-confirm-001", "checkout", None, limit=100)
    assert len(env["facts"]) == expected_count


def test_fixture_is_traceability_sound_and_reports_zero_gaps():
    """Sanity check on the fixture itself: check_traceability must report full
    soundness+coverage so the parity assertions above aren't accidentally
    passing against a broken fixture."""
    report = check_traceability.check(EPIC_DIR, source_root=str(FIXTURE))
    assert report.soundness_ok
    assert report.coverage_ok


# --- IT-fh-03: hotspot fact does not regress #185 (#187) -----------------------
#
# Fixture: tests/fixtures/code_query_repo/docs/quality/hotspots.json lists
# src/order.py (case d: also has a DIRECT @cw-trace annotation) and
# src/legacy_util.py (case a: no other bindings) as decile-10 hotspots, with
# src/coupled_partner.py as a coupled_with partner of src/order.py.
# src/order_summary.py (case c) lexically resembles "order" but is absent from
# the record. src/plain_file.py (case b) has no bindings and no record entry.


def test_orient_case_a_plain_hotspot_file_gets_exactly_one_measured_fact():
    """@cw-trace verifies CTR-fh-033 CTR-fh-034 INV-fh-007"""
    env = code_query.cmd_orient(FIXTURE, "src/legacy_util.py", "checkout")
    hotspot_facts = [f for f in env["facts"] if f["kind"] == "hotspot"]
    assert len(hotspot_facts) == 1
    fact = hotspot_facts[0]
    assert fact["relation"] == "measured"
    assert fact["provenance"]["generating_sha"] == "fixture0000000000000000000000000000000000"

    # `exact` is a ranking hint, never serialized — check it on the Fact object.
    raw_facts = code_query._hotspot_facts_for_file(FIXTURE, "src/legacy_util.py")
    assert len(raw_facts) == 1
    assert raw_facts[0].exact is True


def test_orient_case_b_plain_file_absent_from_record_gets_no_hotspot_fact():
    env = code_query.cmd_orient(FIXTURE, "src/plain_file.py", "checkout")
    assert not [f for f in env["facts"] if f["kind"] == "hotspot"]


def test_orient_case_c_lexically_similar_but_unlisted_file_gets_no_hotspot_fact():
    """The negative required by IT-fh-03: src/order_summary.py shares the word
    'order' with the hotspot src/order.py, but hotspot facts are EXACT
    path-membership only — never routed through the lexical matcher.

    @cw-trace verifies CTR-fh-034 INV-fh-012
    """
    env = code_query.cmd_orient(FIXTURE, "src/order_summary.py", "checkout")
    assert not [f for f in env["facts"] if f["kind"] == "hotspot"]


def test_orient_case_d_direct_annotation_sorts_before_hotspot_fact():
    """src/order.py carries BOTH a direct @cw-trace annotation AND hotspot
    membership. The direct fact must sort FIRST — the leading relation-tier
    rank key (direct=0 < measured=2), not exact-match or list-construction
    order.

    @cw-trace verifies CTR-fh-033 INV-fh-007
    """
    env = code_query.cmd_orient(FIXTURE, "src/order.py", "checkout")
    kinds_in_order = [f["kind"] for f in env["facts"]]
    assert "hotspot" in kinds_in_order
    hotspot_idx = kinds_in_order.index("hotspot")
    direct_idxs = [
        i for i, f in enumerate(env["facts"])
        if f.get("relation") == "direct" or f["kind"] in ("contract", "invariant")
    ]
    assert direct_idxs, env["facts"]
    assert max(direct_idxs) < hotspot_idx, env["facts"]

    direct_fact = code_query.Fact(
        kind="contract", id="CTR-order-confirm-001", statement="x", handle="h",
        epic="checkout", extra={"relation": "direct"}, exact=True, proximity=0,
    )
    measured_fact = code_query.Fact(
        kind="hotspot", id=None, statement="x", handle="h",
        epic=None, extra={"relation": "measured"}, exact=True, proximity=0,
    )
    assert code_query._rank_key(direct_fact, "orient")[0] == 0
    assert code_query._rank_key(measured_fact, "orient")[0] == 2


def test_orient_coupled_partner_of_top_decile_hotspot_gets_measured_fact():
    """A file that is not itself a top-decile hotspot, but IS listed as a
    coupled_with partner of one, also gets a measured fact — still exact
    membership (read off the OTHER record's coupled_with field), never a
    lexical guess."""
    env = code_query.cmd_orient(FIXTURE, "src/coupled_partner.py", "checkout")
    hotspot_facts = [f for f in env["facts"] if f["kind"] == "hotspot"]
    assert len(hotspot_facts) == 1
    assert hotspot_facts[0]["relation"] == "measured"
    assert hotspot_facts[0]["coupled_hotspot"] == "src/order.py"


def test_orient_hotspot_fact_never_uses_path_matches_literal_segments(monkeypatch):
    """Mechanical guard for the #187/#185 non-regression seam: hotspot fact
    construction must never call the lexical matcher.

    @cw-trace verifies CTR-fh-034 INV-fh-012
    """
    calls = []
    original = code_query._path_matches_literal_segments

    def spy(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(code_query, "_path_matches_literal_segments", spy)
    code_query._hotspot_facts_for_file(FIXTURE, "src/legacy_util.py")
    code_query._hotspot_facts_for_file(FIXTURE, "src/coupled_partner.py")
    assert calls == []
