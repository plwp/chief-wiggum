"""Tests for chief_wiggum.trace_links (#169): suspect-link propagation sidecar
+ JUSTIFIED waiver records.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from chief_wiggum import trace_links as tl  # noqa: E402


class _Ann:
    """Minimal stand-in for check_traceability.Annotation (duck-typed: the
    sidecar builder only reads these four attributes)."""

    def __init__(self, verb, target, file, line, source_kind):
        self.verb = verb
        self.target = target
        self.file = file
        self.line = line
        self.source_kind = source_kind


# --- sidecar build/load/write -------------------------------------------------


def test_build_sidecar_records_one_entry_per_known_target():
    anns = [
        _Ann("guards", "CTR-order-001", "order.py", 3, "code"),
        _Ann("verifies", "CTR-order-001", "test_order.py", 1, "test"),
        _Ann("guards", "CTR-ghost-999", "order.py", 9, "code"),  # unknown -> excluded
    ]
    hashes = {"CTR-order-001": "abc123"}
    body = tl.build_sidecar(anns, hashes)
    assert len(body["links"]) == 2
    assert all(link["definition_hash"] == "abc123" for link in body["links"])
    assert {link["target"] for link in body["links"]} == {"CTR-order-001"}


def test_build_sidecar_output_is_deterministically_ordered():
    anns = [
        _Ann("guards", "CTR-b-001", "b.py", 1, "code"),
        _Ann("guards", "CTR-a-001", "a.py", 1, "code"),
    ]
    hashes = {"CTR-a-001": "h1", "CTR-b-001": "h2"}
    body = tl.build_sidecar(anns, hashes)
    assert [link["file"] for link in body["links"]] == ["a.py", "b.py"]


def test_write_and_load_sidecar_round_trips(tmp_path):
    path = tmp_path / "docs" / "quality" / "trace-links.json"
    body = tl.build_sidecar(
        [_Ann("guards", "CTR-order-001", "order.py", 3, "code")],
        {"CTR-order-001": "abc123"},
    )
    tl.write_sidecar(path, body)
    loaded = tl.load_sidecar(path)
    assert loaded["links"] == body["links"]


def test_load_sidecar_missing_file_degrades_to_empty():
    assert tl.load_sidecar("/nonexistent/trace-links.json") == {"links": []}


def test_load_sidecar_malformed_json_degrades_to_empty(tmp_path):
    path = tmp_path / "trace-links.json"
    path.write_text("{not json")
    assert tl.load_sidecar(path) == {"links": []}


# --- suspect-link detection ----------------------------------------------------


def test_find_suspect_links_flags_changed_definition_hash():
    sidecar = {
        "links": [
            {"verb": "guards", "target": "CTR-order-001", "file": "order.py",
             "line": 3, "source_kind": "code", "definition_hash": "old-hash"},
        ]
    }
    suspect = tl.find_suspect_links(sidecar, {"CTR-order-001": "new-hash"})
    assert len(suspect) == 1
    assert suspect[0]["target"] == "CTR-order-001"
    assert suspect[0]["current_hash"] == "new-hash"


def test_find_suspect_links_clears_when_hash_matches():
    sidecar = {
        "links": [
            {"verb": "guards", "target": "CTR-order-001", "file": "order.py",
             "line": 3, "source_kind": "code", "definition_hash": "same-hash"},
        ]
    }
    assert tl.find_suspect_links(sidecar, {"CTR-order-001": "same-hash"}) == []


def test_find_suspect_links_ignores_targets_no_longer_defined():
    """A target that vanished entirely is dangling's problem, not suspect's —
    suspect means 'changed', not 'gone'."""
    sidecar = {
        "links": [
            {"verb": "guards", "target": "CTR-gone-001", "file": "order.py",
             "line": 3, "source_kind": "code", "definition_hash": "old-hash"},
        ]
    }
    assert tl.find_suspect_links(sidecar, {}) == []


def test_find_suspect_links_empty_sidecar_is_empty():
    assert tl.find_suspect_links({"links": []}, {"CTR-order-001": "h"}) == []


# --- JUSTIFIED waivers ----------------------------------------------------------


def test_load_justifications_reads_valid_records(tmp_path):
    epic = tmp_path / "order-lifecycle"
    jdir = epic / "justifications"
    jdir.mkdir(parents=True)
    (jdir / "ctr-order-002.json").write_text(json.dumps({
        "id": "CTR-order-002",
        "reason": "manual QA only, automated coverage tracked separately",
        "approver": "jane@example.com",
        "expiry": "2099-01-01",
        "ticket": "#170",
    }))
    justifications, invalid = tl.load_justifications(epic)
    assert invalid == []
    assert "CTR-order-002" in justifications
    j = justifications["CTR-order-002"]
    assert j.reason.startswith("manual QA")
    assert j.ticket == "#170"


def test_load_justifications_missing_dir_degrades_gracefully(tmp_path):
    justifications, invalid = tl.load_justifications(tmp_path / "no-such-epic")
    assert justifications == {}
    assert invalid == []


def test_load_justifications_without_ticket_ref_is_invalid(tmp_path):
    """A justification without a ticket ref is invalid — ticket-every-deferral."""
    epic = tmp_path / "epic"
    jdir = epic / "justifications"
    jdir.mkdir(parents=True)
    (jdir / "bad.json").write_text(json.dumps({
        "id": "CTR-order-002",
        "reason": "no ticket",
        "approver": "jane@example.com",
        "expiry": "2099-01-01",
    }))
    justifications, invalid = tl.load_justifications(epic)
    assert justifications == {}
    assert len(invalid) == 1
    assert "ticket" in invalid[0]["reason"]


def test_load_justifications_malformed_json_is_invalid(tmp_path):
    epic = tmp_path / "epic"
    jdir = epic / "justifications"
    jdir.mkdir(parents=True)
    (jdir / "bad.json").write_text("{not json")
    justifications, invalid = tl.load_justifications(epic)
    assert justifications == {}
    assert len(invalid) == 1
    assert "cannot parse" in invalid[0]["reason"]


def test_load_justifications_non_object_is_invalid(tmp_path):
    epic = tmp_path / "epic"
    jdir = epic / "justifications"
    jdir.mkdir(parents=True)
    (jdir / "bad.json").write_text(json.dumps(["not", "an", "object"]))
    justifications, invalid = tl.load_justifications(epic)
    assert justifications == {}
    assert len(invalid) == 1


def test_justification_expiry_check():
    j = tl.Justification(
        id="CTR-x-001", reason="r", approver="a", expiry="2020-01-01",
        ticket="#1", source="f.json",
    )
    assert j.is_expired(date(2026, 1, 1)) is True
    assert j.is_expired(date(2019, 1, 1)) is False


def test_justification_unparseable_expiry_treated_as_expired():
    j = tl.Justification(
        id="CTR-x-001", reason="r", approver="a", expiry="not-a-date",
        ticket="#1", source="f.json",
    )
    assert j.is_expired(date(2026, 1, 1)) is True


# --- ticket-ref validation (PR #181 review) ----------------------------------


def test_valid_ticket_ref_accepts_real_forms():
    for ref in (
        "#170",
        "plwp/chief-wiggum#12",
        "https://github.com/plwp/chief-wiggum/issues/170",
        "http://tracker.example.com/t/99",
        "PROJ-123",
        "  #170  ",  # surrounding whitespace is stripped
    ):
        assert tl.valid_ticket_ref(ref), ref


def test_valid_ticket_ref_rejects_placeholders():
    for ref in ("", "  ", "none", "N/A", "TBD", "yes", "ticket", "#", "PROJ-", "170"):
        assert not tl.valid_ticket_ref(ref), ref


def test_load_justifications_placeholder_ticket_is_invalid(tmp_path):
    """Regression: truthiness-only validation let '  '/'none'/'N/A' pass."""
    for i, placeholder in enumerate(("  ", "none", "N/A")):
        epic = tmp_path / f"epic-{i}"
        jdir = epic / "justifications"
        jdir.mkdir(parents=True)
        (jdir / "w.json").write_text(json.dumps({
            "id": "CTR-x-001", "reason": "r", "approver": "a",
            "expiry": "2099-01-01", "ticket": placeholder,
        }))
        justifications, invalid = tl.load_justifications(epic)
        assert justifications == {}, placeholder
        assert len(invalid) == 1, placeholder
        assert "ticket" in invalid[0]["reason"], placeholder


def test_load_justifications_canonicalizes_waiver_id(tmp_path):
    epic = tmp_path / "epic"
    jdir = epic / "justifications"
    jdir.mkdir(parents=True)
    (jdir / "w.json").write_text(json.dumps({
        "id": "CTR-BIL-001", "reason": "r", "approver": "a",
        "expiry": "2099-01-01", "ticket": "#1",
    }))
    justifications, invalid = tl.load_justifications(epic)
    assert invalid == []
    assert set(justifications) == {"CTR-bil-001"}
    assert justifications["CTR-bil-001"].id == "CTR-bil-001"
