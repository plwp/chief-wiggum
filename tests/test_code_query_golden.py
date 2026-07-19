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
