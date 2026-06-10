import json
from pathlib import Path

import formal_models as fm

EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "formal-methods" / "examples"


def load_example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


def test_state_machine_example_validates_and_analyzes():
    model = load_example("order-lifecycle.state-machine.json")

    assert fm.detect_schema_type(model) == "state-machine"
    assert fm.validate(model) == []

    analysis = fm.analyze_graph(model)
    assert analysis.initial == "draft"
    assert not analysis.has_unreachable_states
    assert analysis.all_terminals_reachable
    assert analysis.transition_count > 0


def test_contract_example_validates():
    contracts = load_example("order-lifecycle.contracts.json")

    assert fm.detect_schema_type(contracts) == "contracts"
    assert fm.validate(contracts) == []


def test_xstate_conversion_preserves_initial_and_final_states():
    model = load_example("order-lifecycle.state-machine.json")
    xstate = fm.to_xstate(model)

    assert xstate["initial"] == model["initial"]
    terminal_states = {
        state for state, definition in model["states"].items() if definition.get("type") == "terminal"
    }
    assert terminal_states
    for state in terminal_states:
        assert xstate["states"][state]["type"] == "final"
