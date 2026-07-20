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
    assert fact["invariants_touched"] == ["INV-checkout-001"]
    # Locator discipline: conditions/errors are AT MOST one summary line each.
    assert fact["preconditions"] == ["CTR-order-confirm-001-pre1: order status is pending"]
    assert fact["error_cases"] == ["409: order is already confirmed"]


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


def test_cli_nonexistent_epic_slug_is_usage_error():
    """Review issue 4: a missing --epic slug must be a usage error (exit 2),
    matching the other checkers — never a 'scanned, nothing governs' empty."""
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(FIXTURE), "--epic", "no-such-epic",
         "orient", "src/order.py"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "epic dir not found" in r.stderr


# --- two-plane locator discipline (review issue 1) ------------------------------------


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_strings(item)


def test_contract_and_state_facts_carry_no_structured_bodies_or_expressions():
    """Facts are locators: IDs + handles + one-line summaries at most. The
    machine `expression` bodies from contracts.json/state-machines.json must
    never appear in any verb's facts — only `show` serves declared content."""
    envs = [
        code_query.cmd_contract(FIXTURE, "POST /api/v1/orders/42/confirm", "checkout"),
        code_query.cmd_contract(FIXTURE, "CTR-order-confirm-001-pre1", "checkout"),
        code_query.cmd_state(FIXTURE, "confirmed", "checkout"),
        code_query.cmd_state(FIXTURE, "INV-checkout-001", "checkout"),
        code_query.cmd_orient(FIXTURE, "src/orders/confirm.py", "checkout"),
    ]
    for env in envs:
        for fact in env["facts"]:
            assert "expression" not in fact, fact
            # No nested dict-valued condition/error bodies anywhere in the fact
            # (writer facts embed check_single_writer's own flat site record,
            # which has no nested dicts either).
            for key in ("preconditions", "postconditions", "error_cases", "guards"):
                if key in fact:
                    assert all(isinstance(x, str) for x in fact[key]), (key, fact)
            # The known expression bodies must not leak in as strings either.
            for s in _walk_strings(fact):
                assert "order.status ==" not in s, fact


def test_orient_contract_operation_facts_are_counts_not_bodies():
    env = code_query.cmd_orient(FIXTURE, "src/orders/confirm.py", "checkout")
    op_facts = [f for f in env["facts"] if f["kind"] == "contract_operation"]
    assert op_facts, env
    fact = op_facts[0]
    assert fact["n_preconditions"] == 1
    assert fact["n_postconditions"] == 1
    assert fact["n_error_cases"] == 1
    assert "preconditions" not in fact


# --- all-words inferred binding (review issue 2) --------------------------------------


def test_inferred_binding_requires_all_literal_words():
    # Positive: src/orders/confirm.py covers ALL literal words of
    # /api/v1/orders/:id/confirm ({orders, confirm}) -> bound.
    env = code_query.cmd_orient(FIXTURE, "src/orders/confirm.py", "checkout")
    op_names = {f["statement"] for f in env["facts"] if f["kind"] == "contract_operation"}
    assert any("Confirm Order" in s for s in op_names)

    # Negative: ui/orders/page.tsx matches "orders" but NOT "confirm" -> must
    # NOT inherit the confirm operation specifically (it still gets its
    # ui-spec page, and — #185 fixture addition — its OWN "List Orders"
    # bare-entity operation, which is a genuine "orders" binding, not an
    # over-match: "orders" clears the corpus-specificity bar (CTR-fh-050)
    # because the fixture corpus also contains four unrelated "Provider"
    # operations that never mention it).
    env = code_query.cmd_orient(FIXTURE, "ui/orders/page.tsx", "checkout")
    op_names = {f["statement"] for f in env["facts"] if f["kind"] == "contract_operation"}
    assert not any("Confirm Order" in s for s in op_names), op_names
    assert any("List Orders" in s for s in op_names), op_names
    assert any(f["kind"] == "ui_component" for f in env["facts"])


# --- corpus-derived word specificity (#185 precision fix) -----------------------------
#
# Real-world trigger (dogeared-coach): `ui/src/providers/auth-provider.tsx` word-matched
# ~30 unrelated operations purely because "provider" is the entity name and recurs
# across nearly every Provider operation. The fixture's Provider entity (4 operations,
# 3 of which share ONLY the bare word "providers") + ui-spec route reproduces the same
# shape at small scale: "providers" clears >40% document-frequency in the fixture's own
# 8-document corpus (contracts.json operations + ui-spec.json routes), so it carries no
# binding weight on its own (CTR-fh-050/051, INV-fh-012).


def test_common_entity_word_alone_does_not_bind():
    """
    @cw-trace verifies CTR-fh-050 INV-fh-012
    """
    env = code_query.cmd_orient(FIXTURE, "ui/src/providers/auth-provider.tsx", "checkout")
    assert not any(f["kind"] == "contract_operation" for f in env["facts"]), env["facts"]
    assert not any(f["kind"] == "ui_component" for f in env["facts"]), env["facts"]


def test_entity_verb_combination_still_binds_despite_common_entity_word():
    """
    @cw-trace verifies CTR-fh-050 INV-fh-012
    """
    # "verify" is a specific (low document-frequency) word in the fixture corpus, so
    # the entity+verb combination still clears the specificity bar even though
    # "providers" alone would not.
    env = code_query.cmd_orient(FIXTURE, "ui/src/providers/verify-provider.tsx", "checkout")
    op_names = {f["statement"] for f in env["facts"] if f["kind"] == "contract_operation"}
    assert any("Verify Provider" in s for s in op_names), op_names
    # The bare "List Providers"/"Provider Plan"/"Schedule Provider" operations must
    # NOT also leak in just because "providers" happens to match too.
    assert not any("List Providers" in s for s in op_names), op_names


def test_orient_inferred_binding_is_deterministic_across_runs():
    """CTR-fh-051: same epic artifacts + same file => identical fact set and
    ordering across repeated calls (no dict/set-iteration-order dependence).

    @cw-trace verifies CTR-fh-051
    """
    first = code_query.cmd_orient(FIXTURE, "ui/src/providers/verify-provider.tsx", "checkout")
    for _ in range(5):
        again = code_query.cmd_orient(FIXTURE, "ui/src/providers/verify-provider.tsx", "checkout")
        assert again == first


# --- relation-tier-first rank key (CTR-fh-052/053, INV-fh-007/012) --------------------


def test_rank_key_relation_tier_is_leading_and_orders_direct_inferred_measured():
    """Unit-level guard on `_rank_key` itself: direct < inferred < measured,
    and this leading element dominates `exact` — a `measured` fact (the future
    #187 hotspot tier; no producer exists yet, only the tier) must never
    outrank a `direct` or `inferred` fact even when `exact=True`.

    @cw-trace verifies CTR-fh-052 CTR-fh-053 INV-fh-007
    """
    direct = code_query.Fact(
        kind="contract", id="CTR-x", statement="s", handle="h", epic="e",
        extra={"relation": "direct"}, exact=True, proximity=0,
    )
    inferred = code_query.Fact(
        kind="contract_operation", id=None, statement="s", handle="h", epic="e",
        extra={"relation": "inferred"}, exact=False, proximity=1,
    )
    measured = code_query.Fact(
        kind="hotspot", id=None, statement="s", handle="h", epic="e",
        extra={"relation": "measured"}, exact=True, proximity=1,
    )
    assert code_query._rank_key(direct, "orient")[0] == 0
    assert code_query._rank_key(inferred, "orient")[0] == 1
    assert code_query._rank_key(measured, "orient")[0] == 2

    ranked = code_query.rank_facts([measured, inferred, direct], "orient")
    assert ranked == [direct, inferred, measured]


def test_orient_ranks_direct_before_inferred_for_the_same_file():
    """Property/regression test (IT-fh-03-style, direct-vs-inferred tier
    boundary): src/orders/confirm_direct.py carries BOTH a direct @cw-trace
    annotation AND an artifact-derived inferred match on the same Confirm
    Order operation. `orient` must rank the direct fact first.

    @cw-trace verifies CTR-fh-052 INV-fh-007
    """
    env = code_query.cmd_orient(FIXTURE, "src/orders/confirm_direct.py", "checkout")
    relations = [f.get("relation") for f in env["facts"]]
    assert "direct" in relations and "inferred" in relations, env["facts"]
    assert relations.index("direct") < relations.index("inferred")


# --- every handle round-trips through show (review issue 3) ---------------------------


def _all_fixture_envelopes():
    return [
        code_query.cmd_orient(FIXTURE, "src/order.py", "checkout"),
        code_query.cmd_orient(FIXTURE, "src/orders/confirm.py", "checkout"),
        code_query.cmd_orient(FIXTURE, "ui/orders/page.tsx", "checkout"),
        code_query.cmd_governs(FIXTURE, "src/admin.py", "checkout"),
        code_query.cmd_governs(FIXTURE, "order.status", "checkout"),
        code_query.cmd_writers(FIXTURE, "INV-checkout-001", "checkout"),
        code_query.cmd_guards(FIXTURE, "CTR-order-confirm-001", "checkout"),
        code_query.cmd_verifies(FIXTURE, "CTR-order-confirm-001", "checkout"),
        code_query.cmd_annotations(FIXTURE, "BR-order-001", "checkout", None),
        code_query.cmd_trace(FIXTURE, "BR-order-001", "checkout"),
        code_query.cmd_trace(FIXTURE, "INV-checkout-001", "checkout"),
        code_query.cmd_contract(FIXTURE, "POST /api/v1/orders/42/confirm", "checkout"),
        code_query.cmd_contract(FIXTURE, "CTR-order-confirm-001-pre1", "checkout"),
        code_query.cmd_state(FIXTURE, "confirmed", "checkout"),
        code_query.cmd_state(FIXTURE, "INV-checkout-001", "checkout"),
        code_query.cmd_state(FIXTURE, "Order Status State Machine", "checkout"),
    ]


def test_every_emitted_handle_dereferences_through_show():
    """Property: every handle in every fact from every verb must round-trip
    through `show` — a handle that can't be dereferenced is a broken locator."""
    seen: set[str] = set()
    for env in _all_fixture_envelopes():
        for fact in env["facts"]:
            handle = fact["handle"]
            if handle in seen:
                continue
            seen.add(handle)
            shown = code_query.cmd_show(FIXTURE, handle, "checkout")
            assert not shown["summary"].startswith("unscanned:"), (handle, shown["summary"])
            assert shown["facts"], (handle, shown["summary"])
    assert len(seen) > 10  # sanity: the sweep actually exercised many handles


def test_show_dereferences_contract_operation_pseudo_handle():
    env = code_query.cmd_show(
        FIXTURE, "docs/epics/checkout/models/contracts.json#Order/Confirm Order", "checkout"
    )
    fact = env["facts"][0]
    block = "\n".join(fact["block"])
    assert '"method": "POST"' in block
    assert "order.status == 'pending'" in block  # show DOES serve the body


def test_show_dereferences_state_machine_bracket_handles():
    env = code_query.cmd_show(
        FIXTURE, "docs/epics/checkout/models/state-machines.json#invariants[INV-checkout-001]", "checkout"
    )
    fact = env["facts"][0]
    assert fact["id"] == "INV-checkout-001"
    assert '"controls_field"' in "\n".join(fact["block"])

    env = code_query.cmd_show(
        FIXTURE, "docs/epics/checkout/models/state-machines.json#transitions[pending->confirmed]", "checkout"
    )
    assert '"event": "confirm"' in "\n".join(env["facts"][0]["block"])


def test_show_unknown_fragment_is_genuine_empty_not_unscanned():
    env = code_query.cmd_show(
        FIXTURE, "docs/epics/checkout/models/contracts.json#Order/No Such Op", "checkout"
    )
    assert env["facts"] == []
    assert env["summary"].startswith("show: scanned")


def test_trace_derived_from_handle_names_the_declaring_model_file():
    env = code_query.cmd_trace(FIXTURE, "INV-checkout-001", "checkout")
    df_facts = [f for f in env["facts"] if f["kind"] == "derived_from"]
    assert df_facts, env
    handle = df_facts[0]["handle"]
    assert handle == "docs/epics/checkout/models/state-machines.json#INV-checkout-001"
    shown = code_query.cmd_show(FIXTURE, handle, "checkout")
    assert shown["facts"]
    assert shown["facts"][0]["id"] == "INV-checkout-001"


# --- scanner version inputs (review issue 6) ------------------------------------------


def test_scanner_version_includes_both_checker_sources():
    """The checkers' emissions define this tool's facts, so their source must
    be part of the version hash — pin the exact input list."""
    from chief_wiggum.hashing import scanner_version

    scripts = Path(code_query.__file__).resolve().parent
    cw = scripts / "chief_wiggum"
    expected = scanner_version(
        scripts / "code_query.py",
        scripts / "check_single_writer.py",
        scripts / "check_traceability.py",
        cw / "trace_ids.py", cw / "annotations.py",
        cw / "trace_emission.py", cw / "write_emission.py", cw / "languages.py",
        cw / "manifest.py", cw / "hashing.py",
    )
    assert code_query._scanner_version() == expected
