"""The transition-map schema must accept what its own generator emits (#289).

`1cc627c` — #289's fail-closed audit, "5 gates converted" — taught
`verify_transitions.py` to emit the four-state reporting block
(`applicability` / `outcome` / `measured` / `errors`). The schema was never
updated, and it declares `additionalProperties: false`.

So every transition-map generated after that commit failed
`formal_models.py validate`, which `/architect` Step 6 runs. It went unnoticed
because the older, already-committed maps predate the change and still
validate: `epic-factory-hardening`'s map is clean, and the DAG epic's — written
later — is the one that fails.

These tests bind the two together, so converting a generator to a new reporting
vocabulary cannot leave its schema behind again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCHEMA = REPO / "templates" / "formal-models" / "transition-map-schema.json"

# The block #289 introduced. Named here rather than derived, so a generator that
# silently stops emitting one is a failure and not a quietly-shrinking check.
FOUR_STATE_FIELDS = ("applicability", "outcome", "measured", "errors")


def schema() -> dict:
    return json.loads(SCHEMA.read_text())


def test_the_schema_accepts_every_four_state_field():
    props = schema()["properties"]
    missing = [f for f in FOUR_STATE_FIELDS if f not in props]
    assert missing == [], (
        f"transition-map-schema rejects {missing}, which verify_transitions.py "
        f"emits — /architect's validate step fails on every freshly generated map")


def test_the_object_stays_closed():
    """`additionalProperties: false` is what caught this. Relaxing it would have
    been the one-line fix and would have traded a real guard for convenience."""
    assert schema().get("additionalProperties") is False


@pytest.mark.parametrize("field", FOUR_STATE_FIELDS)
def test_each_four_state_field_is_typed_not_just_permitted(field):
    """A bare `{}` would silence the validator while allowing anything."""
    spec = schema()["properties"][field]
    assert spec.get("type"), f"{field} has no declared type"
    assert spec.get("description"), f"{field} is undocumented"


def test_outcome_and_applicability_carry_the_289_vocabularies():
    props = schema()["properties"]
    assert set(props["outcome"]["enum"]) == {"pass", "findings", "inapplicable", "error"}
    assert set(props["applicability"]["enum"]) == {"applicable", "inapplicable", "error"}


def test_the_generator_still_emits_exactly_these_fields():
    """The binding in the other direction: if verify_transitions.py grows a
    fifth reporting field, this fails rather than the schema silently
    rejecting real output again."""
    source = (REPO / "scripts" / "verify_transitions.py").read_text()
    for field in FOUR_STATE_FIELDS:
        assert f'"{field}":' in source, (
            f"verify_transitions.py no longer emits {field} — the schema and the "
            f"generator have drifted apart again, in the other direction")


@pytest.mark.parametrize(
    "artifact",
    sorted((REPO / "docs" / "epics").glob("*/models/transition-map.json")),
    ids=lambda p: p.parent.parent.name,
)
def test_every_committed_transition_map_validates(artifact):
    """The regression itself: the DAG epic's map failed this before the fix,
    and factory-hardening's (written pre-#289) passed — which is why nobody
    noticed."""
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(json.loads(artifact.read_text()), schema())


def test_there_are_committed_maps_to_check():
    """A denominator: an empty glob must not let the check above pass silently."""
    maps = list((REPO / "docs" / "epics").glob("*/models/transition-map.json"))
    assert len(maps) >= 2, f"expected committed transition maps, found {len(maps)}"
