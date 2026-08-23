"""Duplicated `$defs` may not drift apart silently (chief-wiggum#347).

`provenance` is copy-pasted into six schemas and `asm_ref` into two. #347
proposed deduping them behind a shared file and `$ref`, and noted the cost:
the templates would stop being standalone single-file stamps. That call is
still open.

This guards the part that needs no call — the copies must agree, and the one
deliberate variant must stay deliberate.

Measuring the drift first turned up something the ticket did not predict.
`asm_ref.ref` carried `"minLength": 1` in `architecture-schema.json` and had
LOST it in `system-contracts-schema.json`, so an empty `ref` was rejected by
one schema and accepted by the other. That is a silent constraint divergence,
not a documentation difference, and it is exactly what a copy-paste `$def`
does when nothing watches it.

Not guarded, deliberately: `entity`, `node` and `summary` also appear in two
schemas each, but they are unrelated concepts that happen to share a name — a
contracts `entity` (fields, operations) and a transition-map `entity`
(model_file, transitions) have nothing to reconcile. Requiring those to match
would be a bug, not a guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FM = REPO / "templates" / "formal-models"

PROVENANCE_FILES = [
    "architecture-schema.json",
    "contracts-schema.json",
    "gap-classification.json",
    "state-machine-schema.json",
    "system-contracts-schema.json",
    "ui-spec-schema.json",
]
ASM_REF_FILES = ["architecture-schema.json", "system-contracts-schema.json"]

# The ONE sanctioned divergence, with its reason. A UI fact is evidenced by a
# screenshot; a vendor API doc is not a thing a ui-spec cites. Anything else
# differing is drift.
UI_SPEC_VARIANT = {
    "file": "ui-spec-schema.json",
    "swaps": ("api_doc", "screenshot"),
    "why": "a UI fact is evidenced by a screenshot, not by a vendor API doc",
}


def defs(name: str) -> dict:
    return json.loads((FM / name).read_text())["$defs"]


def test_the_files_and_defs_exist():
    """A denominator: if these move or get renamed, this file must fail rather
    than pass by checking nothing."""
    assert len(PROVENANCE_FILES) >= 6
    for name in PROVENANCE_FILES:
        assert "provenance" in defs(name), f"{name} has no $defs.provenance"
    for name in ASM_REF_FILES:
        assert "asm_ref" in defs(name), f"{name} has no $defs.asm_ref"


def test_provenance_is_identical_except_for_the_sanctioned_variant():
    canonical_files = [f for f in PROVENANCE_FILES if f != UI_SPEC_VARIANT["file"]]
    bodies = {f: defs(f)["provenance"] for f in canonical_files}
    first = bodies[canonical_files[0]]
    drifted = [f for f, body in bodies.items() if body != first]
    assert drifted == [], (
        f"$defs.provenance has drifted in {drifted}; the copies must agree until "
        f"#347's dedupe call is made")


def test_the_ui_spec_variant_differs_only_in_the_documented_swap():
    """A sanctioned variant is not a licence to differ in other ways."""
    canonical = defs("contracts-schema.json")["provenance"]
    variant = defs(UI_SPEC_VARIANT["file"])["provenance"]
    old, new = UI_SPEC_VARIANT["swaps"]

    normalised = json.loads(json.dumps(variant))
    enum = normalised["properties"]["type"]["enum"]
    assert new in enum, f"{UI_SPEC_VARIANT['file']} lost its {new!r} provenance type"
    assert old not in enum, (
        f"{UI_SPEC_VARIANT['file']} now accepts {old!r} too — the variant exists "
        f"because {UI_SPEC_VARIANT['why']}")
    normalised["properties"]["type"]["enum"] = [
        old if e == new else e for e in enum]
    assert normalised == canonical, (
        "the ui-spec provenance differs from the canonical copy in more than the "
        "documented api_doc->screenshot swap")


def test_asm_ref_is_identical_across_its_copies():
    bodies = {f: defs(f)["asm_ref"] for f in ASM_REF_FILES}
    first = bodies[ASM_REF_FILES[0]]
    drifted = [f for f, body in bodies.items() if body != first]
    assert drifted == [], f"$defs.asm_ref has drifted in {drifted}"


@pytest.mark.parametrize("name", ASM_REF_FILES)
def test_asm_ref_keeps_the_constraint_one_copy_had_lost(name):
    """The specific regression #347's inspection uncovered: an empty `ref` was
    rejected by architecture-schema and accepted by system-contracts."""
    ref = defs(name)["asm_ref"]["properties"]["ref"]
    assert ref.get("minLength") == 1, (
        f"{name} dropped minLength on asm_ref.ref — an empty evidence pointer "
        f"would validate, which is the divergence this guard exists for")


def test_unrelated_defs_that_share_a_name_are_not_forced_to_match():
    """`entity` means different things in contracts and transition-map. This
    asserts the guard above is scoped, not that it forgot them."""
    contracts_entity = defs("contracts-schema.json")["entity"]
    transition_entity = defs("transition-map-schema.json")["entity"]
    assert contracts_entity != transition_entity
    assert "operations" in contracts_entity["properties"]
    assert "transitions" in transition_entity["properties"]


def test_every_schema_still_parses():
    for path in sorted(FM.glob("*.json")):
        json.loads(path.read_text())
