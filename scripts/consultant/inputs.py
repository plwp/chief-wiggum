"""inputs.py — read a target repo's adopted patterns + resolve cost-inputs.

Two input sources feed the deriver (scripts/business_consultant.py):

  1. The target repo's ``docs/patterns/adopted.json`` (written by
     ``scripts/apply_pattern.py``): which patterns are adopted, and each
     pattern's *bound parameters* — in particular ``tiered-subscription``'s
     ``tiers``/``matrix``, the per-tenant limit caps that bound worst-case cost.

     ``apply_pattern.list_adopted()`` intentionally does not expose bound
     parameters (it's the ``/architect`` invariant-folding view); this module
     reads the adoption record's raw ``parameters`` block directly instead of
     going through that function, since the deriver's whole job is arithmetic
     over those bound values.

  2. A cost-inputs.json (templates/cost-inputs-schema.json): either the
     operator-authoritative document a human supplied, or — absent that — the
     stack's illustrative seed, loaded with its ``$caveat`` surfaced so the
     deriver never lets those numbers pass as a verified quote.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from apply_pattern import ADOPTED_REL  # noqa: E402

ROOT = SCRIPTS.parent
DEFAULT_STACK = "gcp-serverless-saas"

# Where a target repo keeps its OWN operator-authored cost inputs (preferred over
# a stack's illustrative seed when no explicit --cost-inputs is passed).
TARGET_COST_INPUTS_REL = "docs/cost-inputs.json"

DEFAULT_ILLUSTRATIVE_CAVEAT = (
    "ILLUSTRATIVE -- these cost inputs carry illustrative (unverified) rates, "
    "not verified vendor prices. Verify against live vendor pricing before "
    "quoting a customer or setting a real price floor."
)


def is_illustrative(cost_inputs: dict) -> bool:
    """A cost-inputs document is illustrative — and its caveat must surface,
    however it was supplied — if it declares a top-level ``$caveat`` or ANY
    meter is marked ``provenance: "illustrative"``. This is a property of the
    DATA, so passing an illustrative seed explicitly via ``--cost-inputs`` still
    surfaces the caveat (a rate doesn't become verified by how the file arrived).
    """
    if cost_inputs.get("$caveat"):
        return True
    return any(m.get("provenance") == "illustrative" for m in cost_inputs.get("meters", []) or [])


class ConsultantInputError(Exception):
    """A required input (adopted.json, cost-inputs, stack manifest) is missing or malformed."""


def load_adopted(target_dir: str | Path) -> dict:
    """Return the target repo's adopted patterns: {pattern_id: adoption_record}.

    Empty dict if the repo has no adoption record yet — the deriver degrades
    honestly (no tiered-subscription -> no per-tier unit economics) rather
    than crashing.
    """
    path = Path(target_dir) / ADOPTED_REL
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConsultantInputError(f"malformed {path}: {exc}") from exc
    return doc.get("patterns", {}) or {}


def _maybe_json(value):
    """A parameter bound via `apply_pattern.py --param k=v` is always a raw CLI
    string; a hand-authored fixture may already carry the native JSON value.
    Accept both: try to parse a JSON-looking string, else pass the value through.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "{[":
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def tiered_subscription_binding(adopted: dict) -> tuple[list[str], dict]:
    """Return (tiers, matrix) bound to the adopted `tiered-subscription` pattern,
    or ([], {}) if the pattern isn't adopted. `matrix` per tiered-subscription's
    manifest: {tier: {cap_key: limit (-1 = unlimited), ...}, ...}.
    """
    record = adopted.get("tiered-subscription")
    if not isinstance(record, dict):
        return [], {}
    params = record.get("parameters", {}) or {}
    tiers = _maybe_json(params.get("tiers", []))
    matrix = _maybe_json(params.get("matrix", {}))
    if not isinstance(tiers, list):
        tiers = [t.strip() for t in str(tiers).split(",") if t.strip()]
    if not isinstance(matrix, dict):
        matrix = {}
    return tiers, matrix


def load_stack_manifest(stack_id: str, base: Path = ROOT) -> dict:
    path = base / "patterns" / "stacks" / stack_id / "manifest.json"
    if not path.is_file():
        raise ConsultantInputError(f"unknown stack {stack_id!r}: manifest not found at {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConsultantInputError(f"malformed stack manifest {path}: {exc}") from exc


def active_cost_tier(adopted: dict, stack_manifest: dict) -> str:
    """The highest-numbered stack cost tier (patterns/stacks/<id>/manifest.json's
    `cost_tiers` ladder) among the target's adopted patterns' `bindings` entries.

    A stack manifest's `bindings` map ties an adopted pattern id to the cost tier
    it goes live at (e.g. gcp-serverless-saas binds `tiered-subscription` at T2).
    Falls back to the ladder's lowest/first tier if none of the adopted patterns
    have a binding (a T0 product with no monetization pattern adopted yet).
    """
    ladder = [t.get("id") for t in stack_manifest.get("cost_tiers", []) if t.get("id")]
    bindings = stack_manifest.get("bindings", {}) or {}
    candidates = [
        bindings[pid]["tier"]
        for pid in adopted
        if isinstance(bindings.get(pid), dict) and bindings[pid].get("tier")
    ]
    if not candidates:
        return ladder[0] if ladder else "T0"
    in_ladder = [t for t in candidates if t in ladder]
    if not in_ladder:
        return candidates[0]
    return max(in_ladder, key=ladder.index)


def load_cost_inputs(
    cost_inputs_path: str | Path | None, stack_id: str, base: Path = ROOT
) -> tuple[dict, bool, str]:
    """Return (cost_inputs, used_illustrative_seed, source_path).

    Operator-supplied path wins when given. Otherwise falls back to the stack's
    illustrative seed (patterns/stacks/<id>/cost-inputs.illustrative.json) —
    the DESIGN DECISION per chief-wiggum#122: the seed is a documented fallback,
    never presented as authoritative (its `$caveat` is surfaced by the caller).
    """
    if cost_inputs_path:
        p = Path(cost_inputs_path)
        if not p.is_file():
            raise ConsultantInputError(f"cost-inputs file not found: {p}")
        try:
            return json.loads(p.read_text()), False, str(p)
        except json.JSONDecodeError as exc:
            raise ConsultantInputError(f"malformed cost-inputs {p}: {exc}") from exc

    seed = base / "patterns" / "stacks" / stack_id / "cost-inputs.illustrative.json"
    if not seed.is_file():
        raise ConsultantInputError(
            f"no --cost-inputs supplied and no illustrative seed for stack {stack_id!r} "
            f"(looked at {seed})"
        )
    try:
        return json.loads(seed.read_text()), True, str(seed)
    except json.JSONDecodeError as exc:
        raise ConsultantInputError(f"malformed illustrative seed {seed}: {exc}") from exc
