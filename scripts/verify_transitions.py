#!/usr/bin/env python3
"""
Verify state machine transitions against source code.

Compares a state machine model (state-machines.json) against actual status
assignments in Go/TypeScript handler code. Produces a transition-map showing:

- COVERED:      transition in model AND found in code
- MISSING:      transition in model but NOT found in code
- UNDOCUMENTED: status assignment in code with NO model transition

CLI:
    python3 scripts/verify_transitions.py <repo_path> <state-machine.json> [<state-machine2.json> ...]
    python3 scripts/verify_transitions.py <repo_path> <state-machine.json> --ticket "#42"
    python3 scripts/verify_transitions.py <repo_path> <state-machine.json> --output transition-map.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class CodeMatch:
    file: str
    line: int
    handler: str
    target_status: str
    guard_statuses: list[str] = field(default_factory=list)
    raw_line: str = ""


@dataclass
class ModelTransition:
    from_state: str
    to_state: str
    event: str
    tickets: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 1: Parse state machine model
# ---------------------------------------------------------------------------

def load_model(path: Path) -> tuple[str, list[ModelTransition], set[str]]:
    """Load state machine JSON, return (entity_name, transitions, all_states)."""
    with open(path) as f:
        data = json.load(f)

    name = data.get("name", path.stem)
    entity = _extract_entity_name(name)
    states = set(data.get("states", {}).keys())
    transitions = []

    for t in data.get("transitions", []):
        tickets = []
        for p in t.get("derived_from", []):
            if p.get("type") == "ticket":
                tickets.append(p["ref"])
        transitions.append(ModelTransition(
            from_state=t["from"],
            to_state=t["to"],
            event=t.get("event", ""),
            tickets=tickets,
        ))

    return entity, transitions, states


def _extract_entity_name(model_name: str) -> str:
    """Extract entity name from model name like 'Booking Status State Machine'."""
    name = model_name.lower()
    for suffix in [" state machine", " status", " lifecycle"]:
        name = name.replace(suffix, "")
    parts = name.strip().split()
    return "".join(p.capitalize() for p in parts)


# ---------------------------------------------------------------------------
# Phase 2: Grep source code for status assignments
# ---------------------------------------------------------------------------

# Go patterns for status assignments
GO_PATTERNS = [
    # models.BookingStatusCheckedIn → entity=Booking, status=checked_in
    re.compile(r'models\.(\w+)Status(\w+)'),
    # "status": "confirmed" in bson.M
    re.compile(r'"status"\s*:\s*"([a-z_]+)"'),
    # status = "confirmed"
    re.compile(r'status\s*=\s*"([a-z_]+)"'),
]

# Go patterns for status guards (from-state inference)
GO_GUARD_PATTERNS = [
    # "status": string(models.BookingStatusConfirmed) in filters
    re.compile(r'"status"\s*:\s*(?:string\()?models\.(\w+)Status(\w+)'),
    # "status": "confirmed" in filters
    re.compile(r'"status"\s*:\s*"([a-z_]+)"'),
    # "$in": []string{"pending", "confirmed"}
    re.compile(r'"([a-z_]+)"'),
]

# Go handler function pattern
GO_FUNC_PATTERN = re.compile(r'func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(')

# Status assignment context — lines that set status (not just reference it)
GO_SET_PATTERNS = [
    # "status": string(models.XxxStatusYyy) — in bson.M update
    re.compile(r'"status"\s*:\s*string\(models\.(\w+)Status(\w+)\)'),
    # "status": "value" — in bson.M update
    re.compile(r'"status"\s*:\s*"([a-z_]+)"'),
    # .Status = models.XxxStatusYyy
    re.compile(r'\.Status\s*=\s*models\.(\w+)Status(\w+)'),
    # booking["status"] = "value"
    re.compile(r'\["status"\]\s*=\s*"([a-z_]+)"'),
]


def camel_to_snake(name: str) -> str:
    """Convert CamelCase to snake_case."""
    s1 = re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


def scan_go_files(repo_path: Path) -> list[CodeMatch]:
    """Scan Go files for status assignments."""
    matches = []

    for go_file in repo_path.rglob("*.go"):
        rel = go_file.relative_to(repo_path)
        if any(p in str(rel) for p in ["vendor/", "node_modules/", ".git/"]):
            continue
        if str(rel).endswith("_test.go"):
            continue

        try:
            lines = go_file.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        current_func = ""
        func_guard_statuses: list[str] = []

        for i, line in enumerate(lines, 1):
            # Track current function
            func_match = GO_FUNC_PATTERN.search(line)
            if func_match:
                current_func = func_match.group(1)
                func_guard_statuses = []

            # Track guard statuses in filters (for from-state inference)
            # Look for status checks in MongoDB filters
            if '"status"' in line and ("$in" in line or "FindOne" in "".join(lines[max(0,i-5):i])):
                for gp in GO_GUARD_PATTERNS:
                    for gm in gp.finditer(line):
                        if len(gm.groups()) == 2:
                            func_guard_statuses.append(camel_to_snake(gm.group(2)))
                        elif len(gm.groups()) == 1:
                            val = gm.group(1)
                            if val not in ("$in", "$ne", "$set", "status", "$or"):
                                func_guard_statuses.append(val)

            # Detect status SET operations (not just references)
            for sp in GO_SET_PATTERNS:
                for sm in sp.finditer(line):
                    # Skip lines that are clearly filters, not updates
                    stripped = line.strip()
                    if stripped.startswith("//"):
                        continue

                    if len(sm.groups()) == 2:
                        entity_prefix = sm.group(1)
                        status_val = camel_to_snake(sm.group(2))
                    elif len(sm.groups()) == 1:
                        entity_prefix = ""
                        status_val = sm.group(1)
                    else:
                        continue

                    # Determine if this is in an update context (not a filter)
                    context_window = lines[max(0, i-8):i]
                    context_text = "\n".join(context_window)
                    is_update = any(kw in context_text for kw in [
                        "$set", "Update", "update", "bson.M{",
                        "status:", "Status =", "Status:",
                    ])
                    is_filter = any(kw in stripped for kw in [
                        "FindOne(", "Find(", "CountDocuments(",
                        "filter", "Filter",
                    ])

                    if not is_update and not is_filter:
                        # Check broader context
                        broad_context = "\n".join(lines[max(0, i-15):i+2])
                        is_update = "$set" in broad_context

                    if is_filter and not is_update:
                        continue

                    # Collect guard statuses from the enclosing function
                    guards = list(set(func_guard_statuses))

                    matches.append(CodeMatch(
                        file=str(rel),
                        line=i,
                        handler=current_func,
                        target_status=status_val,
                        guard_statuses=guards,
                        raw_line=stripped,
                    ))

    return matches


# ---------------------------------------------------------------------------
# Phase 3: Diff model vs code
# ---------------------------------------------------------------------------

@dataclass
class TransitionResult:
    from_state: str
    to_state: str
    event: str
    tickets: list[str]
    status: str  # covered, missing, planned
    code_locations: list[dict] = field(default_factory=list)


@dataclass
class UndocumentedResult:
    from_state: str  # "*" if unknown
    to_state: str
    code_locations: list[dict] = field(default_factory=list)


def diff_transitions(
    entity: str,
    model_transitions: list[ModelTransition],
    code_matches: list[CodeMatch],
    all_model_states: set[str],
    ticket_filter: str | None = None,
) -> tuple[list[TransitionResult], list[UndocumentedResult]]:
    """Compare model transitions against code matches."""

    # Filter model transitions by ticket if specified
    if ticket_filter:
        model_transitions = [
            t for t in model_transitions
            if ticket_filter in t.tickets
        ]

    # Build set of model target states for quick lookup
    model_targets = {(t.from_state, t.to_state) for t in model_transitions}
    all_model_target_states = {t.to_state for t in model_transitions}

    # Match code to model
    results = []
    matched_code = set()

    for mt in model_transitions:
        matching_code = []
        for j, cm in enumerate(code_matches):
            if cm.target_status == mt.to_state:
                # Check if guards match the from-state
                if cm.guard_statuses and mt.from_state not in cm.guard_statuses:
                    # Guard exists but doesn't include this from-state
                    # Still could be a match if the handler handles multiple transitions
                    pass
                matching_code.append((j, cm))

        if matching_code:
            code_locs = []
            for j, cm in matching_code:
                matched_code.add(j)
                loc = {"file": cm.file, "line": cm.line, "handler": cm.handler}
                if cm.guard_statuses:
                    loc["guard"] = "status in (" + ", ".join(cm.guard_statuses) + ")"
                code_locs.append(loc)

            results.append(TransitionResult(
                from_state=mt.from_state,
                to_state=mt.to_state,
                event=mt.event,
                tickets=mt.tickets,
                status="covered",
                code_locations=code_locs,
            ))
        else:
            results.append(TransitionResult(
                from_state=mt.from_state,
                to_state=mt.to_state,
                event=mt.event,
                tickets=mt.tickets,
                status="missing",
            ))

    # Find undocumented: code matches not accounted for by any model transition
    undocumented = []
    code_targets_seen: dict[str, list[dict]] = {}
    for j, cm in enumerate(code_matches):
        if j in matched_code:
            continue
        # Check if this target state exists in the model at all
        if cm.target_status not in all_model_states:
            continue  # Unknown status, skip (likely a different entity)

        key = cm.target_status
        if key not in code_targets_seen:
            code_targets_seen[key] = []
        code_targets_seen[key].append({
            "file": cm.file,
            "line": cm.line,
            "handler": cm.handler,
        })

    for target_status, locs in code_targets_seen.items():
        # Check if any model transition targets this state
        model_has_target = any(t.to_state == target_status for t in model_transitions)
        if not model_has_target:
            from_state = "*"
            # Try to infer from guards
            all_guards = set()
            for loc_info in locs:
                for cm in code_matches:
                    if cm.file == loc_info["file"] and cm.line == loc_info["line"]:
                        all_guards.update(cm.guard_statuses)
            if len(all_guards) == 1:
                from_state = all_guards.pop()

            undocumented.append(UndocumentedResult(
                from_state=from_state,
                to_state=target_status,
                code_locations=locs,
            ))

    return results, undocumented


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def format_text(
    entity: str,
    results: list[TransitionResult],
    undocumented: list[UndocumentedResult],
) -> str:
    """Format results as human-readable text."""
    lines = [f"\n=== Transition Map Verification: {entity} ===\n"]

    covered = [r for r in results if r.status == "covered"]
    missing = [r for r in results if r.status == "missing"]

    if covered:
        lines.append(f"COVERED ({len(covered)}):")
        for r in covered:
            tickets = ", ".join(r.tickets) if r.tickets else "—"
            locs = "; ".join(f"{cl['file']}:{cl['line']}" for cl in r.code_locations)
            lines.append(f"  {r.from_state} -> {r.to_state} ({r.event})  {tickets}  {locs}")
        lines.append("")

    if missing:
        lines.append(f"MISSING ({len(missing)}):")
        for r in missing:
            tickets = ", ".join(r.tickets) if r.tickets else "—"
            lines.append(f"  {r.from_state} -> {r.to_state} ({r.event})  {tickets}  -- no code match found")
        lines.append("")

    if undocumented:
        lines.append(f"UNDOCUMENTED ({len(undocumented)}):")
        for u in undocumented:
            locs = "; ".join(f"{cl['file']}:{cl['line']}" for cl in u.code_locations)
            lines.append(f"  {u.from_state} -> {u.to_state}  {locs}  -- no model transition")
        lines.append("")

    total = len(results)
    cov = len(covered)
    pct = (cov / total * 100) if total > 0 else 0
    lines.append(f"Summary: {cov} covered, {len(missing)} missing, {len(undocumented)} undocumented ({pct:.0f}% coverage)")

    return "\n".join(lines)


def build_json(
    repo_path: str,
    all_entities: list[tuple[str, list[TransitionResult], list[UndocumentedResult]]],
) -> dict:
    """Build the transition-map JSON."""
    entities = []
    total_model = 0
    total_covered = 0
    total_missing = 0
    total_undocumented = 0

    for entity_name, results, undocumented in all_entities:
        covered = [r for r in results if r.status == "covered"]
        missing = [r for r in results if r.status == "missing"]

        mapped = []
        for r in results:
            t = {
                "from": r.from_state,
                "to": r.to_state,
                "event": r.event,
                "status": r.status,
            }
            if r.tickets:
                t["tickets"] = r.tickets
            if r.code_locations:
                t["code_locations"] = r.code_locations
            t["verified_at"] = datetime.now(timezone.utc).isoformat()
            mapped.append(t)

        undoc = []
        for u in undocumented:
            entry: dict[str, Any] = {"to": u.to_state}
            if u.from_state:
                entry["from"] = u.from_state
            if u.code_locations:
                entry["code_locations"] = u.code_locations
            undoc.append(entry)

        entity_doc: dict[str, Any] = {
            "name": entity_name,
            "transitions": mapped,
        }
        if undoc:
            entity_doc["undocumented"] = undoc
        entities.append(entity_doc)

        total_model += len(results)
        total_covered += len(covered)
        total_missing += len(missing)
        total_undocumented += len(undocumented)

    return {
        "entities": entities,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": repo_path,
        "summary": {
            "total_model_transitions": total_model,
            "covered": total_covered,
            "missing": total_missing,
            "undocumented": total_undocumented,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Verify state machine transitions against source code")
    parser.add_argument("repo_path", help="Path to the target repository")
    parser.add_argument("model_files", nargs="+", help="State machine JSON file(s)")
    parser.add_argument("--ticket", help="Filter to transitions for a specific ticket (e.g., '#42')")
    parser.add_argument("--output", "-o", help="Write transition-map JSON to this file")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    args = parser.parse_args()

    repo = Path(args.repo_path).resolve()
    if not repo.is_dir():
        print(f"Error: {repo} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Scan code once
    print(f"Scanning {repo} for status assignments...", file=sys.stderr)
    code_matches = scan_go_files(repo)
    print(f"Found {len(code_matches)} status assignments in code", file=sys.stderr)

    all_entities = []

    for model_path_str in args.model_files:
        model_path = Path(model_path_str)
        if not model_path.exists():
            print(f"Warning: {model_path} not found, skipping", file=sys.stderr)
            continue

        entity, model_transitions, all_states = load_model(model_path)
        print(f"Loaded model: {entity} ({len(model_transitions)} transitions, {len(all_states)} states)", file=sys.stderr)

        # Filter code matches to those whose target status is in this model's states
        entity_code_matches = [
            cm for cm in code_matches
            if cm.target_status in all_states
        ]

        results, undocumented = diff_transitions(
            entity, model_transitions, entity_code_matches, all_states, args.ticket,
        )
        all_entities.append((entity, results, undocumented))

        if args.format == "text":
            print(format_text(entity, results, undocumented))

    if args.format == "json" or args.output:
        doc = build_json(str(repo), all_entities)
        if args.output:
            with open(args.output, "w") as f:
                json.dump(doc, f, indent=2)
            print(f"Wrote transition-map to {args.output}", file=sys.stderr)
        if args.format == "json":
            print(json.dumps(doc, indent=2))


if __name__ == "__main__":
    main()
