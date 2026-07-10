"""Tests for the state-machine transition-map verifier (verify_transitions.py).

Exercises the deterministic pure logic: entity-name extraction, CamelCase
conversion, Go source scanning for status writes, and the model-vs-code diff
that classifies transitions as covered / missing / undocumented.
"""

from __future__ import annotations

import json

import verify_transitions as vt

# --- camel_to_snake ---------------------------------------------------------


def test_camel_to_snake_basic():
    assert vt.camel_to_snake("CheckedIn") == "checked_in"
    assert vt.camel_to_snake("InProgress") == "in_progress"
    assert vt.camel_to_snake("Confirmed") == "confirmed"


def test_camel_to_snake_with_digits_and_acronyms():
    assert vt.camel_to_snake("Status2FA") == "status2_fa"
    assert vt.camel_to_snake("already_snake") == "already_snake"


# --- _extract_entity_name ---------------------------------------------------


def test_extract_entity_name_strips_suffixes():
    assert vt._extract_entity_name("Booking Status State Machine") == "Booking"
    assert vt._extract_entity_name("Order Lifecycle") == "Order"
    assert vt._extract_entity_name("Payment Status") == "Payment"


def test_extract_entity_name_multiword():
    assert vt._extract_entity_name("Support Ticket State Machine") == "SupportTicket"


# --- load_model -------------------------------------------------------------


def _write_model(tmp_path, name="Booking Status State Machine"):
    model = {
        "name": name,
        "states": {
            "pending": {"type": "initial"},
            "confirmed": {"type": "normal"},
            "checked_in": {"type": "terminal"},
        },
        "transitions": [
            {
                "from": "pending",
                "to": "confirmed",
                "event": "confirm",
                "derived_from": [
                    {"type": "ticket", "ref": "#42"},
                    {"type": "observed_fact", "ref": "obs-1"},
                ],
            },
            {"from": "confirmed", "to": "checked_in", "event": "check_in"},
        ],
    }
    path = tmp_path / "model.json"
    path.write_text(json.dumps(model))
    return path


def test_load_model_parses_entity_states_and_tickets(tmp_path):
    path = _write_model(tmp_path)
    entity, transitions, states = vt.load_model(path)

    assert entity == "Booking"
    assert states == {"pending", "confirmed", "checked_in"}
    assert len(transitions) == 2

    first = transitions[0]
    assert first.from_state == "pending"
    assert first.to_state == "confirmed"
    assert first.event == "confirm"
    # Only ticket-typed provenance entries become tickets.
    assert first.tickets == ["#42"]
    assert transitions[1].tickets == []


def test_load_model_falls_back_to_stem_for_missing_name(tmp_path):
    path = tmp_path / "widget.json"
    path.write_text(json.dumps({"states": {"a": {}}, "transitions": []}))
    entity, transitions, states = vt.load_model(path)
    # stem "widget" -> entity "Widget"
    assert entity == "Widget"
    assert transitions == []


# --- scan_go_files ----------------------------------------------------------


def test_scan_go_files_detects_status_assignment(tmp_path):
    (tmp_path / "handler.go").write_text(
        "package main\n"
        "func ConfirmBooking() {\n"
        '    update := bson.M{"$set": bson.M{"status": "confirmed"}}\n'
        "    _ = update\n"
        "}\n"
    )
    matches = vt.scan_go_files(tmp_path)
    targets = {m.target_status for m in matches}
    assert "confirmed" in targets
    m = next(m for m in matches if m.target_status == "confirmed")
    assert m.handler == "ConfirmBooking"
    assert m.file == "handler.go"


def test_scan_go_files_detects_models_status_constant(tmp_path):
    (tmp_path / "svc.go").write_text(
        "package main\n"
        "func CheckIn() {\n"
        "    booking.Status = models.BookingStatusCheckedIn\n"
        "}\n"
    )
    matches = vt.scan_go_files(tmp_path)
    assert any(m.target_status == "checked_in" for m in matches)


def test_scan_go_files_skips_test_files_and_vendor(tmp_path):
    (tmp_path / "x_test.go").write_text(
        'func T() { s := bson.M{"$set": bson.M{"status": "confirmed"}} ; _ = s }\n'
    )
    vendor = tmp_path / "vendor" / "lib"
    vendor.mkdir(parents=True)
    (vendor / "v.go").write_text(
        'func V() { s := bson.M{"$set": bson.M{"status": "confirmed"}} ; _ = s }\n'
    )
    matches = vt.scan_go_files(tmp_path)
    assert matches == []


def test_scan_go_files_ignores_commented_lines(tmp_path):
    (tmp_path / "c.go").write_text(
        "package main\n"
        "func F() {\n"
        '    // status = "confirmed" -- this is a comment\n'
        "}\n"
    )
    matches = vt.scan_go_files(tmp_path)
    assert matches == []


# --- diff_transitions -------------------------------------------------------


def _model_tx(frm, to, event="e", tickets=None):
    return vt.ModelTransition(from_state=frm, to_state=to, event=event, tickets=tickets or [])


def _code(target, file="f.go", line=1, handler="H", guards=None):
    return vt.CodeMatch(
        file=file,
        line=line,
        handler=handler,
        target_status=target,
        guard_statuses=guards or [],
    )


def test_diff_marks_transition_covered_when_code_matches():
    model = [_model_tx("pending", "confirmed", tickets=["#1"])]
    code = [_code("confirmed", handler="Confirm")]
    states = {"pending", "confirmed"}

    results, undocumented = vt.diff_transitions("Booking", model, code, states)

    assert len(results) == 1
    assert results[0].status == "covered"
    assert results[0].code_locations[0]["handler"] == "Confirm"
    assert undocumented == []


def test_diff_marks_transition_missing_when_no_code():
    model = [_model_tx("pending", "confirmed")]
    results, undocumented = vt.diff_transitions("Booking", model, [], {"pending", "confirmed"})
    assert results[0].status == "missing"
    assert results[0].code_locations == []


def test_diff_reports_undocumented_code_for_known_state():
    # Code writes "cancelled" but no model transition targets it.
    model = [_model_tx("pending", "confirmed")]
    code = [_code("cancelled", handler="Cancel")]
    states = {"pending", "confirmed", "cancelled"}

    results, undocumented = vt.diff_transitions("Booking", model, code, states)

    assert [r.status for r in results] == ["missing"]
    assert len(undocumented) == 1
    assert undocumented[0].to_state == "cancelled"
    assert undocumented[0].code_locations[0]["handler"] == "Cancel"


def test_diff_skips_undocumented_for_unknown_state():
    # A status written in code that isn't part of THIS entity's model is ignored
    # (likely a different entity's status).
    model = [_model_tx("pending", "confirmed")]
    code = [_code("shipped")]  # not in states
    states = {"pending", "confirmed"}

    _results, undocumented = vt.diff_transitions("Booking", model, code, states)
    assert undocumented == []


def test_diff_infers_from_state_from_single_guard():
    model = [_model_tx("pending", "confirmed")]
    code = [_code("cancelled", guards=["confirmed"])]
    states = {"pending", "confirmed", "cancelled"}

    _results, undocumented = vt.diff_transitions("Booking", model, code, states)
    assert undocumented[0].from_state == "confirmed"


def test_diff_ticket_filter_restricts_model_transitions():
    model = [
        _model_tx("a", "b", tickets=["#1"]),
        _model_tx("b", "c", tickets=["#2"]),
    ]
    code = [_code("b"), _code("c")]
    states = {"a", "b", "c"}

    results, _ = vt.diff_transitions("E", model, code, states, ticket_filter="#1")
    # Only the #1 transition is considered.
    assert len(results) == 1
    assert (results[0].from_state, results[0].to_state) == ("a", "b")


# --- format_text ------------------------------------------------------------


def test_format_text_summary_counts_and_percentage():
    results = [
        vt.TransitionResult("a", "b", "e", ["#1"], "covered",
                            code_locations=[{"file": "f.go", "line": 3, "handler": "H"}]),
        vt.TransitionResult("b", "c", "e", [], "missing"),
    ]
    undocumented = [
        vt.UndocumentedResult("*", "d", code_locations=[{"file": "g.go", "line": 9, "handler": "X"}]),
    ]
    out = vt.format_text("Booking", results, undocumented)

    assert "COVERED (1)" in out
    assert "MISSING (1)" in out
    assert "UNDOCUMENTED (1)" in out
    # 1 covered of 2 total -> 50% coverage
    assert "50% coverage" in out


def test_format_text_zero_transitions_is_safe():
    out = vt.format_text("Empty", [], [])
    assert "0 covered, 0 missing, 0 undocumented" in out
    assert "0% coverage" in out
