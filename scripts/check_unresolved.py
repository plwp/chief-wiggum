#!/usr/bin/env python3
"""
Scan epic architecture artifacts for unresolved external unknowns.

The marker convention: any artifact string (JSON model field or markdown prose)
containing an UPPERCASE `TBD`, `UNRESOLVED`, or `PLACEHOLDER` token marks a fact
that could not be confirmed against a real source (schema names, config values,
external endpoints, metric definitions, ...). Authors write e.g.

    "expression": "orders.total_cents > 0  -- TBD: confirm column against dbt model"
    "UNRESOLVED: which Cloudflare account hosts the prod stream?"

Unknowns must gate dependent work, not silently propagate into implementation.
/architect runs this after synthesis and reports open unknowns as blockers;
/implement-wave runs it before each wave and gates tickets whose artifacts
carry markers.

CLI:
    python3 scripts/check_unresolved.py <epic-dir-or-file> [...] [--format text|json]

Exit codes: 0 = no unresolved markers, 1 = markers found, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Uppercase-only, whole-word: lowercase "tbd"/"placeholder" in normal prose
# (e.g. an input field's placeholder text) must not trip the gate.
MARKER_RE = re.compile(r"\b(TBD|UNRESOLVED|PLACEHOLDER)\b")

SCANNED_SUFFIXES = {".json", ".md"}


@dataclass
class Finding:
    file: str
    location: str  # JSON path or "line N"
    marker: str
    text: str
    tickets: list[str]  # tickets this finding blocks (from derived_from provenance)


def _provenance_tickets(ancestors: list) -> list[str]:
    """Collect ticket refs from derived_from blocks on the value or its ancestors."""
    tickets: list[str] = []
    for node in ancestors:
        if not isinstance(node, dict):
            continue
        for prov in node.get("derived_from", []) or []:
            if isinstance(prov, dict) and prov.get("type") in ("ticket", "acceptance_criterion"):
                ref = str(prov.get("ref", ""))
                if ref and ref not in tickets:
                    tickets.append(ref)
    return tickets


def scan_json(path: Path) -> list[Finding]:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: cannot parse {path}: {exc}", file=sys.stderr)
        return []

    findings: list[Finding] = []

    def walk(node, json_path: str, ancestors: list) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{json_path}.{key}", ancestors + [node])
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{json_path}[{i}]", ancestors)
        elif isinstance(node, str):
            match = MARKER_RE.search(node)
            if match:
                findings.append(Finding(
                    file=str(path),
                    location=json_path.lstrip("."),
                    marker=match.group(1),
                    text=node.strip()[:200],
                    tickets=_provenance_tickets(ancestors),
                ))

    walk(data, "", [])
    return findings


def scan_markdown(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        print(f"WARNING: cannot read {path}: {exc}", file=sys.stderr)
        return []
    for lineno, line in enumerate(lines, start=1):
        match = MARKER_RE.search(line)
        if match:
            ticket_refs = re.findall(r"#\d+", line)
            findings.append(Finding(
                file=str(path),
                location=f"line {lineno}",
                marker=match.group(1),
                text=line.strip()[:200],
                tickets=ticket_refs,
            ))
    return findings


def collect_files(targets: list[Path]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        if target.is_dir():
            files.extend(sorted(p for p in target.rglob("*") if p.suffix in SCANNED_SUFFIXES))
        elif target.suffix in SCANNED_SUFFIXES:
            files.append(target)
    return files


def scan(targets: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in collect_files(targets):
        if path.suffix == ".json":
            findings.extend(scan_json(path))
        else:
            findings.extend(scan_markdown(path))
    return findings


def blocked_tickets(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        for ticket in f.tickets:
            counts[ticket] = counts.get(ticket, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan epic artifacts for unresolved external unknowns (TBD/UNRESOLVED/PLACEHOLDER markers)"
    )
    parser.add_argument("targets", nargs="+", help="Epic directory or artifact file(s) to scan")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    targets = [Path(t) for t in args.targets]
    missing = [t for t in targets if not t.exists()]
    if missing:
        for t in missing:
            print(f"ERROR: {t} does not exist", file=sys.stderr)
        return 2

    findings = scan(targets)
    blocked = blocked_tickets(findings)

    if args.format == "json":
        print(json.dumps({
            "findings": [asdict(f) for f in findings],
            "blocked_tickets": blocked,
            "count": len(findings),
        }, indent=2))
    else:
        if not findings:
            print("OK: no unresolved markers found")
        else:
            print(f"UNRESOLVED: {len(findings)} marker(s) found\n")
            for f in findings:
                tickets = f" [blocks {', '.join(f.tickets)}]" if f.tickets else ""
                print(f"  {f.file} ({f.location}){tickets}")
                print(f"    {f.marker}: {f.text}")
            if blocked:
                print("\nTickets blocked by unresolved unknowns:")
                for ticket, count in sorted(blocked.items()):
                    print(f"  {ticket}: {count} unresolved marker(s)")

    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        import os
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from factory_log import emit_gate
        repo = os.path.basename(os.path.abspath(str(targets[0]))) if targets else None
        emit_gate("check_unresolved", "fail" if findings else "pass",
                  caught=len(findings), repo=repo)
    except Exception:
        pass

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
