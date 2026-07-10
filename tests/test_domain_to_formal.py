"""Tests for domain_to_formal.py — the duplicat-rex DomainModel → formal model bridge.

All logic here is pure transformation (dict in, dict out), so these are direct
behavioural assertions on synthetic domain-model fragments.
"""

from __future__ import annotations

import domain_to_formal as d2f

# --- entity_to_contracts ----------------------------------------------------


def test_entity_to_contracts_maps_field_types_and_required():
    entity = {
        "fields": {
            "name": {"field_type": "string", "required": True},
            "count": {"field_type": "integer", "required": False},
            "ratio": {"field_type": "float"},
        }
    }
    contracts = d2f.entity_to_contracts(entity, "Widget")
    by_name = {f["name"]: f for f in contracts["fields"]}

    assert contracts["name"] == "Widget"
    assert by_name["name"]["type"] == "string"
    assert by_name["name"]["required"] == "always"
    assert by_name["count"]["type"] == "integer"
    assert by_name["count"]["required"] == "optional"
    # float maps to "number"
    assert by_name["ratio"]["type"] == "number"


def test_entity_to_contracts_enum_notes_and_unique_immutable():
    entity = {
        "fields": {
            "status": {"field_type": "enum", "enum_values": ["open", "closed"]},
            "slug": {"field_type": "string", "unique": True},
        }
    }
    contracts = d2f.entity_to_contracts(entity, "Ticket")
    by_name = {f["name"]: f for f in contracts["fields"]}

    assert "Valid values: open, closed" in by_name["status"]["notes"]
    assert by_name["slug"]["immutable"] is True


def test_entity_to_contracts_relation_becomes_objectid_with_source():
    entity = {"fields": {"owner": {"field_type": "relation", "related_entity": "User"}}}
    contracts = d2f.entity_to_contracts(entity, "Post")
    owner = contracts["fields"][0]
    assert owner["type"] == "ObjectID"
    assert owner["source_of_truth"] == "users collection"


def test_entity_to_contracts_limits_evidence_to_three():
    entity = {
        "fields": {
            "x": {"field_type": "string", "evidence": ["e1", "e2", "e3", "e4", "e5"]},
        }
    }
    contracts = d2f.entity_to_contracts(entity, "E")
    derived = contracts["fields"][0]["derived_from"]
    assert len(derived) == 3
    assert [d["ref"] for d in derived] == ["e1", "e2", "e3"]


def test_entity_to_contracts_operation_preconditions_and_required_fields():
    entity = {
        "fields": {},
        "operations": [
            {
                "name": "create",
                "method": "POST",
                "endpoint_pattern": "/widgets",
                "preconditions": ["user is authenticated"],
                "required_fields": ["name"],
                "postconditions": ["widget is persisted"],
                "error_cases": [{"status": 409, "condition": "duplicate"}],
            }
        ],
    }
    contracts = d2f.entity_to_contracts(entity, "Widget")
    op = contracts["operations"][0]

    assert op["name"] == "Create Widget"
    assert op["method"] == "POST"
    assert op["path"] == "/widgets"
    # One authored precondition + one required-field precondition.
    descs = [p["description"] for p in op["preconditions"]]
    assert "user is authenticated" in descs
    assert "name is provided" in descs
    assert op["postconditions"][0]["description"] == "widget is persisted"
    assert op["error_cases"][0] == {"status": 409, "condition": "duplicate"}


# --- entity_to_state_machine ------------------------------------------------


def test_entity_to_state_machine_none_without_states_or_transitions():
    assert d2f.entity_to_state_machine({"states": [], "transitions": []}, "X") is None
    assert d2f.entity_to_state_machine({"states": ["a"], "transitions": []}, "X") is None


def test_entity_to_state_machine_classifies_initial_and_terminal():
    entity = {
        "states": ["draft", "active", "closed"],
        "transitions": [
            {"from_state": "draft", "to_state": "active", "operation": "activate"},
            {"from_state": "active", "to_state": "closed", "operation": "close"},
        ],
    }
    sm = d2f.entity_to_state_machine(entity, "Deal")
    assert sm["initial"] == "draft"
    assert sm["states"]["draft"]["type"] == "initial"
    assert sm["states"]["active"]["type"] == "normal"
    # "closed" has no outgoing transition -> terminal.
    assert sm["states"]["closed"]["type"] == "terminal"


def test_entity_to_state_machine_terminal_state_gets_invalid_out_transitions():
    entity = {
        "states": ["open", "done"],
        "transitions": [{"from_state": "open", "to_state": "done", "operation": "finish"}],
    }
    sm = d2f.entity_to_state_machine(entity, "Job")
    invalids = sm.get("invalid_transitions", [])
    # 'done' is terminal, so a done->open transition must be flagged invalid.
    assert any(iv["from"] == "done" and iv["to"] == "open" for iv in invalids)


def test_entity_to_state_machine_guards_from_operation_preconditions():
    entity = {
        "states": ["open", "done"],
        "transitions": [{"from_state": "open", "to_state": "done", "operation": "finish"}],
        "operations": [
            {"name": "finish", "preconditions": ["all tasks complete"]},
        ],
    }
    sm = d2f.entity_to_state_machine(entity, "Job")
    tx = sm["transitions"][0]
    assert tx["guards"][0]["description"] == "all tasks complete"


# --- convert_domain_model ---------------------------------------------------


def test_convert_domain_model_produces_contracts_and_state_machines():
    dm = {
        "version": 2,
        "target": "acme/app",
        "entities": {
            "Board": {
                "fields": {"title": {"field_type": "string", "required": True}},
                "states": ["draft", "published"],
                "transitions": [
                    {"from_state": "draft", "to_state": "published", "operation": "publish"}
                ],
            },
            "Tag": {
                "fields": {"label": {"field_type": "string"}},
                # no states -> no state machine
            },
        },
    }
    contracts, state_machines = d2f.convert_domain_model(dm)

    assert {e["name"] for e in contracts["entities"]} == {"Board", "Tag"}
    # Only Board (which has states+transitions) yields a state machine.
    assert len(state_machines) == 1
    assert state_machines[0]["name"] == "Board Lifecycle"
    # Provenance references the domain model version + target.
    assert contracts["derived_from"][0]["ref"] == "domain-model-v2"
    assert "acme/app" in contracts["derived_from"][0]["description"]


def test_convert_domain_model_entity_filter():
    dm = {
        "entities": {
            "Board": {"fields": {}, "states": ["a", "b"],
                      "transitions": [{"from_state": "a", "to_state": "b", "operation": "go"}]},
            "Card": {"fields": {}},
        }
    }
    contracts, _ = d2f.convert_domain_model(dm, entity_filter="Board")
    assert [e["name"] for e in contracts["entities"]] == ["Board"]
