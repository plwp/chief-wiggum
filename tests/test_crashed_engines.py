"""#265: a debt engine that CRASHED must not render like an unsupported tier.

The failure this pins: jscpd exhausted a 4 GB V8 heap, the clone engine recorded
`{"skipped": "jscpd produced no report"}`, and every surfacing layer printed it
the same way it prints "this language has no dead-code tier". The inventory said
zero items and `/status` said nothing at all — a whole detection dimension died
in silence.

The marker's precision matters as much as its existence: an absent tool is a
declared limitation and must stay quiet, or the operator learns to ignore it.
"""

from __future__ import annotations

import json
from pathlib import Path

import debt_inventory
import status


def _quality_dir(tmp_path) -> Path:
    q = tmp_path / "docs" / "quality"
    q.mkdir(parents=True)
    return q


OOM_NOTE = ("<--- Last few GCs --->\n[.....] 176679 ms: Scavenge (interleaved) "
            "3930.2 (4120.4) -> 3930.1 (4121.4) MB, allocation failure;")


def _debt(engines: dict, items: list | None = None) -> str:
    return json.dumps({"items": items or [], "engines": engines,
                       "unscanned_languages": {}})


def test_crashed_engine_is_reported(tmp_path):
    q = _quality_dir(tmp_path)
    (q / "debt.json").write_text(_debt({"clones": {
        "engine": "clones", "status": "crashed",
        "crashed": "jscpd produced no report", "note": OOM_NOTE}}))
    crashed = status.debt_engines_crashed(q)
    assert set(crashed) == {"clones"}
    assert "jscpd produced no report" in crashed["clones"]
    assert "Last few GCs" in crashed["clones"]


def test_crash_is_reported_even_when_other_engines_found_things(tmp_path):
    """The bug's core shape. `debt_not_measured` / `debt_partial_coverage` are
    gated on a ZERO item count, so a crash alongside real findings from the
    other three engines would render as an ordinary, healthy inventory."""
    q = _quality_dir(tmp_path)
    (q / "debt.json").write_text(_debt(
        {"clones": {"status": "crashed", "crashed": "jscpd timed out after 600s"},
         "markers": {"engine": "markers", "files_scanned": 40}},
        items=[{"severity": "high"}, {"severity": "low"}],
    ))
    counts = status.debt_counts(q)
    assert counts  # non-empty: the #259 markers stay silent here, by design
    assert status.debt_not_measured(q, counts) is None
    assert status.debt_partial_coverage(q, counts) is None
    # ...and the crash is still reported.
    assert "clones" in status.debt_engines_crashed(q)


def test_absent_tool_is_never_reported_as_a_crash(tmp_path):
    """An unsupported/absent tier is a declared limitation. Over-claiming it as
    a crash is how a marker loses the operator's trust."""
    q = _quality_dir(tmp_path)
    (q / "debt.json").write_text(_debt({"clones": {
        "engine": "clones", "status": "skipped",
        "skipped": "jscpd/node not found", "note": "requires node + jscpd"}}))
    assert status.debt_engines_crashed(q) == {}


def test_healthy_and_absent_inventories_report_nothing(tmp_path):
    q = _quality_dir(tmp_path)
    assert status.debt_engines_crashed(q) == {}
    (q / "debt.json").write_text(_debt({"clones": {
        "engine": "clones", "status": "measured", "files_in_corpus": 61}}))
    assert status.debt_engines_crashed(q) == {}


def test_legacy_inventory_without_status_keys_is_quiet(tmp_path):
    """A pre-#265 debt.json has neither `status` nor `crashed`. It must not be
    retro-classified as crashed on the strength of a bare `skipped`."""
    q = _quality_dir(tmp_path)
    (q / "debt.json").write_text(_debt({"clones": {
        "engine": "clones", "skipped": "jscpd produced no report"}}))
    assert status.debt_engines_crashed(q) == {}


def test_rendered_status_surfaces_the_crash(tmp_path, monkeypatch):
    """AC2 (#265): visible in /status without reading the raw JSON."""
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))
    target = tmp_path / "target"
    q = _quality_dir(target)
    (q / "debt.json").write_text(_debt(
        {"clones": {"status": "crashed", "crashed": "jscpd produced no report",
                    "note": OOM_NOTE}},
        items=[{"severity": "high"}],
    ))
    st = status.gather(target)
    assert "clones" in st["crashed_engines"]
    text = status.render_text(st)
    assert "CRASHED: debt engine clones" in text
    assert "NOT measured" in text
    # the counts still render — the marker adds context, never hides data
    assert "high: 1" in text


def test_rendered_status_is_quiet_when_nothing_crashed(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))
    target = tmp_path / "target"
    q = _quality_dir(target)
    (q / "debt.json").write_text(_debt({"clones": {"status": "measured"}}))
    st = status.gather(target)
    assert st["crashed_engines"] == {}
    assert "CRASHED" not in status.render_text(st)


def test_inventory_report_distinguishes_crashed_from_skipped():
    """`format_report` printed both as `- clones: skipped — ...`."""
    envelope = {
        "authority": "report-only", "scope": "whole repo",
        "counts": {}, "unscanned_languages": {}, "items": [],
        "engines": {
            "clones": {"status": "crashed", "crashed": "jscpd produced no report",
                       "note": OOM_NOTE},
            "dead_code": {"skipped": "no dead-code tier for csharp"},
        },
    }
    text = debt_inventory.format_report(envelope)
    assert "clones: CRASHED — jscpd produced no report" in text
    assert "dimension NOT measured" in text
    assert "Last few GCs" in text
    # the genuinely-skipped tier keeps its calmer wording
    assert "dead_code: skipped — no dead-code tier for csharp" in text


def test_inventory_report_surfaces_a_corpus_fallback():
    """A widened corpus is allowed, but never silent."""
    envelope = {
        "authority": "report-only", "scope": "whole repo",
        "counts": {}, "unscanned_languages": {}, "items": [],
        "engines": {"clones": {
            "status": "measured",
            "corpus_fallback": "argv budget exceeded (99000 files) — scanned the "
                               "repo root instead; clone findings are NOT scope-narrowed"}},
    }
    text = debt_inventory.format_report(envelope)
    assert "argv budget exceeded" in text
    assert "NOT scope-narrowed" in text
