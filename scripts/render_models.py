#!/usr/bin/env python3
"""
Multi-rendering framework for formal models.

Takes structured model JSON (state machines, contracts) and renders three views:

  Human view  — Markdown + Mermaid (backward-compatible with /architect output)
  Machine view — XState JSON, deal decorators, Hypothesis skeletons, guard templates
  Test view   — Test paths from @xstate/graph, assertion templates, coverage report

CLI:
    python3 scripts/render_models.py <model.json> --view human|machine|test|all --output <dir>
    python3 scripts/render_models.py <dir-of-models> --view all --output <dir>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Import sibling module
sys.path.insert(0, str(Path(__file__).resolve().parent))
import formal_models as fm

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Mermaid color palette (matches /implement Step 10 PR creation)
# ---------------------------------------------------------------------------

COLORS = {
    "existing": "#003f5c",
    "modified": "#665191",
    "new": "#d45087",
    "entry": "#ff7c43",
    "highlight": "#ffa600",
}


# ---------------------------------------------------------------------------
# Human view: Markdown + Mermaid
# ---------------------------------------------------------------------------


def render_state_machine_human(sm: dict) -> str:
    """Render a state machine model to prose markdown matching /architect format."""
    lines = [f"## {sm['name']}", ""]

    if sm.get("description"):
        lines.append(sm["description"])
        lines.append("")

    # Mermaid stateDiagram
    lines.append("```mermaid")
    lines.append("stateDiagram-v2")

    # Initial state
    lines.append(f"    [*] --> {sm['initial']}")

    # Transitions
    for t in sm.get("transitions", []):
        label = t["event"]
        if t.get("guards"):
            guard_text = ", ".join(g["description"] for g in t["guards"])
            label += f" [{guard_text}]"
        lines.append(f"    {t['from']} --> {t['to']}: {label}")

    # Terminal states
    for state_id, state_def in sm.get("states", {}).items():
        if state_def.get("type") == "terminal":
            lines.append(f"    {state_id} --> [*]")

    lines.append("```")
    lines.append("")

    # States table
    lines.append("### States")
    for state_id, state_def in sm.get("states", {}).items():
        desc = state_def.get("description", "")
        stype = state_def.get("type", "normal")
        marker = ""
        if stype == "initial":
            marker = " (initial)"
        elif stype == "terminal":
            marker = " (terminal)"
        lines.append(f"- `{state_id}`{marker} — {desc}")
    lines.append("")

    # Transitions table
    lines.append("### Transitions")
    lines.append("| From | To | Trigger | Guard Conditions |")
    lines.append("|------|----|---------|-----------------|")
    for t in sm.get("transitions", []):
        guards = ", ".join(g["description"] for g in t.get("guards", [])) or "—"
        lines.append(f"| {t['from']} | {t['to']} | {t['event']} | {guards} |")
    lines.append("")

    # Invalid transitions
    invalid = sm.get("invalid_transitions", [])
    if invalid:
        lines.append("### Invalid Transitions (must be rejected)")
        for it in invalid:
            lines.append(f"- {it['from']} → {it['to']} ({it.get('reason', 'invalid')})")
        lines.append("")

    # Invariants
    invariants = sm.get("invariants", [])
    if invariants:
        lines.append("### Invariants")
        for inv in invariants:
            cat = inv.get("category", "")
            cat_label = f" [{cat}]" if cat else ""
            lines.append(f"- **{inv['id']}**{cat_label}: {inv['description']}")
        lines.append("")

    return "\n".join(lines)


def render_contracts_human(contracts: dict) -> str:
    """Render contract definitions to prose markdown matching /architect format."""
    lines = []

    for entity in contracts.get("entities", []):
        lines.append(f"## Entity: {entity['name']}")
        lines.append("")

        if entity.get("description"):
            lines.append(entity["description"])
            lines.append("")

        # Fields table
        lines.append("### Canonical Fields")
        lines.append("| Field | Type | Required | Source of Truth | Notes |")
        lines.append("|-------|------|----------|-----------------|-------|")
        for f in entity.get("fields", []):
            req = f.get("required", "optional")
            if req == "conditional" and f.get("required_when"):
                req = f"after {f['required_when']}"
            sot = f.get("source_of_truth", "—")
            notes = f.get("notes", "—")
            if f.get("immutable"):
                notes = "immutable" + (f"; {notes}" if notes != "—" else "")
            lines.append(f"| {f['name']} | {f['type']} | {req} | {sot} | {notes} |")
        lines.append("")

        # Operations
        for op in entity.get("operations", []):
            lines.append(f"### {op['method']} {op['path']}")
            if op.get("description"):
                lines.append(op["description"])
            lines.append("")

            # Preconditions
            pres = op.get("preconditions", [])
            if pres:
                pre_text = "; ".join(p["description"] for p in pres)
                lines.append(f"- **REQUIRES**: {pre_text}")

            # Postconditions
            posts = op.get("postconditions", [])
            if posts:
                post_text = "; ".join(p["description"] for p in posts)
                lines.append(f"- **ENSURES**: {post_text}")

            # Error cases
            errors = op.get("error_cases", [])
            if errors:
                err_text = "; ".join(f"{e['status']} if {e['condition']}" for e in errors)
                lines.append(f"- **ERROR CASES**: {err_text}")

            # State transition
            if op.get("state_transition"):
                lines.append(f"- **STATE TRANSITION**: {op['state_transition']}")

            lines.append("")

        # Entity invariants
        for inv in entity.get("invariants", []):
            lines.append(f"- **INVARIANT**: {inv['description']}")
        if entity.get("invariants"):
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Machine view: XState JSON, deal decorators, Hypothesis, guards
# ---------------------------------------------------------------------------


def render_machine_view(model: dict, output_dir: Path, schema_type: str) -> list[str]:
    """Generate machine-readable artifacts. Returns list of files created."""
    files = []

    if schema_type == "state-machine":
        # XState JSON
        xstate = fm.to_xstate(model)
        xstate_path = output_dir / "xstate-machine.json"
        xstate_path.write_text(json.dumps(xstate, indent=2))
        files.append(str(xstate_path))

        # Hypothesis RuleBasedStateMachine
        hyp_code = fm.generate_hypothesis(model)
        hyp_path = output_dir / "test_state_machine.py"
        hyp_path.write_text(hyp_code)
        files.append(str(hyp_path))

    elif schema_type == "contracts":
        # deal decorators
        deal_code = fm.generate_deal_decorators(model)
        deal_path = output_dir / "contracts_deal.py"
        deal_path.write_text(deal_code)
        files.append(str(deal_path))

        # Python guard clauses
        guards_py = fm.generate_guards_python(model)
        guards_py_path = output_dir / "guards.py"
        guards_py_path.write_text(guards_py)
        files.append(str(guards_py_path))

        # Go guard clauses
        guards_go = fm.generate_guards_go(model)
        guards_go_path = output_dir / "guards.go"
        guards_go_path.write_text(guards_go)
        files.append(str(guards_go_path))

    return files


# ---------------------------------------------------------------------------
# Test view: paths from @xstate/graph, assertion templates, coverage
# ---------------------------------------------------------------------------


def run_xstate_paths(xstate_json: dict) -> dict | None:
    """Shell out to Node xstate_paths.js. Returns parsed output or None on failure."""
    script = REPO_ROOT / "scripts" / "xstate_paths.js"
    if not script.exists():
        print(f"WARNING: {script} not found, skipping XState path generation", file=sys.stderr)
        return None

    try:
        result = subprocess.run(
            ["node", str(script)],
            input=json.dumps(xstate_json),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(f"WARNING: xstate_paths.js failed: {result.stderr.strip()}", file=sys.stderr)
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        print(f"WARNING: xstate_paths.js error: {e}", file=sys.stderr)
        return None


def render_test_view(model: dict, output_dir: Path, schema_type: str) -> list[str]:
    """Generate test artifacts. Returns list of files created."""
    files = []

    if schema_type == "state-machine":
        # XState path generation via Node bridge
        xstate = fm.to_xstate(model)
        path_data = run_xstate_paths(xstate)

        if path_data:
            # Write raw path data
            paths_path = output_dir / "test-paths.json"
            paths_path.write_text(json.dumps(path_data, indent=2))
            files.append(str(paths_path))

            # Generate human-readable test plan
            plan_lines = [
                f"# Test Plan: {model['name']}",
                "",
                f"Generated from formal model. {path_data['summary']['total_paths']} paths covering "
                f"{path_data['summary']['states_covered']}/{path_data['summary']['states_total']} states "
                f"and {path_data['summary']['transitions_covered']} transitions.",
                "",
                "## Positive Test Cases (valid paths)",
                "",
            ]
            for i, path in enumerate(path_data["paths"], 1):
                steps_str = " → ".join(
                    f"{s['state']}--{s['event']}-->{s['next_state']}"
                    for s in path["steps"]
                )
                plan_lines.append(f"### Path {i}: → {path['target']}")
                plan_lines.append(f"```")
                plan_lines.append(steps_str)
                plan_lines.append(f"```")
                plan_lines.append("")

            # Invalid transition tests
            invalid = model.get("invalid_transitions", [])
            if invalid:
                plan_lines.append("## Negative Test Cases (must be rejected)")
                plan_lines.append("")
                for it in invalid:
                    plan_lines.append(
                        f"- **{it['from']} → {it['to']}**: {it.get('reason', 'invalid transition')} "
                        f"— expect 400/409"
                    )
                plan_lines.append("")

            # Invariant checks
            invariants = model.get("invariants", [])
            if invariants:
                plan_lines.append("## Invariant Checks (verify at each state)")
                plan_lines.append("")
                for inv in invariants:
                    scope = ""
                    if inv.get("scope") == "state-specific" and inv.get("applies_to_states"):
                        scope = f" (in states: {', '.join(inv['applies_to_states'])})"
                    plan_lines.append(f"- **{inv['id']}**{scope}: {inv['description']}")
                plan_lines.append("")

            # Coverage summary
            plan_lines.extend([
                "## Coverage Summary",
                "",
                f"| Metric | Value |",
                f"|--------|-------|",
                f"| Total paths | {path_data['summary']['total_paths']} |",
                f"| States covered | {path_data['summary']['states_covered']}/{path_data['summary']['states_total']} |",
                f"| Transitions covered | {path_data['summary']['transitions_covered']} |",
                f"| Invalid transitions to test | {len(invalid)} |",
                f"| Invariants to verify | {len(invariants)} |",
                "",
            ])

            plan_path = output_dir / "test-plan.md"
            plan_path.write_text("\n".join(plan_lines))
            files.append(str(plan_path))
        else:
            # Fallback: use Python path enumeration
            paths = fm.enumerate_paths(model)
            fallback = {
                "paths": [
                    {
                        "target": p[-1]["next_state"] if p else model["initial"],
                        "steps": p,
                        "length": len(p),
                    }
                    for p in paths
                ],
                "summary": {
                    "total_paths": len(paths),
                    "note": "Generated by Python fallback (Node bridge unavailable)",
                },
            }
            paths_path = output_dir / "test-paths.json"
            paths_path.write_text(json.dumps(fallback, indent=2))
            files.append(str(paths_path))

    elif schema_type == "contracts":
        # Generate assertion templates per operation
        lines = [
            "# Contract Assertion Templates",
            "",
            "Generated from formal contracts. Each operation has precondition and postcondition checks.",
            "",
        ]

        for entity in model.get("entities", []):
            for op in entity.get("operations", []):
                lines.append(f"## {op['name']} ({op['method']} {op['path']})")
                lines.append("")

                lines.append("### Precondition Tests")
                for pre in op.get("preconditions", []):
                    lines.append(f"- [ ] **{pre.get('id', 'PRE')}**: Verify {pre['description']}")
                    lines.append(f"  - Call WITHOUT this condition → expect error")
                lines.append("")

                lines.append("### Postcondition Tests")
                for post in op.get("postconditions", []):
                    lines.append(f"- [ ] **{post.get('id', 'POST')}**: Verify {post['description']}")
                    lines.append(f"  - Call correctly → assert postcondition holds")
                lines.append("")

                lines.append("### Error Case Tests")
                for ec in op.get("error_cases", []):
                    lines.append(f"- [ ] Status {ec['status']}: {ec['condition']}")
                lines.append("")

        assertions_path = output_dir / "contract-assertions.md"
        assertions_path.write_text("\n".join(lines))
        files.append(str(assertions_path))

    return files


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def render_model(model_path: Path, view: str, output_dir: Path) -> list[str]:
    """Render a single model file. Returns list of files created."""
    model = fm._load_json(model_path)
    schema_type = fm.detect_schema_type(model)

    # Validate first
    errors = fm.validate(model, schema_type)
    if errors:
        print(f"ERROR: {model_path} fails validation:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    files_created = []

    if view in ("human", "all"):
        if schema_type == "state-machine":
            md = render_state_machine_human(model)
            out = output_dir / f"{model_path.stem}.md"
            out.write_text(md)
            files_created.append(str(out))
        elif schema_type == "contracts":
            md = render_contracts_human(model)
            out = output_dir / f"{model_path.stem}.md"
            out.write_text(md)
            files_created.append(str(out))

    if view in ("machine", "all"):
        files_created.extend(render_machine_view(model, output_dir, schema_type))

    if view in ("test", "all"):
        files_created.extend(render_test_view(model, output_dir, schema_type))

    return files_created


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-rendering framework for formal models")
    parser.add_argument("input", help="Model JSON file or directory of model files")
    parser.add_argument("--view", required=True, choices=["human", "machine", "test", "all"])
    parser.add_argument("--output", required=True, help="Output directory")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output)

    if input_path.is_dir():
        model_files = sorted(input_path.glob("*.json"))
    else:
        model_files = [input_path]

    all_files = []
    for model_path in model_files:
        files = render_model(model_path, args.view, output_dir)
        all_files.extend(files)

    if all_files:
        print(f"Generated {len(all_files)} file(s):")
        for f in all_files:
            print(f"  {f}")
    else:
        print("No files generated.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
