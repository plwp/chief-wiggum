"""Transactional GraphJournal — sole sanctioned writer of graph_revision.

@cw-trace guards INV-dag-001 INV-dag-002 INV-dag-003 INV-dag-004
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes
from .errors import ContractViolation, ErrorCode
from .schemas import validate_record
from .semantics import validate_mutation

_GENESIS_HASH = "0" * 64


class JournalError(Exception):
    """Raised when the journal cannot be opened, is corrupt, or fails closed."""


@dataclass(frozen=True)
class Decision:
    accepted: bool
    journal_seq: int
    graph_revision: int | None
    reason: str
    violations: tuple[ContractViolation, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    graph_id: str
    graph_revision: int
    schedule_hash: str
    audit_state_hash: str
    snapshot_data: dict[str, Any] = field(default_factory=dict)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _schedule_projection(snapshot_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "execution_nodes": snapshot_data.get("execution_nodes", []),
        "schedulable_edges": snapshot_data.get("schedulable_edges", []),
        "control_records": snapshot_data.get("control_records", []),
        "lease_records": snapshot_data.get("lease_records", []),
    }


def _audit_projection(snapshot_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "intent_nodes": snapshot_data.get("intent_nodes", []),
        "intent_edges": snapshot_data.get("intent_edges", []),
        "execution_nodes": snapshot_data.get("execution_nodes", []),
        "schedulable_edges": snapshot_data.get("schedulable_edges", []),
        "relations": snapshot_data.get("relations", []),
        "evidence_records": snapshot_data.get("evidence_records", []),
        "approval_records": snapshot_data.get("approval_records", []),
        "lease_records": snapshot_data.get("lease_records", []),
        "control_records": snapshot_data.get("control_records", []),
        "mutations": snapshot_data.get("mutations", []),
    }


def _compute_schedule_hash(snapshot_data: dict[str, Any]) -> str:
    return _sha256(canonical_json_bytes(_schedule_projection(snapshot_data)))


def _compute_audit_state_hash(snapshot_data: dict[str, Any]) -> str:
    return _sha256(canonical_json_bytes(_audit_projection(snapshot_data)))


def _empty_snapshot(graph_id: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "record_type": "graph_snapshot",
        "graph_id": graph_id,
        "graph_revision": 0,
        "authority_matrix_version": "1.0.0",
        "intent_nodes": [],
        "intent_edges": [],
        "execution_nodes": [],
        "schedulable_edges": [],
        "relations": [],
        "evidence_records": [],
        "approval_records": [],
        "lease_records": [],
        "control_records": [],
        "mutations": [],
    }


def _apply_operation(snapshot_data: dict[str, Any], operation: dict[str, Any]) -> None:
    op_type = operation["operation_type"]
    collection_map = {
        "add_intent_node": "intent_nodes",
        "add_intent_edge": "intent_edges",
        "add_execution_node": "execution_nodes",
        "add_schedulable_edge": "schedulable_edges",
        "add_relation": "relations",
        "record_evidence": "evidence_records",
        "transition_node": "transition_node",
        "acquire_lease": "lease_records",
        "release_lease": "lease_records",
        "set_control": "control_records",
        "promote_candidate": "promote_candidate",
        "compensate": "compensate",
    }
    collection = collection_map.get(op_type)
    if collection in ("transition_node", "promote_candidate", "compensate"):
        return
    if collection is None:
        raise JournalError(f"unknown operation type {op_type!r}")
    if op_type.startswith("add_") or op_type == "record_evidence":
        record = dict(operation["value"])
        if "schema_version" not in record:
            record["schema_version"] = "1.0.0"
        if "record_type" not in record:
            type_map = {
                "add_intent_node": "intent_node",
                "add_intent_edge": "intent_edge",
                "add_execution_node": "execution_node",
                "add_schedulable_edge": "schedulable_edge",
                "add_relation": "relation",
                "record_evidence": "evidence_record",
            }
            record["record_type"] = type_map[op_type]
        snapshot_data[collection].append(record)


class GraphJournal:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._conn = sqlite3.connect(str(self._path), isolation_level=None)
        self._cached_state: dict[str, Any] | None = None
        self._cached_seq: int = -1
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_events (
                journal_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                graph_id TEXT NOT NULL,
                previous_record_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

    def _graph_id(self) -> str:
        row = self._conn.execute("SELECT graph_id FROM graph_events WHERE event_type='init' LIMIT 1").fetchone()
        return row[0] if row else ""

    def _last_event_hash(self) -> str:
        row = self._conn.execute("SELECT event_hash FROM graph_events ORDER BY journal_seq DESC LIMIT 1").fetchone()
        return row[0] if row else _GENESIS_HASH

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        self._conn.close()

    def _verify_chain(self) -> None:
        previous = _GENESIS_HASH
        for row in self._conn.execute(
            "SELECT journal_seq, previous_record_hash, event_hash, payload FROM graph_events ORDER BY journal_seq"
        ):
            seq, prev_hash, stored_hash, payload_text = row
            if prev_hash != previous:
                raise JournalError(f"hash chain broken at journal_seq={seq}: expected previous={previous!r}, got {prev_hash!r}")
            computed_hash = _sha256(payload_text.encode("utf-8"))
            if computed_hash != stored_hash:
                raise JournalError(f"event hash mismatch at journal_seq={seq}")
            previous = stored_hash

    def replay(self) -> dict[str, Any]:
        self._verify_chain()
        current_max = self._conn.execute("SELECT COALESCE(MAX(journal_seq), 0) FROM graph_events").fetchone()[0]
        if self._cached_state is not None and self._cached_seq == current_max:
            return dict(self._cached_state)
        state = None
        for row in self._conn.execute(
            "SELECT journal_seq, event_type, payload FROM graph_events WHERE event_type IN ('init', 'mutation_accepted', 'snapshot') ORDER BY journal_seq"
        ):
            _, event_type, payload_text = row
            payload = json.loads(payload_text)
            if event_type == "init":
                state = _empty_snapshot(payload["graph_id"])
            elif event_type == "snapshot":
                state = payload["snapshot"]
            elif event_type == "mutation_accepted":
                if state is None:
                    raise JournalError(f"accepted mutation at journal_seq without prior init")
                for operation in payload.get("operations", []):
                    _apply_operation(state, operation)
                state["graph_revision"] += 1
                state["mutations"].append(payload["envelope"])
        if isinstance(state, dict):
            self._cached_state = dict(state)
            self._cached_seq = current_max
        if state is None:
            return {"graph_id": "", "graph_revision": 0, "events": []}
        return state

    def current_revision(self) -> int:
        row = self._conn.execute("SELECT MAX(journal_seq) FROM graph_events").fetchone()
        if row[0] is None:
            return 0
        return row[0]

    def init_graph(self, graph_id: str) -> None:
        existing = self._conn.execute("SELECT COUNT(*) FROM graph_events WHERE event_type='init'").fetchone()[0]
        if existing:
            raise JournalError("journal already initialised with a different graph")
        self._commit_event(graph_id, "init", {"graph_id": graph_id})

    def propose(self, envelope: dict[str, Any], *, actor_note: str = "") -> Decision:
        errors = validate_record(envelope, "mutation_envelope")
        if errors:
            return self._reject(envelope, errors, "schema validation failed")
        graph_id = envelope["graph_id"]
        known_graph_id = self._graph_id()
        if not known_graph_id or graph_id != known_graph_id:
            return self._reject(envelope, (
                ContractViolation(ErrorCode.GRAPH_ID_MISMATCH, f"envelope graph_id {graph_id!r} does not match journal graph {known_graph_id!r}", "/graph_id"),
            ), "unknown graph")
        state = self.replay() if self.current_revision() > 0 else _empty_snapshot(graph_id)
        history = state.get("mutations", []) if isinstance(state, dict) else []
        envelope_digest = _sha256(canonical_json_bytes(envelope))
        for prior in history:
            if prior.get("idempotency_key") == envelope.get("idempotency_key"):
                if _sha256(canonical_json_bytes(prior)) == envelope_digest:
                    return Decision(accepted=True, journal_seq=self.current_revision(), graph_revision=state["graph_revision"], reason="idempotent no-op")
                return self._reject(envelope, (
                    ContractViolation(ErrorCode.IDEMPOTENCY_KEY_DIVERGENT, "idempotency key was previously used with a different canonical envelope", "/idempotency_key"),
                ), "idempotency key collision")
        validation_errors = validate_mutation(state, envelope, history=history)
        if validation_errors:
            return self._reject(envelope, validation_errors, "semantic validation failed")
        return self._accept(envelope)

    def _reject(self, envelope: dict[str, Any], violations: tuple[ContractViolation, ...], summary: str) -> Decision:
        graph_id = envelope.get("graph_id", "")
        payload = {
            "graph_id": graph_id,
            "mutation_id": envelope.get("mutation_id"),
            "idempotency_key": envelope.get("idempotency_key"),
            "base_revision": envelope.get("base_revision"),
            "reason": summary,
            "violations": [v.to_dict() for v in violations],
            "envelope_digest": _sha256(canonical_json_bytes(envelope)),
        }
        self._commit_event(graph_id, "mutation_rejected", payload)
        known_graph_id = self._graph_id()
        if not known_graph_id:
            revision = 0
        else:
            revision = self.replay().get("graph_revision", 0)
        return Decision(accepted=False, journal_seq=self.current_revision(), graph_revision=revision, reason=summary, violations=violations)

    def _accept(self, envelope: dict[str, Any]) -> Decision:
        graph_id = envelope["graph_id"]
        state = self.replay() if self.current_revision() > 0 else _empty_snapshot(graph_id)
        for operation in envelope.get("operations", []):
            _apply_operation(state, operation)
        state["graph_revision"] += 1
        state["mutations"] = state.get("mutations", []) + [envelope]
        new_schedule = _compute_schedule_hash(state)
        new_audit = _compute_audit_state_hash(state)
        payload = {
            "graph_id": graph_id,
            "envelope": envelope,
            "operations": envelope.get("operations", []),
            "schedule_hash": new_schedule,
            "audit_state_hash": new_audit,
        }
        self._commit_event(graph_id, "mutation_accepted", payload)
        seq = self.current_revision()
        return Decision(accepted=True, journal_seq=seq, graph_revision=state["graph_revision"], reason="accepted")

    def hash(self) -> dict[str, str]:
        state = self.replay()
        if not isinstance(state, dict) or not state.get("graph_id"):
            return {"schedule_hash": _GENESIS_HASH, "audit_state_hash": _GENESIS_HASH}
        return {
            "schedule_hash": _compute_schedule_hash(state),
            "audit_state_hash": _compute_audit_state_hash(state),
        }

    def _commit_event(self, graph_id: str, event_type: str, payload: dict[str, Any]) -> None:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            last_row = self._conn.execute(
                "SELECT event_hash FROM graph_events ORDER BY journal_seq DESC LIMIT 1"
            ).fetchone()
            previous_hash = last_row[0] if last_row else _GENESIS_HASH
            payload_bytes = canonical_json_bytes(payload)
            event_hash = _sha256(payload_bytes)
            self._conn.execute(
                "INSERT INTO graph_events (graph_id, previous_record_hash, event_hash, event_type, payload) VALUES (?,?,?,?,?)",
                (graph_id, previous_hash, event_hash, event_type, payload_bytes.decode()),
            )
            self._conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self._conn.execute("ROLLBACK")
            raise JournalError(f"durability failure: {exc}") from exc
        self._cached_seq = -1
