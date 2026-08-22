#!/usr/bin/env python3
"""CLI for the transactional DAG journal and graph engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.dag.journal import GraphJournal, JournalError  # noqa: E402


def _init(args: argparse.Namespace) -> int:
    journal = GraphJournal(args.db)
    try:
        journal.init_graph(args.graph_id)
    except JournalError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    finally:
        journal.close()
    print(json.dumps({"ok": True, "graph_id": args.graph_id}))
    return 0


def _propose(args: argparse.Namespace) -> int:
    envelope = json.loads(Path(args.envelope).read_bytes())
    journal = GraphJournal(args.db)
    try:
        decision = journal.propose(envelope)
    except JournalError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    finally:
        journal.close()
    result = {
        "accepted": decision.accepted,
        "journal_seq": decision.journal_seq,
        "graph_revision": decision.graph_revision,
        "reason": decision.reason,
    }
    if not decision.accepted:
        result["violations"] = [v.to_dict() for v in decision.violations]
    print(json.dumps(result, indent=2))
    return 0 if decision.accepted else 1


def _inspect(args: argparse.Namespace) -> int:
    journal = GraphJournal(args.db)
    try:
        state = journal.replay()
        hashes = journal.hash()
    except JournalError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    finally:
        journal.close()
    if args.human:
        print(f"Graph: {state.get('graph_id', '(uninitialised)')}")
        print(f"Revision: {state.get('graph_revision', 0)}")
        print(f"Schedule hash: {hashes['schedule_hash'][:16]}…")
        print(f"Audit hash: {hashes['audit_state_hash'][:16]}…")
        print(f"Execution nodes: {len(state.get('execution_nodes', []))}")
        print(f"Schedulable edges: {len(state.get('schedulable_edges', []))}")
    else:
        print(json.dumps(state, indent=2))
    return 0


def _replay(args: argparse.Namespace) -> int:
    journal = GraphJournal(args.db)
    try:
        state = journal.replay()
    except JournalError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    finally:
        journal.close()
    print(json.dumps(state, indent=2))
    return 0


def _hash(args: argparse.Namespace) -> int:
    journal = GraphJournal(args.db)
    try:
        hashes = journal.hash()
    except JournalError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    finally:
        journal.close()
    print(json.dumps(hashes))
    return 0


def _project(args: argparse.Namespace) -> int:
    from chief_wiggum import planning

    journal = GraphJournal(args.db)
    try:
        state = journal.replay()
    except JournalError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    finally:
        journal.close()

    ticket_nodes = [n for n in state.get("intent_nodes", []) if isinstance(n.get("source_ticket"), int) and n.get("in_scope", True)]
    issues = sorted({n["source_ticket"] for n in ticket_nodes})
    edges: dict[int, list[int]] = {ticket: [] for ticket in issues}
    for edge in state.get("intent_edges", []):
        src, tgt = edge.get("source_ticket"), edge.get("target_ticket")
        if isinstance(src, int) and isinstance(tgt, int):
            edges.setdefault(src, []).append(tgt)

    try:
        plan = planning.plan_waves(issues, edges, closed=[], gated=[])
    except planning.DependencyCycleError as exc:
        print(f"Error: cycle: {exc}", file=sys.stderr)
        return 2

    output = plan.to_dict()
    print(json.dumps(output, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transactional DAG engine CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_db(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", required=True, help="path to the SQLite journal database")

    init = sub.add_parser("init", help="initialise a new graph")
    init.add_argument("--db", required=True); init.add_argument("--graph-id", required=True)
    init.set_defaults(func=_init)
    propose = sub.add_parser("propose", help="submit a mutation envelope")
    propose.add_argument("--db", required=True); propose.add_argument("envelope")
    propose.set_defaults(func=_propose)
    inspect_p = sub.add_parser("inspect", help="show current graph state")
    inspect_p.add_argument("--db", required=True); inspect_p.add_argument("--human", action="store_true")
    inspect_p.set_defaults(func=_inspect)
    replay = sub.add_parser("replay", help="reconstruct graph from journal")
    replay.add_argument("--db", required=True); replay.set_defaults(func=_replay)
    hash_p = sub.add_parser("hash", help="print canonical schedule and audit hashes")
    hash_p.add_argument("--db", required=True); hash_p.set_defaults(func=_hash)
    project = sub.add_parser("project", help="emit the legacy six-field wave plan")
    project.add_argument("--db", required=True); project.set_defaults(func=_project)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
