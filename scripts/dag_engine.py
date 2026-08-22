#!/usr/bin/env python3
"""CLI for the transactional DAG journal and graph engine.

Exit codes are distinct on purpose: 0 success, 1 a decision the engine made
(rejection / an input it refuses to project), 2 a dependency cycle, 3 an engine
or storage failure. Collapsing "rejected" and "crashed" into one code is what
lets a caller read a broken engine as a clean run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.dag import compatibility  # noqa: E402
from chief_wiggum.dag.journal import GraphJournal, JournalError  # noqa: E402

EXIT_OK = 0
EXIT_DECISION = 1
EXIT_CYCLE = 2
EXIT_ERROR = 3


def _fail(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}, indent=2))
    return EXIT_ERROR


def _open(args: argparse.Namespace) -> GraphJournal:
    return GraphJournal(args.db)


def _init(args: argparse.Namespace) -> int:
    try:
        journal = _open(args)
    except JournalError as exc:
        return _fail(str(exc))
    try:
        journal.init_graph(args.graph_id)
    except JournalError as exc:
        return _fail(str(exc))
    finally:
        journal.close()
    print(json.dumps({"ok": True, "graph_id": args.graph_id}, indent=2))
    return EXIT_OK


def _decision_output(decision: Any) -> dict[str, Any]:
    result = {
        "accepted": decision.accepted,
        "idempotent": decision.idempotent,
        "journal_seq": decision.journal_seq,
        "graph_revision": decision.graph_revision,
        "reason": decision.reason,
    }
    if not decision.accepted:
        result["violations"] = [violation.to_dict() for violation in decision.violations]
    return result


def _propose(args: argparse.Namespace) -> int:
    try:
        envelope = json.loads(Path(args.envelope).read_bytes())
    except (OSError, ValueError) as exc:
        return _fail(f"cannot read envelope {args.envelope}: {exc}")
    try:
        journal = _open(args)
    except JournalError as exc:
        return _fail(str(exc))
    try:
        decision = journal.propose(envelope, actor_note=getattr(args, "note", "") or "")
    except JournalError as exc:
        return _fail(str(exc))
    finally:
        journal.close()
    print(json.dumps(_decision_output(decision), indent=2))
    return EXIT_OK if decision.accepted else EXIT_DECISION


def _admit(args: argparse.Namespace) -> int:
    """Propose, and treat a rejection as a hard failure of the caller's intent."""
    return _propose(args)


def _reject(args: argparse.Namespace) -> int:
    """Record an operator's refusal of an envelope without applying it."""
    try:
        envelope = json.loads(Path(args.envelope).read_bytes())
    except (OSError, ValueError) as exc:
        return _fail(f"cannot read envelope {args.envelope}: {exc}")
    try:
        journal = _open(args)
    except JournalError as exc:
        return _fail(str(exc))
    try:
        decision = journal.reject(envelope, reason=args.reason, actor=args.actor)
    except JournalError as exc:
        return _fail(str(exc))
    finally:
        journal.close()
    print(json.dumps(_decision_output(decision), indent=2))
    return EXIT_DECISION


def _inspect(args: argparse.Namespace) -> int:
    try:
        journal = _open(args)
    except JournalError as exc:
        return _fail(str(exc))
    try:
        state = journal.replay()
        hashes = journal.hash()
        ready = journal.ready_set()
    except JournalError as exc:
        return _fail(str(exc))
    finally:
        journal.close()
    if args.human:
        print(f"Graph: {state.get('graph_id') or '(uninitialised)'}")
        print(f"Revision: {state.get('graph_revision', 0)}")
        print(f"Schedule hash: {hashes['schedule_hash'][:16]}…")
        print(f"Audit hash: {hashes['audit_state_hash'][:16]}…")
        print(f"Execution nodes: {len(state.get('execution_nodes', []))}")
        print(f"Schedulable edges: {len(state.get('schedulable_edges', []))}")
        print(f"Ready set: {', '.join(ready) or '(empty)'}")
    else:
        print(json.dumps({"state": state, "hashes": hashes, "ready_set": ready}, indent=2))
    return EXIT_OK


def _replay(args: argparse.Namespace) -> int:
    try:
        journal = _open(args)
    except JournalError as exc:
        return _fail(str(exc))
    try:
        state = journal.replay(from_snapshot=args.from_snapshot)
    except JournalError as exc:
        return _fail(str(exc))
    finally:
        journal.close()
    print(json.dumps(state, indent=2))
    return EXIT_OK


def _hash(args: argparse.Namespace) -> int:
    try:
        journal = _open(args)
    except JournalError as exc:
        return _fail(str(exc))
    try:
        hashes = journal.hash()
    except JournalError as exc:
        return _fail(str(exc))
    finally:
        journal.close()
    print(json.dumps(hashes, indent=2))
    return EXIT_OK


def _snapshot(args: argparse.Namespace) -> int:
    try:
        journal = _open(args)
    except JournalError as exc:
        return _fail(str(exc))
    try:
        snapshot = journal.snapshot()
    except JournalError as exc:
        return _fail(str(exc))
    finally:
        journal.close()
    print(
        json.dumps(
            {
                "ok": True,
                "graph_id": snapshot.graph_id,
                "graph_revision": snapshot.graph_revision,
                "journal_seq": snapshot.journal_seq,
                "schedule_hash": snapshot.schedule_hash,
                "audit_state_hash": snapshot.audit_state_hash,
            },
            indent=2,
        )
    )
    return EXIT_OK


def _verify(args: argparse.Namespace) -> int:
    try:
        journal = _open(args)
    except JournalError as exc:
        return _fail(str(exc))
    try:
        if args.recover:
            outcome = journal.recover()
            print(json.dumps({"ok": True, **outcome}, indent=2))
            return EXIT_OK
        records = journal.verify()
    except JournalError as exc:
        return _fail(str(exc))
    finally:
        journal.close()
    print(json.dumps({"ok": True, "records_verified": records}, indent=2))
    return EXIT_OK


def _project(args: argparse.Namespace) -> int:
    """Emit the legacy six-field wave plan from the journal's intent graph.

    Delegates to compatibility.project_legacy_waves so the guards live in one
    place: an unscanned or malformed intent graph is an error here, never an
    empty plan that reads as "no work to do".
    """
    try:
        journal = _open(args)
    except JournalError as exc:
        return _fail(str(exc))
    try:
        state = journal.replay()
    except JournalError as exc:
        return _fail(str(exc))
    finally:
        journal.close()

    intent_graph = {
        "schema_version": state.get("schema_version", "1.0.0"),
        "record_type": "intent_graph",
        "graph_id": state.get("graph_id", ""),
        "source_ref": args.source_ref,
        "source_digest": "sha256:" + "0" * 64,
        "scan_status": "observed",
        "has_dependency_block": True,
        "source_warnings": [],
        "nodes": state.get("intent_nodes", []),
        "edges": state.get("intent_edges", []),
    }
    result = compatibility.project_legacy_waves(
        intent_graph,
        closed=args.closed or (),
        gated=args.gated or (),
    )
    if result.exit_code == EXIT_CYCLE:
        print(f"Error: cycle: {result.error}", file=sys.stderr)
        return EXIT_CYCLE
    if result.exit_code != EXIT_OK:
        print(json.dumps({"ok": False, "error": result.error}, indent=2))
        return EXIT_DECISION
    print(json.dumps(result.plan, indent=2))
    return EXIT_OK


def _int_list(value: str) -> list[int]:
    return [int(item) for item in value.split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transactional DAG engine CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str, func: Any) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--db", required=True, help="path to the SQLite journal database")
        command.set_defaults(func=func)
        return command

    init = add("init", "initialise a new graph", _init)
    init.add_argument("--graph-id", required=True)

    propose = add("propose", "submit a mutation envelope", _propose)
    propose.add_argument("envelope")
    propose.add_argument("--note", default="")

    admit = add("admit", "submit an envelope expected to be admitted", _admit)
    admit.add_argument("envelope")
    admit.add_argument("--note", default="")

    reject = add("reject", "record an operator refusal of an envelope", _reject)
    reject.add_argument("envelope")
    reject.add_argument("--reason", required=True)
    reject.add_argument("--actor", default="actor:operator")

    inspect_command = add("inspect", "show current graph state", _inspect)
    inspect_command.add_argument("--human", action="store_true")

    replay = add("replay", "reconstruct graph from journal", _replay)
    replay.add_argument(
        "--from-snapshot",
        action="store_true",
        help="fold forward from the latest snapshot instead of from genesis",
    )

    add("hash", "print canonical schedule and audit hashes", _hash)
    add("snapshot", "write a verifiable checkpoint of the current fold", _snapshot)

    verify = add("verify", "verify the hash chain", _verify)
    verify.add_argument(
        "--recover",
        action="store_true",
        help="truncate a torn tail back to the last valid record",
    )

    project = add("project", "emit the legacy six-field wave plan", _project)
    project.add_argument(
        "--waves",
        action="store_true",
        help="emit the wave plan (default, accepted for symmetry with plan_waves.py)",
    )
    project.add_argument("--source-ref", default="journal")
    project.add_argument("--closed", type=_int_list, default=None)
    project.add_argument("--gated", type=_int_list, default=None)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except JournalError as exc:
        return _fail(str(exc))
    except (OSError, ValueError) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
