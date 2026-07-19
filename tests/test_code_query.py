"""Tests for scripts/code_query.py (#159) — the agent-facing architecture
knowledge CLI. Exercises each verb against tests/fixtures/code_query_repo, a
small "checkout" epic with:

- an annotated backend handler (src/order.py: confirm_order, @cw-trace
  guards/ensures CTR-order-confirm-001 + INV-checkout-001)
- an un-annotated frontend page (ui/orders/page.tsx) that must still resolve
  via ui-spec.json route matching (artifact-derived binding)
- a sanctioned writer (src/order.py) and an UNSANCTIONED writer
  (src/admin.py: admin_override_status) of the same single-write-path field
- a transition-map.json binding (covered + undocumented transition) on the
  same file as the annotation
- a fully-linked BR -> CTR -> code -> test slice for `trace`
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "code_query_repo"
SCRIPT = Path(__file__).parent.parent / "scripts" / "code_query.py"
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import code_query  # noqa: E402

ENVELOPE_KEYS = {"summary", "facts", "omitted", "cursor", "warnings", "provenance"}


def _envelope_shape_ok(env: dict) -> bool:
    return set(env.keys()) == ENVELOPE_KEYS


# --- envelope shape -----------------------------------------------------------


def test_every_verb_returns_the_specified_envelope_shape():
    envs = [
        code_query.cmd_orient(FIXTURE, "src/order.py", None),
        code_query.cmd_governs(FIXTURE, "src/order.py", None),
        code_query.cmd_writers(FIXTURE, "order.status", None),
        code_query.cmd_guards(FIXTURE, "CTR-order-confirm-001", None),
        code_query.cmd_verifies(FIXTURE, "CTR-order-confirm-001", None),
        code_query.cmd_annotations(FIXTURE, "BR-order-001", None, None),
        code_query.cmd_trace(FIXTURE, "BR-order-001", None),
        code_query.cmd_contract(FIXTURE, "CTR-order-confirm-001-pre1", None),
        code_query.cmd_state(FIXTURE, "confirmed", None),
        code_query.cmd_show(FIXTURE, "src/order.py:17", None),
    ]
    for env in envs:
        assert _envelope_shape_ok(env), env.keys()
        assert isinstance(env["facts"], list)
        assert isinstance(env["warnings"], list)


# --- orient ---------------------------------------------------------------------


def test_orient_binds_direct_annotation_writer_and_transition_map():
    env = code_query.cmd_orient(FIXTURE, "src/order.py", "checkout")
    kinds = {f["kind"] for f in env["facts"]}
    assert "contract" in kinds or "invariant" in kinds  # direct @cw-trace binding
    assert "writer" in kinds
    assert "transition" in kinds  # transition-map code_location, exact, un-annotated binding
    assert "transition_undocumented" in kinds  # ship_order drift signal

    direct_ids = {f["id"] for f in env["facts"] if f.get("kind") in ("contract", "invariant")}
    assert {"CTR-order-confirm-001", "INV-checkout-001"} <= direct_ids


def test_orient_frontend_file_binds_via_ui_spec_without_annotation():
    env = code_query.cmd_orient(FIXTURE, "ui/orders/page.tsx", "checkout")
    ui_facts = [f for f in env["facts"] if f["kind"] == "ui_component"]
    assert ui_facts, env
    assert ui_facts[0]["auth"] == "required"
    # Artifact-derived binding is inferred, not exact/annotation-derived.
    assert ui_facts[0]["relation"] == "inferred"


def test_orient_admin_file_surfaces_unsanctioned_writer_as_violation():
    env = code_query.cmd_orient(FIXTURE, "src/admin.py", "checkout")
    writer_facts = [f for f in env["facts"] if f["kind"] == "writer"]
    assert writer_facts
    assert writer_facts[0]["sanctioned"] is False


def test_orient_unscanned_for_nonexistent_path():
    env = code_query.cmd_orient(FIXTURE, "src/does_not_exist.py", None)
    assert env["facts"] == []
    assert env["summary"].startswith("unscanned:")
    assert any("unscanned" in w for w in env["warnings"])


def test_orient_genuine_empty_is_not_unscanned():
    # tests/test_checkout_order.py is a real, scanned file with no governing binding of
    # its own contract role beyond `verifies` (which `orient` doesn't surface as
    # a governing artifact for the file that DOES the verifying) — but it DOES
    # exist. Use a plain scanned file with no bindings at all instead.
    plain = FIXTURE / "src" / "util.py"
    plain.write_text('"""No bindings at all — a genuine scanned-and-empty case."""\n')
    try:
        env = code_query.cmd_orient(FIXTURE, "src/util.py", "checkout")
        assert env["facts"] == []
        assert env["summary"].startswith("orient: scanned")
        assert not any("unscanned" in w for w in env["warnings"])
    finally:
        plain.unlink()


# --- governs ----------------------------------------------------------------------


def test_governs_path_marks_direct_vs_inferred():
    env = code_query.cmd_governs(FIXTURE, "src/order.py", "checkout")
    relations = {f.get("relation") for f in env["facts"]}
    assert "direct" in relations


def test_governs_field_mode_returns_writers_and_field_contract():
    env = code_query.cmd_governs(FIXTURE, "order.status", None)
    kinds = {f["kind"] for f in env["facts"]}
    assert "writer" in kinds
    assert "field_contract" in kinds
    field_facts = [f for f in env["facts"] if f["kind"] == "field_contract"]
    assert field_facts[0]["source_of_truth"] == "orders collection"


def test_governs_unscanned_for_path_like_missing_target():
    env = code_query.cmd_governs(FIXTURE, "src/missing.py", None)
    assert env["summary"].startswith("unscanned:")


def test_governs_field_mode_genuine_empty_for_unknown_field():
    env = code_query.cmd_governs(FIXTURE, "shipping_address", None)
    assert env["facts"] == []
    assert not env["summary"].startswith("unscanned:")


# --- writers ------------------------------------------------------------------------


def test_writers_by_invariant_id_finds_sanctioned_and_unsanctioned():
    env = code_query.cmd_writers(FIXTURE, "INV-checkout-001", "checkout")
    sanctioned = [f for f in env["facts"] if f["sanctioned"]]
    unsanctioned = [f for f in env["facts"] if not f["sanctioned"]]
    assert sanctioned and unsanctioned
    assert any(f["file"] == "src/admin.py" for f in unsanctioned)


def test_writers_ranks_violations_before_sanctioned():
    env = code_query.cmd_writers(FIXTURE, "order.status", "checkout")
    sanctioned_flags = [f["sanctioned"] for f in env["facts"]]
    first_unsanctioned = sanctioned_flags.index(False)
    first_sanctioned = sanctioned_flags.index(True)
    assert first_unsanctioned < first_sanctioned


def test_writers_unknown_field_is_genuine_empty():
    env = code_query.cmd_writers(FIXTURE, "nonexistent_field", None)
    assert env["facts"] == []
    assert not env["summary"].startswith("unscanned:")
    assert any("not declared" in w for w in env["warnings"])


# --- guards / verifies / annotations --------------------------------------------------


def test_guards_matches_check_traceability_annotation_site():
    env = code_query.cmd_guards(FIXTURE, "CTR-order-confirm-001", "checkout")
    handles = {f["handle"] for f in env["facts"]}
    assert handles == {"src/order.py:13"}


def test_verifies_matches_check_traceability_annotation_site():
    env = code_query.cmd_verifies(FIXTURE, "CTR-order-confirm-001", "checkout")
    handles = {f["handle"] for f in env["facts"]}
    assert handles == {"tests/test_checkout_order.py:3"}


def test_annotations_finds_epic_doc_realizes_with_normalized_handle():
    env = code_query.cmd_annotations(FIXTURE, "BR-order-001", "checkout", None)
    assert env["facts"], env
    fact = env["facts"][0]
    assert fact["handle"] == "docs/epics/checkout/contracts.md:16"
    assert fact["verb"] == "realizes"


def test_annotations_verb_filter_narrows_results():
    env_all = code_query.cmd_annotations(FIXTURE, "CTR-order-confirm-001", "checkout", None)
    env_guards = code_query.cmd_annotations(FIXTURE, "CTR-order-confirm-001", "checkout", "guards")
    assert len(env_guards["facts"]) < len(env_all["facts"])
    assert all(f["verb"] == "guards" for f in env_guards["facts"])


# --- trace --------------------------------------------------------------------------


def test_trace_br_returns_full_realizes_guards_verifies_slice():
    env = code_query.cmd_trace(FIXTURE, "BR-order-001", "checkout")
    got_verbs = {f.get("verb") for f in env["facts"] if f.get("verb")}
    assert "realizes" in got_verbs
    assert "guards" in got_verbs or "ensures" in got_verbs
    assert "verifies" in got_verbs


# --- contract -----------------------------------------------------------------------


def test_contract_by_method_and_path_template():
    env = code_query.cmd_contract(FIXTURE, "POST /api/v1/orders/42/confirm", "checkout")
    assert env["facts"], env
    fact = env["facts"][0]
    assert fact["state_transition"] == "pending -> confirmed"
    assert fact["invariants_touched"][0]["id"] == "INV-checkout-001"


def test_contract_by_condition_id():
    env = code_query.cmd_contract(FIXTURE, "CTR-order-confirm-001-pre1", "checkout")
    assert env["facts"], env
    assert env["facts"][0]["statement"] == "order status is pending"


def test_contract_no_match_is_genuine_empty():
    env = code_query.cmd_contract(FIXTURE, "DELETE /api/v1/orders/1", "checkout")
    assert env["facts"] == []
    assert env["summary"].startswith("contract: scanned")


# --- state --------------------------------------------------------------------------


def test_state_by_state_id_returns_adjacency_and_invariants():
    env = code_query.cmd_state(FIXTURE, "confirmed", "checkout")
    kinds = {f["kind"] for f in env["facts"]}
    assert "transition" in kinds
    assert "invalid_transition" in kinds
    assert "invariant" in kinds
    inv_facts = [f for f in env["facts"] if f["kind"] == "invariant"]
    assert inv_facts[0]["id"] == "INV-checkout-001"


def test_state_by_invariant_id():
    env = code_query.cmd_state(FIXTURE, "INV-checkout-001", "checkout")
    assert env["facts"]
    assert env["facts"][0]["applies_to_states"] == ["confirmed"]


def test_state_by_machine_name():
    env = code_query.cmd_state(FIXTURE, "Order Status State Machine", "checkout")
    kinds = {f["kind"] for f in env["facts"]}
    assert "transition" in kinds and "invariant" in kinds


# --- show ---------------------------------------------------------------------------


def test_show_by_handle_returns_context_and_symbol():
    env = code_query.cmd_show(FIXTURE, "src/order.py:17", None)
    fact = env["facts"][0]
    assert 'order.status = "confirmed"' in fact["statement"]
    assert fact["symbol"] == "confirm_order"


def test_show_by_id_returns_declaration():
    env = code_query.cmd_show(FIXTURE, "CTR-order-confirm-001", "checkout")
    assert env["facts"]
    assert env["facts"][0]["handle"].startswith("docs/epics/checkout/contracts.md:")


def test_show_unscanned_for_missing_file_handle():
    env = code_query.cmd_show(FIXTURE, "src/missing.py:1", None)
    assert env["summary"].startswith("unscanned:")


# --- pagination ---------------------------------------------------------------------


def test_pagination_cursor_advances_through_all_facts():
    full = code_query.cmd_writers(FIXTURE, "INV-checkout-001", "checkout")
    total = len(full["facts"])
    assert total >= 2

    seen = []
    cursor = None
    for _ in range(total + 1):
        page = code_query.cmd_writers(FIXTURE, "INV-checkout-001", "checkout", limit=1, cursor=cursor)
        assert len(page["facts"]) <= 1
        seen.extend(page["facts"])
        cursor = page["cursor"]
        if cursor is None:
            break
    assert len(seen) == total


# --- CLI + telemetry ------------------------------------------------------------------


def test_cli_scanner_version_is_deterministic_hex():
    r1 = subprocess.run([sys.executable, str(SCRIPT), "--scanner-version"], capture_output=True, text=True)
    r2 = subprocess.run([sys.executable, str(SCRIPT), "--scanner-version"], capture_output=True, text=True)
    assert r1.returncode == 0
    assert r1.stdout.strip() == r2.stdout.strip()
    assert len(r1.stdout.strip()) == 64  # sha256 hex digest


def test_cli_missing_repo_is_usage_error():
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", "/no/such/repo", "orient", "x.py"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2


def test_cli_orient_json_output_end_to_end():
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(FIXTURE), "--epic", "checkout", "--format", "json",
         "orient", "src/order.py"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    import json
    env = json.loads(r.stdout)
    assert _envelope_shape_ok(env)
    assert env["facts"]


def test_cli_emits_query_telemetry_event(tmp_path, monkeypatch):
    log = tmp_path / "factory-log.jsonl"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(FIXTURE), "--epic", "checkout",
         "orient", "src/order.py"],
        capture_output=True, text=True,
        env={**__import__("os").environ, "CW_FACTORY_LOG": str(log)},
    )
    assert r.returncode == 0
    import json
    recs = [json.loads(line) for line in log.read_text().splitlines()]
    query_recs = [r for r in recs if r["event"] == "query"]
    assert query_recs
    assert query_recs[0]["verb"] == "orient"
    assert query_recs[0]["hit_count"] > 0
