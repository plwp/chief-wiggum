"""Behavioural tests for formal_models.py's pure analysis + code generation.

The existing tests/test_formal_models.py smoke-tests the happy path on the
committed example. This file drives the deterministic logic with synthetic
models that isolate specific behaviours: schema detection, unreachable/dead
state detection, path enumeration with cycles, and the four code generators.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import formal_models as fm

# --- detect_schema_type -----------------------------------------------------


def test_detect_schema_type_variants():
    assert fm.detect_schema_type({"states": {}, "transitions": []}) == "state-machine"
    assert fm.detect_schema_type({"entities": [], "summary": {}}) == "transition-map"
    assert fm.detect_schema_type({"entities": []}) == "contracts"
    assert fm.detect_schema_type({"gaps": []}) == "gap"
    assert fm.detect_schema_type({"pages": [], "navigation": []}) == "ui-spec"
    assert fm.detect_schema_type({"id": "m", "states": {}}) == "xstate"


def test_detect_schema_type_unknown_raises():
    import pytest

    with pytest.raises(ValueError):
        fm.detect_schema_type({"foo": "bar"})


# --- analyze_graph ----------------------------------------------------------


def _sm(states, transitions, initial="a", **extra):
    return {"name": "M", "initial": initial, "states": states, "transitions": transitions, **extra}


def test_analyze_graph_flags_unreachable_state():
    sm = _sm(
        states={"a": {}, "b": {}, "orphan": {}},
        transitions=[{"from": "a", "to": "b", "event": "go"}],
    )
    analysis = fm.analyze_graph(sm)
    assert analysis.unreachable_states == {"orphan"}
    assert analysis.has_unreachable_states
    assert analysis.reachable_states == {"a", "b"}


def test_analyze_graph_flags_dead_state():
    # 'b' is non-terminal with no outgoing transitions -> dead.
    sm = _sm(
        states={"a": {}, "b": {}},
        transitions=[{"from": "a", "to": "b", "event": "go"}],
    )
    analysis = fm.analyze_graph(sm)
    assert analysis.dead_states == {"b"}
    assert analysis.has_dead_states


def test_analyze_graph_terminal_state_is_not_dead():
    sm = _sm(
        states={"a": {}, "done": {"type": "terminal"}},
        transitions=[{"from": "a", "to": "done", "event": "finish"}],
    )
    analysis = fm.analyze_graph(sm)
    assert analysis.dead_states == set()
    assert analysis.terminal_states == ["done"]
    assert analysis.all_terminals_reachable


def test_analyze_graph_unreachable_terminal_flagged():
    sm = _sm(
        states={"a": {}, "b": {}, "done": {"type": "terminal"}},
        transitions=[{"from": "a", "to": "b", "event": "go"}],
    )
    analysis = fm.analyze_graph(sm)
    assert not analysis.all_terminals_reachable
    assert "done" in analysis.unreachable_states


def test_analyze_graph_counts():
    sm = _sm(
        states={"a": {}, "b": {}, "done": {"type": "terminal"}},
        transitions=[
            {"from": "a", "to": "b", "event": "go"},
            {"from": "b", "to": "done", "event": "finish"},
        ],
        invariants=[{"id": "INV-1", "description": "x"}],
    )
    analysis = fm.analyze_graph(sm)
    assert analysis.transition_count == 2
    assert analysis.invariant_count == 1


# --- enumerate_paths --------------------------------------------------------


def test_enumerate_paths_finds_all_simple_paths():
    sm = _sm(
        states={"a": {}, "b": {}, "c": {}, "done": {"type": "terminal"}},
        transitions=[
            {"from": "a", "to": "b", "event": "e1"},
            {"from": "a", "to": "c", "event": "e2"},
            {"from": "b", "to": "done", "event": "e3"},
            {"from": "c", "to": "done", "event": "e4"},
        ],
    )
    paths = fm.enumerate_paths(sm)
    assert len(paths) == 2
    # Each path ends at a transition into the terminal state.
    for path in paths:
        assert path[-1]["next_state"] == "done"


def test_enumerate_paths_does_not_loop_on_cycle():
    # a <-> b cycle, plus an exit to done. Simple-path DFS must terminate.
    sm = _sm(
        states={"a": {}, "b": {}, "done": {"type": "terminal"}},
        transitions=[
            {"from": "a", "to": "b", "event": "e1"},
            {"from": "b", "to": "a", "event": "e2"},
            {"from": "b", "to": "done", "event": "e3"},
        ],
    )
    paths = fm.enumerate_paths(sm)
    # a->b->done is the only simple path (a->b->a->... is pruned as visited).
    assert len(paths) == 1
    events = [step["event"] for step in paths[0]]
    assert events == ["e1", "e3"]


def test_enumerate_paths_captures_guard_descriptions():
    sm = _sm(
        states={"a": {}, "done": {"type": "terminal"}},
        transitions=[
            {
                "from": "a",
                "to": "done",
                "event": "finish",
                "guards": [{"description": "payment settled"}],
            }
        ],
    )
    paths = fm.enumerate_paths(sm)
    assert paths[0][0]["guards"] == ["payment settled"]


# --- to_xstate --------------------------------------------------------------


def test_to_xstate_marks_terminal_as_final_and_lowercases_id():
    sm = _sm(
        states={"a": {}, "done": {"type": "terminal"}},
        transitions=[{"from": "a", "to": "done", "event": "GO"}],
    )
    sm["name"] = "My Machine"
    xs = fm.to_xstate(sm)
    assert xs["id"] == "my_machine"
    assert xs["initial"] == "a"
    assert xs["states"]["done"]["type"] == "final"
    assert xs["states"]["a"]["on"]["GO"] == {"target": "done"}


def test_to_xstate_multiple_transitions_same_event_become_array():
    sm = _sm(
        states={"a": {}, "b": {}, "c": {}},
        transitions=[
            {"from": "a", "to": "b", "event": "SUBMIT", "guards": [{"id": "g1", "description": "d"}]},
            {"from": "a", "to": "c", "event": "SUBMIT", "guards": [{"id": "g2", "description": "d"}]},
        ],
    )
    xs = fm.to_xstate(sm)
    submit = xs["states"]["a"]["on"]["SUBMIT"]
    assert isinstance(submit, list)
    assert {t["target"] for t in submit} == {"b", "c"}
    assert {t["guard"] for t in submit} == {"g1", "g2"}


def test_to_xstate_actions_and_entry_exit():
    sm = _sm(
        states={
            "a": {"entry_actions": ["log_entry"], "exit_actions": ["log_exit"]},
            "b": {},
        },
        transitions=[{"from": "a", "to": "b", "event": "GO", "actions": ["notify"]}],
    )
    xs = fm.to_xstate(sm)
    assert xs["states"]["a"]["entry"] == [{"type": "log_entry"}]
    assert xs["states"]["a"]["exit"] == [{"type": "log_exit"}]
    assert xs["states"]["a"]["on"]["GO"]["actions"] == [{"type": "notify"}]


# --- generate_hypothesis ----------------------------------------------------


def test_generate_hypothesis_emits_rule_per_transition():
    sm = _sm(
        states={"a": {}, "done": {"type": "terminal"}},
        transitions=[{"from": "a", "to": "done", "event": "finish"}],
        invariants=[{"id": "INV-1", "description": "always true", "expression": "x > 0"}],
    )
    sm["name"] = "Order Flow"
    code = fm.generate_hypothesis(sm)
    assert "class OrderFlow(RuleBasedStateMachine):" in code
    assert "def transition_a_to_done_via_finish(self):" in code
    assert 'self.state = "done"' in code
    assert "def check_inv_1(self):" in code
    assert "TestStateMachine = OrderFlow.TestCase" in code
    # Generated source must be syntactically valid Python.
    compile(code, "<generated>", "exec")


def test_generate_hypothesis_emits_invalid_transition_assertions():
    sm = _sm(
        states={"a": {}, "b": {}},
        transitions=[{"from": "a", "to": "b", "event": "go"}],
        invalid_transitions=[{"from": "b", "to": "a", "reason": "no going back"}],
    )
    code = fm.generate_hypothesis(sm)
    assert "def invalid_b_to_a(self):" in code
    assert "no going back" in code
    compile(code, "<generated>", "exec")


# --- generate_deal_decorators / guards --------------------------------------


def _contracts():
    return {
        "entities": [
            {
                "name": "Order",
                "invariants": [{"description": "total >= 0", "expression": "total >= 0"}],
                "operations": [
                    {
                        "name": "Create Order",
                        "method": "POST",
                        "path": "/orders",
                        "description": "Create an order",
                        "preconditions": [
                            {"description": "customer exists", "expression": "customer is not None"}
                        ],
                        "postconditions": [
                            {"description": "order persisted", "expression": "result.id is not None"}
                        ],
                        "error_cases": [{"status": 404, "condition": "customer not found"}],
                    }
                ],
            }
        ]
    }


def test_generate_deal_decorators_emits_pre_post():
    code = fm.generate_deal_decorators(_contracts())
    assert "import deal" in code
    assert "@deal.pre(lambda: customer is not None" in code
    assert "@deal.post(lambda result: result.id is not None" in code
    assert "def create_order(request):" in code
    compile(code, "<generated>", "exec")


def test_generate_guards_python_maps_error_status_from_condition():
    code = fm.generate_guards_python(_contracts())
    assert "def create_order(request):" in code
    assert "if not (customer is not None):" in code
    # "customer exists" precondition shares the word "customer" with the
    # error condition "customer not found" -> status 404 is selected.
    assert "raise HTTPError(404" in code
    compile(code, "<generated>", "exec")


def test_generate_guards_python_defaults_to_400_when_no_match():
    contracts = {
        "entities": [
            {
                "name": "X",
                "operations": [
                    {
                        "name": "Do Thing",
                        "method": "POST",
                        "path": "/x",
                        "preconditions": [
                            {"description": "flag set", "expression": "flag"}
                        ],
                        "error_cases": [{"status": 409, "condition": "totally unrelated wording"}],
                    }
                ],
            }
        ]
    }
    code = fm.generate_guards_python(contracts)
    assert "raise HTTPError(400" in code


def test_generate_guards_go_emits_capitalized_func_and_http_error():
    code = fm.generate_guards_go(_contracts())
    assert "package handlers" in code
    assert "func CreateOrder(w http.ResponseWriter, r *http.Request) {" in code
    assert 'http.Error(w, "customer exists", 404)' in code


# --- #232: non_codegen awareness, pseudo-operator transpile, slugified names --


def test_is_non_codegen_detects_prefix():
    assert fm.is_non_codegen("non_codegen(archguard): no controller imports repository directly")
    assert fm.is_non_codegen("  non_codegen(index): composite index exists")
    assert not fm.is_non_codegen("order.status == 'pending'")
    assert not fm.is_non_codegen(None)
    assert not fm.is_non_codegen("")


def test_transpile_expression_implies_word_form():
    assert fm.transpile_expression("A implies B") == "(not (A)) or (B)"


def test_transpile_expression_implies_unicode_arrow():
    assert fm.transpile_expression("A ⇒ B") == "(not (A)) or (B)"


def test_transpile_expression_times_unicode_operator():
    assert fm.transpile_expression("A × B") == "A * B"


def test_transpile_expression_delta_unicode_operator():
    # Δ ("change from A to B") transpiles to a plain subtraction so the
    # generated guard is valid Python/Go arithmetic.
    result = fm.transpile_expression("A Δ B")
    compile(result, "<generated>", "eval")
    assert "Δ" not in result


def test_transpile_expression_passthrough_when_no_pseudo_operators():
    assert fm.transpile_expression("order.status == 'pending'") == "order.status == 'pending'"


def test_slugify_identifier_strips_apostrophes_and_punctuation():
    slug = fm.slugify_identifier("What's Biting — Aggregation (v2)")
    assert slug.isidentifier()
    assert "'" not in slug
    assert "-" not in slug
    assert "(" not in slug and ")" not in slug


def test_slugify_identifier_guards_leading_digit():
    slug = fm.slugify_identifier("2FA Challenge")
    assert slug.isidentifier()
    assert not slug[0].isdigit()


def test_slugify_identifier_pascal_case_for_go():
    slug = fm.slugify_identifier("admin what's-biting aggregation", pascal_case=True)
    assert slug.isidentifier()
    assert slug[0].isupper()
    assert "'" not in slug and "-" not in slug


def _contracts_with(preconditions=None, postconditions=None, op_name="Create Order", extra_op=None):
    op = {
        "name": op_name,
        "method": "POST",
        "path": "/orders",
        "description": "Create an order",
        "preconditions": preconditions or [],
        "postconditions": postconditions or [],
        "error_cases": [{"status": 404, "condition": "customer not found"}],
    }
    if extra_op:
        op.update(extra_op)
    return {"entities": [{"name": "Order", "operations": [op]}]}


def test_generate_deal_decorators_skips_non_codegen_precondition():
    contracts = _contracts_with(
        preconditions=[
            {
                "description": "composite index exists on (tenant_id, status)",
                "expression": "non_codegen(index): composite index exists on (tenant_id, status)",
            }
        ]
    )
    code = fm.generate_deal_decorators(contracts)
    assert "@deal.pre(lambda: non_codegen" not in code
    assert "non_codegen" in code  # surfaced as a comment, not dropped silently
    compile(code, "<generated>", "exec")


def test_generate_guards_python_skips_non_codegen_precondition():
    contracts = _contracts_with(
        preconditions=[
            {
                "description": "archguard: no controller imports repository directly",
                "expression": "non_codegen(archguard): no controller imports repository directly",
            }
        ]
    )
    code = fm.generate_guards_python(contracts)
    assert "if not (non_codegen" not in code
    assert "non_codegen" in code
    compile(code, "<generated>", "exec")


def test_generate_guards_go_skips_non_codegen_precondition():
    contracts = _contracts_with(
        preconditions=[
            {
                "description": "provisioning: bucket must pre-exist",
                "expression": "non_codegen(provisioning): bucket must pre-exist",
            }
        ]
    )
    code = fm.generate_guards_go(contracts)
    assert "if !(non_codegen" not in code
    assert "non_codegen" in code


def test_generate_deal_decorators_transpiles_pseudo_operators():
    contracts = _contracts_with(
        preconditions=[{"description": "conditional grant", "expression": "has_role implies has_scope"}],
        postconditions=[{"description": "totals reconcile", "expression": "before Δ after"}],
    )
    code = fm.generate_deal_decorators(contracts)
    assert "(not (has_role)) or (has_scope)" in code
    assert "Δ" not in code
    compile(code, "<generated>", "exec")


def test_generate_guards_python_transpiles_unicode_operators():
    contracts = _contracts_with(
        preconditions=[{"description": "scope check", "expression": "has_role ⇒ has_scope"}]
    )
    code = fm.generate_guards_python(contracts)
    assert "⇒" not in code
    assert "(not (has_role)) or (has_scope)" in code
    compile(code, "<generated>", "exec")


def test_generate_guards_go_transpiles_unicode_operators():
    contracts = _contracts_with(
        preconditions=[{"description": "rate check", "expression": "cost × quantity"}]
    )
    code = fm.generate_guards_go(contracts)
    assert "×" not in code
    assert "cost * quantity" in code


def test_generate_deal_decorators_slugifies_operation_name_with_apostrophe_and_parens():
    contracts = _contracts_with(op_name="What's Biting — Aggregation (v2)")
    code = fm.generate_deal_decorators(contracts)
    compile(code, "<generated>", "exec")
    assert "def what's" not in code.lower()


def test_generate_guards_python_slugifies_operation_name():
    contracts = _contracts_with(op_name="Admin What's-Biting Aggregation")
    code = fm.generate_guards_python(contracts)
    compile(code, "<generated>", "exec")


def test_generate_guards_go_slugifies_operation_name_with_apostrophe_and_em_dash():
    contracts = _contracts_with(op_name="Admin What's-Biting — Aggregation")
    code = fm.generate_guards_go(contracts)
    # Must be a single legal Go identifier: no apostrophes, hyphens, em-dashes, or spaces.
    func_line = next(line for line in code.splitlines() if line.startswith("func "))
    func_name = func_line.split("func ")[1].split("(")[0]
    assert func_name.isidentifier()


def test_generated_guards_go_compiles_with_gofmt():
    if not shutil.which("gofmt"):
        return
    contracts = _contracts_with(
        preconditions=[
            {"description": "scope check", "expression": "has_role ⇒ has_scope"},
            {
                "description": "provisioning check",
                "expression": "non_codegen(provisioning): bucket must pre-exist",
            },
        ],
        op_name="Admin What's-Biting — Aggregation (v2)",
    )
    code = fm.generate_guards_go(contracts)
    with tempfile.TemporaryDirectory() as tmp:
        go_file = Path(tmp) / "guards.go"
        go_file.write_text(code)
        result = subprocess.run(
            ["gofmt", "-e", str(go_file)], capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, result.stderr
