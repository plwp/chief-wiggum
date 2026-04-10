#!/usr/bin/env python3
"""
Convert duplicat-rex DomainModel to chief-wiggum formal model JSON.

Bridges the reverse engineering pipeline to the formal methods pipeline:
  EntityHypothesis → contracts-schema.json + state-machine-schema.json
  OperationHypothesis → operation contracts with REQUIRES/ENSURES
  StateTransition → state machine transitions with guards

CLI:
    python3 scripts/domain_to_formal.py <domain-model.json> --output <dir>
    python3 scripts/domain_to_formal.py <domain-model.json> --entity Board --output <dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Map duplicat-rex FieldType to contract schema types
FIELD_TYPE_MAP = {
    "string": "string",
    "integer": "integer",
    "float": "number",
    "boolean": "boolean",
    "datetime": "string",  # ISO 8601
    "enum": "enum",
    "relation": "ObjectID",
    "unknown": "string",
}

# Map HTTP methods to operation verbs for naming
METHOD_VERB = {
    "GET": "read",
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}


def load_domain_model(path: Path) -> dict:
    """Load a duplicat-rex domain model JSON."""
    with open(path) as f:
        return json.load(f)


def entity_to_contracts(entity: dict, entity_name: str) -> dict:
    """Convert an EntityHypothesis to contracts-schema.json format."""
    fields = []
    for fname, fdata in entity.get("fields", {}).items():
        field_type = FIELD_TYPE_MAP.get(fdata.get("field_type", "unknown"), "string")
        if field_type == "enum" and fdata.get("enum_values"):
            notes = f"Valid values: {', '.join(fdata['enum_values'])}"
        else:
            notes = None

        field_def = {
            "name": fname,
            "type": field_type,
            "required": "always" if fdata.get("required") else "optional",
        }
        if fdata.get("unique"):
            field_def["immutable"] = True
        if fdata.get("related_entity"):
            field_def["source_of_truth"] = f"{fdata['related_entity'].lower()}s collection"
            field_def["type"] = "ObjectID"
        if notes:
            field_def["notes"] = notes

        # Add provenance from evidence
        if fdata.get("evidence"):
            field_def["derived_from"] = [
                {"type": "observed_fact", "ref": ev, "description": f"Field {fname} observed"}
                for ev in fdata["evidence"][:3]  # Limit to 3 evidence refs
            ]

        fields.append(field_def)

    operations = []
    for op in entity.get("operations", []):
        operation = {
            "name": f"{op['name'].capitalize()} {entity_name}",
            "method": op.get("method", "POST"),
            "path": op.get("endpoint_pattern", f"/api/{entity.get('plural', entity_name.lower() + 's')}"),
        }

        # Preconditions from the operation
        preconditions = []
        for i, pre in enumerate(op.get("preconditions", [])):
            preconditions.append({
                "id": f"PRE-{entity_name[:3].upper()}-{op['name'][:3].upper()}-{i+1:03d}",
                "description": pre,
                "expression": f"# TODO: {pre}",
            })
        # Add required fields as preconditions
        for req_field in op.get("required_fields", []):
            preconditions.append({
                "id": f"PRE-{entity_name[:3].upper()}-{op['name'][:3].upper()}-REQ-{req_field}",
                "description": f"{req_field} is provided",
                "expression": f"request.body.{req_field} is not None",
            })
        if preconditions:
            operation["preconditions"] = preconditions

        # Postconditions
        postconditions = []
        for i, post in enumerate(op.get("postconditions", [])):
            postconditions.append({
                "id": f"POST-{entity_name[:3].upper()}-{op['name'][:3].upper()}-{i+1:03d}",
                "description": post,
                "expression": f"# TODO: {post}",
            })
        if postconditions:
            operation["postconditions"] = postconditions

        # Error cases
        error_cases = []
        for ec in op.get("error_cases", []):
            if isinstance(ec, dict):
                error_cases.append({
                    "status": ec.get("status", 400),
                    "condition": ec.get("condition", ec.get("description", "error")),
                })
        if error_cases:
            operation["error_cases"] = error_cases

        # Provenance
        if op.get("evidence"):
            operation["derived_from"] = [
                {"type": "observed_fact", "ref": ev, "description": f"Operation {op['name']} observed"}
                for ev in op["evidence"][:3]
            ]

        operations.append(operation)

    result = {
        "name": entity_name,
        "description": f"Reverse-engineered entity from domain model",
        "fields": fields,
    }
    if operations:
        result["operations"] = operations

    # Entity-level provenance
    if entity.get("evidence"):
        result["derived_from"] = [
            {"type": "observed_fact", "ref": ev}
            for ev in entity["evidence"][:5]
        ]

    return result


def entity_to_state_machine(entity: dict, entity_name: str) -> dict | None:
    """Convert EntityHypothesis states + transitions to state-machine-schema.json format."""
    states_list = entity.get("states", [])
    transitions = entity.get("transitions", [])

    if not states_list or not transitions:
        return None

    # Determine initial and terminal states
    # Heuristic: first state is initial, states with no outgoing transitions are terminal
    outgoing = {t["from_state"] for t in transitions}
    incoming = {t["to_state"] for t in transitions}

    initial = states_list[0]  # First listed state as initial
    terminal_candidates = set(states_list) - outgoing  # States with no outgoing

    states = {}
    for s in states_list:
        state_def = {"description": f"{entity_name} in {s} state"}
        if s == initial:
            state_def["type"] = "initial"
        elif s in terminal_candidates:
            state_def["type"] = "terminal"
        else:
            state_def["type"] = "normal"
        states[s] = state_def

    # Convert transitions
    formal_transitions = []
    for t in transitions:
        tx = {
            "from": t["from_state"],
            "to": t["to_state"],
            "event": t["operation"],
        }

        # Find the matching operation for preconditions as guards
        matching_ops = [
            op for op in entity.get("operations", [])
            if op["name"] == t["operation"]
        ]
        if matching_ops and matching_ops[0].get("preconditions"):
            tx["guards"] = [
                {
                    "id": f"guard_{t['operation']}_{i}",
                    "description": pre,
                }
                for i, pre in enumerate(matching_ops[0]["preconditions"])
            ]

        if matching_ops and matching_ops[0].get("postconditions"):
            tx["actions"] = [
                post for post in matching_ops[0]["postconditions"]
            ]

        formal_transitions.append(tx)

    # Build invalid transitions: for terminal states, all outgoing are invalid
    invalid_transitions = []
    for s in terminal_candidates:
        for target in states_list:
            if target != s:
                invalid_transitions.append({
                    "from": s,
                    "to": target,
                    "reason": f"{s} is a terminal state — no transitions out",
                })

    # Also flag transitions that skip states (from initial to non-adjacent)
    # This is heuristic — we flag direct jumps over intermediate states
    direct_targets = {t["to_state"] for t in transitions if t["from_state"] == initial}
    for s in states_list:
        if s != initial and s not in direct_targets and s not in terminal_candidates:
            invalid_transitions.append({
                "from": initial,
                "to": s,
                "reason": f"Cannot skip directly from {initial} to {s}",
            })

    result = {
        "name": f"{entity_name} Lifecycle",
        "description": f"State machine for {entity_name} entity, extracted from domain model",
        "initial": initial,
        "states": states,
        "transitions": formal_transitions,
    }

    if invalid_transitions:
        result["invalid_transitions"] = invalid_transitions

    # Context from entity fields
    if entity.get("fields"):
        context = {}
        for fname, fdata in entity["fields"].items():
            ctx_field = {
                "type": FIELD_TYPE_MAP.get(fdata.get("field_type", "unknown"), "string"),
            }
            if fdata.get("related_entity"):
                ctx_field["type"] = "ObjectID"
                ctx_field["source_of_truth"] = f"{fdata['related_entity'].lower()}s collection"
            context[fname] = ctx_field
        result["context"] = context

    # Provenance
    if entity.get("evidence"):
        result["derived_from"] = [
            {"type": "observed_fact", "ref": ev}
            for ev in entity["evidence"][:5]
        ]

    return result


def convert_domain_model(dm: dict, entity_filter: str | None = None) -> tuple[dict, list[dict]]:
    """Convert a full DomainModel to formal model artifacts.

    Returns (contracts_json, list_of_state_machine_jsons).
    """
    entities = dm.get("entities", {})
    if entity_filter:
        entities = {k: v for k, v in entities.items() if k == entity_filter}

    contract_entities = []
    state_machines = []

    for name, entity in entities.items():
        # Contracts
        contract_entity = entity_to_contracts(entity, name)
        contract_entities.append(contract_entity)

        # State machine (only if states exist)
        sm = entity_to_state_machine(entity, name)
        if sm:
            state_machines.append(sm)

    contracts = {
        "entities": contract_entities,
        "derived_from": [
            {
                "type": "observed_fact",
                "ref": f"domain-model-v{dm.get('version', 1)}",
                "description": f"Extracted from {dm.get('target', 'unknown')} domain model",
            }
        ],
    }

    return contracts, state_machines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert duplicat-rex DomainModel to chief-wiggum formal models"
    )
    parser.add_argument("model", help="Path to domain model JSON")
    parser.add_argument("--entity", help="Convert only this entity (default: all)")
    parser.add_argument("--output", required=True, help="Output directory")
    args = parser.parse_args()

    dm = load_domain_model(Path(args.model))
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    contracts, state_machines = convert_domain_model(dm, args.entity)

    # Write contracts
    contracts_path = output_dir / "contracts.json"
    contracts_path.write_text(json.dumps(contracts, indent=2))
    print(f"  {contracts_path} — {len(contracts['entities'])} entities")

    # Write state machines (one file per entity)
    for sm in state_machines:
        name_slug = sm["name"].lower().replace(" ", "-")
        sm_path = output_dir / f"{name_slug}.state-machine.json"
        sm_path.write_text(json.dumps(sm, indent=2))
        print(f"  {sm_path} — {len(sm['states'])} states, {len(sm['transitions'])} transitions")

    print(f"\nConverted {len(contracts['entities'])} entities, {len(state_machines)} state machines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
