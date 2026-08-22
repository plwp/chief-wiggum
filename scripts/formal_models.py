#!/usr/bin/env python3
"""
Formal model parser, validator, and generator.

Consumes state machine and contract JSON conforming to the schemas in
templates/formal-models/. Provides:

- Schema validation
- Pure-Python state machine graph analysis (reachable states, dead states, etc.)
- Conversion to XState v5 JSON
- Hypothesis RuleBasedStateMachine code generation
- deal/icontract DbC decorator code generation
- Guard clause template generation (Python and Go)

CLI:
    python3 scripts/formal_models.py validate <model.json>
    python3 scripts/formal_models.py graph <state-machine.json>
    python3 scripts/formal_models.py convert <state-machine.json> --format xstate
    python3 scripts/formal_models.py generate <model.json> --format hypothesis|deal|guards-py|guards-go
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.trace_ids import near_miss_ids  # noqa: E402

# ---------------------------------------------------------------------------
# Schema paths — resolved relative to this file's parent (scripts/) → repo root
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "templates" / "formal-models"

SCHEMAS = {
    "state-machine": SCHEMA_DIR / "state-machine-schema.json",
    "contracts": SCHEMA_DIR / "contracts-schema.json",
    "xstate": SCHEMA_DIR / "xstate-schema.json",
    "gap": SCHEMA_DIR / "gap-classification.json",
    "transition-map": SCHEMA_DIR / "transition-map-schema.json",
    "ui-spec": SCHEMA_DIR / "ui-spec-schema.json",
    "architecture": SCHEMA_DIR / "architecture-schema.json",
}


def _load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _load_schema(name: str) -> dict:
    return _load_json(SCHEMAS[name])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def detect_schema_type(data: dict) -> str:
    """Heuristic detection of which schema a JSON document conforms to."""
    if "nodes" in data and "edges" in data:
        return "architecture"
    if "states" in data and "transitions" in data:
        return "state-machine"
    if "entities" in data and "summary" in data:
        return "transition-map"
    if "entities" in data:
        return "contracts"
    if "gaps" in data:
        return "gap"
    # UI spec has 'pages' and 'navigation' (required by schema)
    if "pages" in data and "navigation" in data:
        return "ui-spec"
    # XState format has 'id' and 'states' but no 'transitions' array
    if "id" in data and "states" in data:
        return "xstate"
    raise ValueError("Cannot detect schema type from document structure")


def validate(data: dict, schema_type: str | None = None) -> list[str]:
    """Validate data against its schema. Returns list of error messages (empty = valid)."""
    if schema_type is None:
        schema_type = detect_schema_type(data)
    schema = _load_schema(schema_type)
    validator = jsonschema.Draft202012Validator(schema)
    return [e.message for e in validator.iter_errors(data)]


# find_malformed_ids IS chief_wiggum.trace_ids.near_miss_ids (chief-wiggum#293):
# a malformed stable id in a formal model (INV-001 instead of INV-order-001)
# is schema-VALID today — neither state-machine-schema.json's $defs.invariant
# .id nor contracts-schema.json's $defs.condition.id carries a `pattern` — yet
# invisible to check_traceability.py's DEFINE_RE/ID_RE once folded into an
# epic (chief-wiggum#281). Reusing near_miss_ids() over the model's raw JSON
# TEXT (not a bare extracted id string — near_miss_ids only matches in the
# same declaration positions DEFINE_RE uses: a JSON `"id": "..."` field, a
# markdown heading, or a bold label) means this is the SAME detector
# check_traceability.py uses, not a second regex. Deliberately NOT
# `DEFINE_RE.search(bare_id)`: DEFINE_RE requires a declaration prefix and
# never matches a bare id string on its own.
find_malformed_ids = near_miss_ids


# ---------------------------------------------------------------------------
# State machine graph analysis (pure Python — no Node dependency)
# ---------------------------------------------------------------------------


@dataclass
class GraphAnalysis:
    states: list[str]
    initial: str
    terminal_states: list[str]
    reachable_states: set[str]
    unreachable_states: set[str]
    dead_states: set[str]
    transitions: list[dict]
    transition_count: int
    invalid_transitions: list[dict]
    invariant_count: int
    has_dead_states: bool
    has_unreachable_states: bool
    all_terminals_reachable: bool


def analyze_graph(sm: dict) -> GraphAnalysis:
    """Analyze a state machine model for graph properties."""
    states = list(sm["states"].keys())
    initial = sm["initial"]
    transitions = sm.get("transitions", [])
    invalid_transitions = sm.get("invalid_transitions", [])
    invariants = sm.get("invariants", [])

    terminal_states = [
        s for s, defn in sm["states"].items()
        if defn.get("type") == "terminal"
    ]

    # Build adjacency list
    adj: dict[str, set[str]] = defaultdict(set)
    for t in transitions:
        adj[t["from"]].add(t["to"])

    # BFS from initial to find reachable states
    reachable: set[str] = set()
    queue: deque[str] = deque([initial])
    while queue:
        s = queue.popleft()
        if s in reachable:
            continue
        reachable.add(s)
        for neighbor in adj.get(s, set()):
            if neighbor not in reachable:
                queue.append(neighbor)

    unreachable = set(states) - reachable

    # Dead states: non-terminal states with no outgoing transitions
    states_with_outgoing = {t["from"] for t in transitions}
    dead = {
        s for s in states
        if s not in states_with_outgoing
        and sm["states"][s].get("type") != "terminal"
    }

    # Check all terminal states are reachable
    all_terminals_reachable = all(t in reachable for t in terminal_states)

    return GraphAnalysis(
        states=states,
        initial=initial,
        terminal_states=terminal_states,
        reachable_states=reachable,
        unreachable_states=unreachable,
        dead_states=dead,
        transitions=transitions,
        transition_count=len(transitions),
        invalid_transitions=invalid_transitions,
        invariant_count=len(invariants),
        has_dead_states=len(dead) > 0,
        has_unreachable_states=len(unreachable) > 0,
        all_terminals_reachable=all_terminals_reachable,
    )


def enumerate_paths(sm: dict, max_depth: int = 20) -> list[list[dict]]:
    """Enumerate all simple paths from initial state to terminal states (DFS, no cycles).

    Returns a list of paths, where each path is a list of
    {state, event, next_state, guards} dicts.
    """
    initial = sm["initial"]
    terminal_states = {
        s for s, defn in sm["states"].items()
        if defn.get("type") == "terminal"
    }

    # Build transition lookup: from_state → [(to, event, guards, actions)]
    tx_by_source: dict[str, list[dict]] = defaultdict(list)
    for t in sm.get("transitions", []):
        tx_by_source[t["from"]].append(t)

    paths: list[list[dict]] = []

    def dfs(state: str, path: list[dict], visited: set[str]) -> None:
        if state in terminal_states:
            paths.append(list(path))
            return
        if len(path) >= max_depth:
            return
        for t in tx_by_source.get(state, []):
            next_state = t["to"]
            if next_state in visited:
                continue
            step = {
                "state": state,
                "event": t["event"],
                "next_state": next_state,
                "guards": [g.get("description", "") for g in t.get("guards", [])],
            }
            path.append(step)
            visited.add(next_state)
            dfs(next_state, path, visited)
            visited.discard(next_state)
            path.pop()

    dfs(initial, [], {initial})
    return paths


# ---------------------------------------------------------------------------
# Conversion: state machine → XState v5 JSON
# ---------------------------------------------------------------------------


def to_xstate(sm: dict) -> dict:
    """Convert a state-machine-schema model to XState v5 machine config."""
    machine: dict[str, Any] = {
        "id": sm["name"].lower().replace(" ", "_"),
        "initial": sm["initial"],
        "states": {},
    }

    # Context
    if sm.get("context"):
        machine["context"] = {
            name: None for name in sm["context"]
        }

    # Build transition lookup by source state
    tx_by_source: dict[str, list[dict]] = defaultdict(list)
    for t in sm.get("transitions", []):
        tx_by_source[t["from"]].append(t)

    # States
    for state_id, state_def in sm["states"].items():
        node: dict[str, Any] = {}

        if state_def.get("type") == "terminal":
            node["type"] = "final"

        # Transitions
        state_transitions = tx_by_source.get(state_id, [])
        if state_transitions:
            on: dict[str, Any] = {}
            for t in state_transitions:
                event = t["event"]
                tx_obj: dict[str, Any] = {"target": t["to"]}
                if t.get("guards"):
                    # Combine guard descriptions into a single guard name
                    guard_name = "_and_".join(
                        g.get("id", g["description"].replace(" ", "_"))
                        for g in t["guards"]
                    )
                    tx_obj["guard"] = guard_name
                if t.get("actions"):
                    tx_obj["actions"] = [
                        {"type": a} for a in t["actions"]
                    ]

                if event in on:
                    # Multiple transitions for same event → array
                    existing = on[event]
                    if isinstance(existing, list):
                        existing.append(tx_obj)
                    else:
                        on[event] = [existing, tx_obj]
                else:
                    on[event] = tx_obj
            node["on"] = on

        # Entry/exit actions
        if state_def.get("entry_actions"):
            node["entry"] = [{"type": a} for a in state_def["entry_actions"]]
        if state_def.get("exit_actions"):
            node["exit"] = [{"type": a} for a in state_def["exit_actions"]]

        # Meta
        if state_def.get("description"):
            node["meta"] = {"description": state_def["description"]}

        machine["states"][state_id] = node

    return machine


# ---------------------------------------------------------------------------
# Code generation: Hypothesis RuleBasedStateMachine
# ---------------------------------------------------------------------------


def generate_hypothesis(sm: dict) -> str:
    """Generate a Hypothesis RuleBasedStateMachine test class from a state machine model."""
    name = sm["name"].replace(" ", "")
    states_list = list(sm["states"].keys())
    initial = sm["initial"]
    transitions = sm.get("transitions", [])
    invalid_transitions = sm.get("invalid_transitions", [])
    invariants = sm.get("invariants", [])

    lines = [
        '"""',
        f"Auto-generated Hypothesis RuleBasedStateMachine for: {sm['name']}",
        f"Generated from formal model. Do not edit by hand.",
        '"""',
        "",
        "from hypothesis import settings",
        "from hypothesis.stateful import RuleBasedStateMachine, rule, invariant, initialize",
        "",
        "",
        f"class {name}(RuleBasedStateMachine):",
        f'    """State machine test: {sm.get("description", sm["name"])}"""',
        "",
        f"    VALID_STATES = {states_list!r}",
        f"    TERMINAL_STATES = {[s for s, d in sm['states'].items() if d.get('type') == 'terminal']!r}",
        "",
        "    @initialize()",
        "    def init(self):",
        f'        self.state = "{initial}"',
        "",
    ]

    # Generate a rule for each transition
    for t in transitions:
        method_name = f"transition_{t['from']}_to_{t['to']}_via_{t['event']}"
        guard_comment = ""
        if t.get("guards"):
            guard_descs = ", ".join(g["description"] for g in t["guards"])
            guard_comment = f"  # Guards: {guard_descs}"

        lines.extend([
            f"    @rule()",
            f"    def {method_name}(self):{guard_comment}",
            f'        if self.state != "{t["from"]}":',
            f"            return",
            f'        self.state = "{t["to"]}"',
            "",
        ])

    # Generate invariants
    for inv in invariants:
        inv_id = inv["id"].replace("-", "_").lower()
        lines.extend([
            f"    @invariant()",
            f"    def check_{inv_id}(self):",
            f'        """{inv["description"]}"""',
        ])
        if inv.get("scope") == "state-specific" and inv.get("applies_to_states"):
            states = inv["applies_to_states"]
            lines.append(f"        if self.state not in {states!r}:")
            lines.append(f"            return")
        lines.extend([
            f"        # TODO: implement check — expression: {inv.get('expression', 'N/A')}",
            f"        pass",
            "",
        ])

    # Generate negative tests for invalid transitions
    if invalid_transitions:
        lines.extend([
            "",
            "    # --- Invalid transition assertions ---",
            "",
        ])
        for it in invalid_transitions:
            method_name = f"invalid_{it['from']}_to_{it['to']}"
            lines.extend([
                f"    @rule()",
                f"    def {method_name}(self):",
                f'        """Must be rejected: {it.get("reason", "invalid transition")}"""',
                f'        if self.state != "{it["from"]}":',
                f"            return",
                f'        # Assert this transition is not possible',
                f'        assert self.state != "{it["to"]}" or self.state == "{it["from"]}"',
                "",
            ])

    lines.extend([
        "",
        f"TestStateMachine = {name}.TestCase",
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Expression idiom: non_codegen guards, pseudo-operator transpile, slugify
# ---------------------------------------------------------------------------
#
# Contract preconditions/postconditions carry a "Python-flavored pseudo-code"
# `expression` string (see templates/formal-models/contracts-schema.json)
# that the guard/deal generators below interpolate directly into generated
# Python and Go. Two idioms in that pseudo-code are NOT directly
# codegen-able and must be handled before interpolation:
#
#   1. `non_codegen(<kind>): <description>` — a build/provision-time
#      assertion (archguard, index, provisioning, ...) that has no runtime
#      representation. It must render as a comment/stub, never as an
#      executable `if`/`lambda` guard.
#   2. Pseudo-code operators — `implies`, `⇒` (implication), `×`
#      (multiplication), `Δ` (delta / change-of) — which read naturally in
#      prose but are not valid Python or Go syntax as-is.

NON_CODEGEN_PREFIX = "non_codegen("

# Unicode pseudo-operator glyph -> ASCII word/symbol form. Applied before the
# word-level transpile below so `⇒` and `implies` share one code path.
_UNICODE_OPERATOR_ALIASES = {
    "⇒": "implies",
    "×": "*",
    "Δ": "delta",
}


def is_non_codegen(expression: str | None) -> bool:
    """True if `expression` is a build/provision-time assertion (the
    `non_codegen(<kind>): ...` idiom) that must never be emitted as a
    runtime guard."""
    if not expression:
        return False
    return expression.strip().startswith(NON_CODEGEN_PREFIX)


def _normalize_unicode_operators(expression: str) -> str:
    """Replace pseudo-code unicode operator glyphs with their ASCII form,
    then collapse the whitespace runs that substitution introduces."""
    normalized = expression
    for glyph, ascii_form in _UNICODE_OPERATOR_ALIASES.items():
        normalized = normalized.replace(glyph, f" {ascii_form} ")
    return re.sub(r"[ \t]+", " ", normalized).strip()


def _split_top_level(expression: str, operator: str) -> tuple[str, str] | None:
    """Split `expression` on the first top-level (paren-depth 0) standalone
    occurrence of the word `operator`. Returns (left, right), or None if the
    operator does not appear at the top nesting level."""
    for match in re.finditer(rf"(?<!\w){re.escape(operator)}(?!\w)", expression):
        prefix = expression[: match.start()]
        depth = prefix.count("(") - prefix.count(")")
        if depth == 0:
            left = expression[: match.start()].strip()
            right = expression[match.end():].strip()
            if left and right:
                return left, right
    return None


def transpile_expression(expression: str, lang: str = "python") -> str:
    """Transpile the pseudo-code expression idiom into compilable syntax for
    `lang` ("python" or "go"). Handles the established operators (and their
    unicode glyphs):

        A implies B  /  A ⇒ B   ->  (not (A)) or (B)      [python]
                                 ->  (!(A) || (B))         [go]
        A × B                    ->  A * B                 [both]
        A Δ B                    ->  (A - B)                [both]  (delta / change-of)

    Expressions without any of these tokens pass through unchanged (after
    unicode normalization, which is a no-op when no glyphs are present).
    Callers must check `is_non_codegen` first — non_codegen expressions are
    never meant to reach this function.
    """
    working = _normalize_unicode_operators(expression)

    implies_split = _split_top_level(working, "implies")
    if implies_split:
        left, right = implies_split
        if lang == "go":
            return f"(!({left}) || ({right}))"
        return f"(not ({left})) or ({right})"

    delta_split = _split_top_level(working, "delta")
    if delta_split:
        left, right = delta_split
        return f"({left} - {right})"

    return working


_SLUG_SPLIT_RE = re.compile(r"[^0-9A-Za-z_]+")


def slugify_identifier(name: str, *, pascal_case: bool = False) -> str:
    """Convert an arbitrary contract/operation name into a legal Python/Go
    identifier: strips apostrophes/quotes outright, splits on any other
    run of non-alphanumeric characters (spaces, hyphens, em-dashes,
    parentheses, ...), and guards against a leading digit.

    `pascal_case=True` renders Go-style PascalCase (for exported function
    names); otherwise renders snake_case (for Python function names).
    """
    cleaned = re.sub(r"['’\"]", "", name)
    words = [w for w in _SLUG_SPLIT_RE.split(cleaned) if w]
    if not words:
        return "_unnamed"

    if pascal_case:
        slug = "".join(w[:1].upper() + w[1:] for w in words)
    else:
        slug = "_".join(w.lower() for w in words)

    if slug[0].isdigit():
        slug = f"_{slug}"
    return slug


def _render_precondition_python(pre: dict, op: dict) -> list[str]:
    """Render one precondition as Python guard-clause lines: a runtime `if`
    guard for a codegen-able expression, or a comment stub for a
    `non_codegen(...)` build/provision-time assertion."""
    expr = pre.get("expression", "False  # TODO")
    description = pre["description"]

    if is_non_codegen(expr):
        return [
            f"    # BUILD/PROVISION-TIME GUARD (not enforced at runtime): {description}",
            f"    # {expr}",
            "",
        ]

    lines = [
        f"    # REQUIRES: {description}",
        f"    if not ({transpile_expression(expr)}):",
    ]
    error_status = 400
    for ec in op.get("error_cases", []):
        if any(word in ec["condition"].lower() for word in description.lower().split()):
            error_status = ec["status"]
            break
    lines.append(f'        raise HTTPError({error_status}, "{description}")')
    lines.append("")
    return lines


def _render_precondition_go(pre: dict, op: dict) -> list[str]:
    """Render one precondition as Go guard-clause lines: a runtime `if`
    guard for a codegen-able expression, or a comment stub for a
    `non_codegen(...)` build/provision-time assertion."""
    expr = pre.get("expression", "false /* TODO */")
    description = pre["description"]

    if is_non_codegen(expr):
        return [
            f"\t// BUILD/PROVISION-TIME GUARD (not enforced at runtime): {description}",
            f"\t// {expr}",
            "",
        ]

    error_status = 400
    for ec in op.get("error_cases", []):
        if any(word in ec["condition"].lower() for word in description.lower().split()):
            error_status = ec["status"]
            break
    return [
        f"\t// REQUIRES: {description}",
        f"\tif !({transpile_expression(expr, lang='go')}) {{",
        f'\t\thttp.Error(w, "{description}", {error_status})',
        f"\t\treturn",
        f"\t}}",
        "",
    ]


# ---------------------------------------------------------------------------
# Code generation: deal/icontract DbC decorators
# ---------------------------------------------------------------------------


def generate_deal_decorators(contracts: dict) -> str:
    """Generate Python deal decorator stubs from contract definitions."""
    lines = [
        '"""',
        "Auto-generated Design-by-Contract decorators from formal contracts.",
        "Generated from formal model. Do not edit by hand.",
        '"""',
        "",
        "import deal",
        "",
    ]

    for entity in contracts.get("entities", []):
        entity_name = entity["name"]
        lines.append(f"# === {entity_name} ===")
        lines.append("")

        # Entity-level invariants
        for inv in entity.get("invariants", []):
            lines.extend([
                f"# Invariant: {inv['description']}",
                f"# Expression: {inv.get('expression', 'N/A')}",
                "",
            ])

        for op in entity.get("operations", []):
            func_name = slugify_identifier(op["name"])
            lines.append(f"# {op['method']} {op['path']}")

            # Preconditions
            for pre in op.get("preconditions", []):
                expr = pre.get("expression", "True  # TODO: implement")
                if is_non_codegen(expr):
                    lines.append(f"# BUILD/PROVISION-TIME (not enforced at runtime): {pre['description']} — {expr}")
                    continue
                expr = transpile_expression(expr)
                lines.append(f"@deal.pre(lambda: {expr}, message=\"{pre['description']}\")")

            # Postconditions
            for post in op.get("postconditions", []):
                expr = post.get("expression", "True  # TODO: implement")
                if is_non_codegen(expr):
                    lines.append(f"# BUILD/PROVISION-TIME (not enforced at runtime): {post['description']} — {expr}")
                    continue
                expr = transpile_expression(expr)
                lines.append(f"@deal.post(lambda result: {expr}, message=\"{post['description']}\")")

            lines.extend([
                f"def {func_name}(request):",
                f'    """{op.get("description", op["name"])}"""',
                f"    raise NotImplementedError",
                "",
                "",
            ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Code generation: guard clause templates
# ---------------------------------------------------------------------------


def generate_guards_python(contracts: dict) -> str:
    """Generate Python guard clause templates from contract preconditions."""
    lines = [
        '"""',
        "Auto-generated guard clauses from formal contracts.",
        "Generated from formal model. Do not edit by hand.",
        '"""',
        "",
    ]

    for entity in contracts.get("entities", []):
        for op in entity.get("operations", []):
            func_name = slugify_identifier(op["name"])
            lines.append(f"def {func_name}(request):")
            lines.append(f'    """{op.get("description", op["name"])}"""')

            # Guard clauses from preconditions
            for pre in op.get("preconditions", []):
                lines.extend(_render_precondition_python(pre, op))

            lines.extend([
                "    # --- implementation ---",
                "",
                "    # ENSURES:",
            ])
            for post in op.get("postconditions", []):
                lines.append(f"    # {post['description']}")

            lines.extend(["", ""])

    return "\n".join(lines)


def generate_guards_go(contracts: dict) -> str:
    """Generate Go guard clause templates from contract preconditions."""
    lines = [
        # chief-wiggum#380: this file is a TEMPLATE, not compilable Go —
        # signatures carry contract argument names and bodies are pseudocode.
        # Without the constraint, `go build ./...` picks it up on a Go target
        # and fails with syntax errors, so every re-render silently
        # reintroduced a build break. The blank line after the constraint is
        # required: Go only honours a build tag separated from the package
        # clause by one.
        "//go:build ignore",
        "",
        "// Auto-generated guard clauses from formal contracts.",
        "// Generated from formal model. Do not edit by hand.",
        "//",
        "// This is a generated TEMPLATE, not compilable Go. The build",
        "// constraint above keeps it out of `go build ./...`.",
        "",
        "package handlers",
        "",
        'import "fmt"',
        "",
    ]

    for entity in contracts.get("entities", []):
        for op in entity.get("operations", []):
            func_name = slugify_identifier(op["name"], pascal_case=True)
            lines.append(f"// {op['method']} {op['path']}")
            lines.append(f"func {func_name}(w http.ResponseWriter, r *http.Request) {{")

            for pre in op.get("preconditions", []):
                lines.extend(_render_precondition_go(pre, op))

            lines.extend([
                "\t// --- implementation ---",
                "",
                "\t// ENSURES:",
            ])
            for post in op.get("postconditions", []):
                lines.append(f"\t// {post['description']}")

            lines.extend(["}", "", ""])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    raw_text = Path(args.model).read_text()
    data = json.loads(raw_text)
    schema_type = args.type or detect_schema_type(data)
    errors = validate(data, schema_type)
    # chief-wiggum#293: report-only by default (docs/gate-rollout.md) — a
    # malformed id never hard-fails `validate` unless --strict-ids is passed,
    # so an adopted brownfield repo's pre-existing two-segment models are not
    # broken on arrival.
    malformed = find_malformed_ids(raw_text)

    if errors:
        print(f"INVALID ({schema_type}): {len(errors)} error(s)")
        for e in errors:
            print(f"  - {e}")

    if malformed:
        severity = "ERROR" if args.strict_ids else "WARNING"
        note = "" if args.strict_ids else " (report-only; pass --strict-ids to block)"
        print(
            f"{severity}: malformed stable id(s){note} — two-segment KIND-NNN "
            f"(missing the slug segment) is invisible to the traceability "
            f"scanner (chief-wiggum#281): {', '.join(malformed)}"
        )

    if errors:
        return 1
    if malformed:
        return 1 if args.strict_ids else 0
    print(f"VALID ({schema_type})")
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    data = _load_json(Path(args.model))
    analysis = analyze_graph(data)
    print(f"State Machine: {data['name']}")
    print(f"  States: {len(analysis.states)} ({', '.join(analysis.states)})")
    print(f"  Initial: {analysis.initial}")
    print(f"  Terminal: {', '.join(analysis.terminal_states) or 'none'}")
    print(f"  Transitions: {analysis.transition_count}")
    print(f"  Invalid transitions: {len(analysis.invalid_transitions)}")
    print(f"  Invariants: {analysis.invariant_count}")
    print(f"  Reachable: {', '.join(sorted(analysis.reachable_states))}")
    if analysis.unreachable_states:
        print(f"  UNREACHABLE: {', '.join(sorted(analysis.unreachable_states))}")
    if analysis.dead_states:
        print(f"  DEAD (non-terminal, no outgoing): {', '.join(sorted(analysis.dead_states))}")
    print(f"  All terminals reachable: {analysis.all_terminals_reachable}")

    paths = enumerate_paths(data)
    print(f"  Paths to terminal states: {len(paths)}")
    for i, path in enumerate(paths):
        steps = " → ".join(f"{s['state']}--{s['event']}-->{s['next_state']}" for s in path)
        print(f"    [{i+1}] {steps}")

    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    data = _load_json(Path(args.model))
    if args.format == "xstate":
        result = to_xstate(data)
        print(json.dumps(result, indent=2))
    else:
        print(f"Unknown format: {args.format}", file=sys.stderr)
        return 1
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    data = _load_json(Path(args.model))
    fmt = args.format

    if fmt == "hypothesis":
        print(generate_hypothesis(data))
    elif fmt == "deal":
        print(generate_deal_decorators(data))
    elif fmt == "guards-py":
        print(generate_guards_python(data))
    elif fmt == "guards-go":
        print(generate_guards_go(data))
    else:
        print(f"Unknown format: {fmt}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Formal model parser, validator, and generator"
    )
    sub = parser.add_subparsers(dest="command")

    p_validate = sub.add_parser("validate", help="Validate a model against its schema")
    p_validate.add_argument("model", help="Path to model JSON file")
    p_validate.add_argument("--type", choices=list(SCHEMAS.keys()), help="Schema type (auto-detected if omitted)")
    p_validate.add_argument(
        "--strict-ids", action="store_true",
        help="Fail (exit 1) on malformed stable ids (two-segment KIND-NNN, e.g. INV-001) "
        "invisible to check_traceability.py's scanner (chief-wiggum#293). Report-only by "
        "default (docs/gate-rollout.md): findings print but do not fail an adopted repo's "
        "pre-existing two-segment models on arrival. Opt in for newly authored models.")

    p_graph = sub.add_parser("graph", help="Analyze state machine graph properties")
    p_graph.add_argument("model", help="Path to state machine JSON file")

    p_convert = sub.add_parser("convert", help="Convert model to another format")
    p_convert.add_argument("model", help="Path to model JSON file")
    p_convert.add_argument("--format", required=True, choices=["xstate"], help="Output format")

    p_generate = sub.add_parser("generate", help="Generate code from model")
    p_generate.add_argument("model", help="Path to model JSON file")
    p_generate.add_argument("--format", required=True, choices=["hypothesis", "deal", "guards-py", "guards-go"])

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    dispatch = {
        "validate": cmd_validate,
        "graph": cmd_graph,
        "convert": cmd_convert,
        "generate": cmd_generate,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
