"""Formal-model template schemas are closed, and stay closed (chief-wiggum#347).

An object with `properties` but no `additionalProperties: false` accepts
anything extra, so a misspelled key validates cleanly and the mistake surfaces
much later as absent behaviour rather than as a schema error. Every object in
these schemas was closed except one transition form, which this file both
fixes-in-place-forever and generalises: any NEW open object in any
formal-models schema fails here rather than being noticed in a review months
later.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import jsonschema.validators
import pytest

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "templates" / "formal-models"
SCHEMAS = sorted(SCHEMA_DIR.glob("*-schema.json"))


def _open_objects(node, path: str = "root") -> list[str]:
    """Every `type: object` with `properties` that does not close itself."""
    found: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            if "additionalProperties" not in node:
                found.append(path)
        for key, value in node.items():
            found.extend(_open_objects(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_open_objects(value, f"{path}[{index}]"))
    return found


def test_there_are_schemas_to_check():
    """Guard the denominator: an empty glob would make every test below vacuous."""
    assert SCHEMAS, f"no *-schema.json under {SCHEMA_DIR}"


@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: p.name)
def test_the_schema_is_itself_a_valid_json_schema(path):
    schema = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.validators.validator_for(schema).check_schema(schema)


@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: p.name)
def test_every_object_is_closed(path):
    schema = json.loads(path.read_text(encoding="utf-8"))
    open_objects = _open_objects(schema)
    assert not open_objects, (
        f"{path.name} has objects that accept unknown keys, so a typo in a "
        f"stamped artifact would validate silently: {open_objects}")


class TestTheTransitionObjectRejectsTypos:
    """The specific hole #347 named, pinned by behaviour rather than by shape."""

    @pytest.fixture
    def transition(self):
        schema = json.loads(
            (SCHEMA_DIR / "ui-spec-schema.json").read_text(encoding="utf-8"))
        return (schema["$defs"]["navigationGraph"]["properties"]["states"]
                ["additionalProperties"]["properties"]["on"])

    def test_the_string_shorthand_is_still_accepted(self, transition):
        jsonschema.validate({"CLICK": "home"}, transition)

    def test_the_object_form_is_still_accepted(self, transition):
        jsonschema.validate({"CLICK": {"target": "home", "guard": "isAuthed"}},
                            transition)

    def test_a_misspelled_key_is_rejected(self, transition):
        with pytest.raises(jsonschema.ValidationError, match="gaurd"):
            jsonschema.validate({"CLICK": {"target": "home", "gaurd": "x"}},
                                transition)

    def test_target_is_still_required(self, transition):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"CLICK": {"guard": "isAuthed"}}, transition)
