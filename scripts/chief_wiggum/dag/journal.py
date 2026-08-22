"""Transactional GraphJournal — sole sanctioned writer of graph_revision.

The journal is an append-only, hash-chained log of decision records. The graph
is a deterministic fold over that log; nothing else may mutate graph state.

Concurrency posture: every decision (read current state -> validate -> append)
runs inside a single ``BEGIN IMMEDIATE`` transaction. Compare-and-swap on
``base_revision`` is only meaningful if the check and the append are atomic, so
the write lock spans validation rather than just the insert.

@cw-trace guards INV-dag-001 INV-dag-002 INV-dag-003 INV-dag-004
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes
from .errors import ContractViolation, ErrorCode
from .schemas import SCHEMA_VERSION, validate_record
from .semantics import validate_mutation, validate_snapshot

_GENESIS_HASH = "0" * 64
_FOLD_EVENTS = ("init", "mutation_accepted", "snapshot")

# Operations that append a schema-bearing record to a snapshot collection.
_ADD_OPS: dict[str, tuple[str, str]] = {
    "add_intent_node": ("intent_nodes", "intent_node"),
    "add_intent_edge": ("intent_edges", "intent_edge"),
    "add_execution_node": ("execution_nodes", "execution_node"),
    "add_schedulable_edge": ("schedulable_edges", "schedulable_edge"),
    "add_relation": ("relations", "relation"),
    "record_evidence": ("evidence_records", "evidence_record"),
}

# Operations that drop a record from a collection, keyed by target_ref.
_REMOVE_OPS: dict[str, tuple[str, str]] = {
    "remove_intent_node": ("intent_nodes", "intent_node_id"),
    "remove_intent_edge": ("intent_edges", "edge_id"),
    "remove_schedulable_edge": ("schedulable_edges", "edge_id"),
}

# Operations that upsert a keyed record supplied in `value`.
_UPSERT_OPS: dict[str, tuple[str, str, str]] = {
    "acquire_lease": ("lease_records", "lease_id", "lease_record"),
    "set_control": ("control_records", "control_id", "control_record"),
    "approve_mutation": ("approval_records", "approval_id", "approval_record"),
}


class JournalError(Exception):
    """Raised when the journal cannot be opened, is corrupt, or fails closed."""


@dataclass(frozen=True)
class Decision:
    accepted: bool
    journal_seq: int
    graph_revision: int | None
    reason: str
    violations: tuple[ContractViolation, ...] = ()
    idempotent: bool = False


@dataclass(frozen=True)
class Snapshot:
    graph_id: str
    graph_revision: int
    journal_seq: int
    schedule_hash: str
    audit_state_hash: str
    snapshot_data: dict[str, Any] = field(default_factory=dict)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(record: Any) -> str:
    return _sha256(canonical_json_bytes(record))


def _schedule_projection(snapshot_data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "execution_nodes": snapshot_data.get("execution_nodes", []),
        "schedulable_edges": snapshot_data.get("schedulable_edges", []),
        "control_records": snapshot_data.get("control_records", []),
        "lease_records": snapshot_data.get("lease_records", []),
    }


def _audit_projection(snapshot_data: Mapping[str, Any]) -> dict[str, Any]:
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


def _compute_schedule_hash(snapshot_data: Mapping[str, Any]) -> str:
    return _sha256(canonical_json_bytes(_schedule_projection(snapshot_data)))


def _compute_audit_state_hash(snapshot_data: Mapping[str, Any]) -> str:
    return _sha256(canonical_json_bytes(_audit_projection(snapshot_data)))


def _empty_snapshot(graph_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
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


def _find(records: Sequence[dict[str, Any]], key: str, value: Any) -> dict[str, Any] | None:
    return next((record for record in records if record.get(key) == value), None)


def _derive_readiness(snapshot_data: dict[str, Any]) -> None:
    """Materialise the derived `ready` state after a fold step.

    docs/dag-contract.md: `ready` is a derived projection that a proposal may
    not set, and #385 is the ticket that composes the lifecycle and control
    machines — so the engine owns this transition and nothing else does.

    Schedulable edges run `source` AFTER `target`, so a node's predecessors are
    the targets of the edges it sources. Only the admitted<->ready pair is
    derived; `blocked`, `running` and the terminal states are proposal-owned.
    """
    nodes = {node["execution_node_id"]: node for node in snapshot_data["execution_nodes"]}
    predecessors: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for edge in snapshot_data["schedulable_edges"]:
        source, target = str(edge.get("source")), str(edge.get("target"))
        if source in predecessors:
            predecessors[source].append(target)
    for node_id, node in nodes.items():
        satisfied = all(
            (nodes.get(target) or {}).get("lifecycle_state") == "succeeded"
            for target in predecessors[node_id]
        )
        if node.get("lifecycle_state") == "admitted" and satisfied:
            node["lifecycle_state"] = "ready"
        elif node.get("lifecycle_state") == "ready" and not satisfied:
            node["lifecycle_state"] = "admitted"


def _record_from(operation: Mapping[str, Any], record_type: str) -> dict[str, Any]:
    record = dict(operation["value"])
    record.setdefault("schema_version", SCHEMA_VERSION)
    record.setdefault("record_type", record_type)
    return record


def _apply_operation(snapshot_data: dict[str, Any], operation: Mapping[str, Any]) -> None:
    """Apply one operation to the folded graph state, in place.

    Every operation type in the v1 vocabulary is handled. An operation that
    cannot be applied raises JournalError rather than silently doing nothing —
    a no-op fold makes graph_revision advance while the projection stands
    still, which is the failure mode this engine exists to prevent.
    """
    op_type = operation["operation_type"]
    target = operation.get("target_ref")

    if op_type in _ADD_OPS:
        collection, record_type = _ADD_OPS[op_type]
        snapshot_data[collection].append(_record_from(operation, record_type))
        return

    if op_type in _REMOVE_OPS:
        collection, key = _REMOVE_OPS[op_type]
        remaining = [record for record in snapshot_data[collection] if record.get(key) != target]
        if len(remaining) == len(snapshot_data[collection]):
            raise JournalError(f"{op_type}: no {key} matching {target!r}")
        snapshot_data[collection] = remaining
        return

    if op_type in _UPSERT_OPS:
        collection, key, record_type = _UPSERT_OPS[op_type]
        record = _record_from(operation, record_type)
        existing = _find(snapshot_data[collection], key, record.get(key))
        if existing is not None:
            existing.clear()
            existing.update(record)
        else:
            snapshot_data[collection].append(record)
        if op_type == "acquire_lease":
            node = _find(
                snapshot_data["execution_nodes"], "execution_node_id", record.get("execution_node_id")
            )
            if node is None:
                raise JournalError(
                    f"acquire_lease: execution node {record.get('execution_node_id')!r} does not exist"
                )
            node["lease_state"] = record["state"]
        return

    if op_type == "transition_node":
        node = _find(snapshot_data["execution_nodes"], "execution_node_id", target)
        if node is None:
            raise JournalError(f"transition_node: execution node {target!r} does not exist")
        node["lifecycle_state"] = operation["to_state"]
        return

    if op_type == "release_lease":
        lease = _find(snapshot_data["lease_records"], "lease_id", target)
        if lease is None:
            lease = _find(snapshot_data["lease_records"], "execution_node_id", target)
        if lease is None:
            raise JournalError(f"release_lease: no lease matching {target!r}")
        lease["state"] = "released"
        node = _find(
            snapshot_data["execution_nodes"], "execution_node_id", lease.get("execution_node_id")
        )
        if node is not None:
            node["lease_state"] = "released"
        return

    if op_type == "promote_candidate":
        value = operation["value"]
        node = _find(snapshot_data["execution_nodes"], "execution_node_id", value["execution_node_id"])
        if node is None:
            raise JournalError(
                f"promote_candidate: execution node {value['execution_node_id']!r} does not exist"
            )
        node["candidate"] = {"group_id": value["candidate_group_id"], "disposition": "promoted"}
        return

    if op_type == "compensate":
        # A compensating event does not rewrite history; its durable effect is
        # the envelope itself, which the fold appends to `mutations`. Candidate
        # routing and compensation *policy* are explicit non-goals of #385
        # (they belong to #386/#387), so there is no further structural change.
        return

    raise JournalError(f"unknown operation type {op_type!r}")


class GraphJournal:
    """Append-only hash-chained journal with a deterministic graph fold."""

    def __init__(self, path: Path | str, *, timeout: float = 30.0) -> None:
        self._path = Path(path)
        try:
            self._conn = sqlite3.connect(str(self._path), isolation_level=None, timeout=timeout)
            self._conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            self._conn.execute("PRAGMA synchronous=FULL")
            # Switching journal mode needs a brief exclusive lock and SQLite does
            # NOT run the busy handler for it, so two processes opening the same
            # new journal at once would otherwise leave one with "database is
            # locked". Retry both setup steps instead of failing the open.
            self._retry_while_locked(timeout, self._enable_wal)
            self._retry_while_locked(timeout, self._create_schema)
        except sqlite3.Error as exc:
            raise JournalError(f"cannot open journal at {self._path}: {exc}") from exc
        # Verification is incremental within a process but always starts from
        # genesis on a fresh connection, so tamper-evidence survives restarts.
        self._verified_seq = 0
        self._verified_hash = _GENESIS_HASH
        self._chain_error: str | None = None
        self._cached_state: dict[str, Any] | None = None
        self._cached_seq = -1
        self._idempotency_index: dict[str, dict[str, Any]] = {}
        self._state_validated = False

    def _enable_wal(self) -> None:
        mode = self._conn.execute("PRAGMA journal_mode").fetchone()
        if mode and str(mode[0]).lower() == "wal":
            return
        self._conn.execute("PRAGMA journal_mode=WAL")

    def _create_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS graph_events (
                journal_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                graph_id TEXT NOT NULL,
                previous_record_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

    @staticmethod
    def _retry_while_locked(timeout: float, action: Any) -> None:
        deadline = time.monotonic() + timeout
        delay = 0.005
        while True:
            try:
                action()
                return
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.25)

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- chain

    def _safe_rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            # No active transaction (e.g. BEGIN itself failed). Swallowing this
            # is deliberate: re-raising here would mask the original failure.
            pass

    @contextmanager
    def _write_transaction(self) -> Generator[None]:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise JournalError(f"cannot acquire the journal write lock: {exc}") from exc
        try:
            yield
        except BaseException:
            self._safe_rollback()
            raise
        try:
            self._conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self._safe_rollback()
            raise JournalError(f"durability failure: {exc}") from exc

    def _rows(self, after_seq: int = 0) -> list[tuple[int, str, str, str]]:
        return list(
            self._conn.execute(
                "SELECT journal_seq, previous_record_hash, event_hash, payload"
                " FROM graph_events WHERE journal_seq > ? ORDER BY journal_seq",
                (after_seq,),
            )
        )

    def _ensure_verified(self) -> None:
        """Verify the hash chain, re-checking only records not yet verified."""
        if self._chain_error is not None:
            raise JournalError(self._chain_error)
        previous = self._verified_hash
        for seq, prev_hash, stored_hash, payload_text in self._rows(self._verified_seq):
            if prev_hash != previous:
                self._chain_error = (
                    f"hash chain broken at journal_seq={seq}:"
                    f" expected previous={previous!r}, got {prev_hash!r}"
                )
                raise JournalError(self._chain_error)
            if _sha256(payload_text.encode("utf-8")) != stored_hash:
                self._chain_error = f"event hash mismatch at journal_seq={seq}"
                raise JournalError(self._chain_error)
            previous = stored_hash
            self._verified_seq, self._verified_hash = seq, stored_hash

    def verify(self) -> int:
        """Verify the whole chain from genesis. Returns the verified record count."""
        self._verified_seq, self._verified_hash, self._chain_error = 0, _GENESIS_HASH, None
        self._ensure_verified()
        return self._verified_seq

    def recover(self) -> dict[str, Any]:
        """Truncate a torn tail, restoring the longest valid prefix.

        A torn write can only ever damage the tail. Corruption with intact
        records after it is not a crash artefact, and truncating would discard
        admitted mutations — so that fails closed instead.
        """
        rows = self._rows()
        previous, last_valid, first_bad = _GENESIS_HASH, 0, None
        for seq, prev_hash, stored_hash, payload_text in rows:
            if prev_hash != previous or _sha256(payload_text.encode("utf-8")) != stored_hash:
                first_bad = seq
                break
            previous, last_valid = stored_hash, seq
        if first_bad is None:
            return {"dropped": 0, "last_valid_seq": last_valid}
        tail = [row for row in rows if row[0] > first_bad]
        if any(_sha256(payload.encode("utf-8")) == stored for _, _, stored, payload in tail):
            raise JournalError(
                f"mid-journal corruption at journal_seq={first_bad} with valid records after it;"
                " refusing to truncate because admitted mutations would be lost"
            )
        with self._write_transaction():
            self._conn.execute("DELETE FROM graph_events WHERE journal_seq >= ?", (first_bad,))
        dropped = sum(1 for row in rows if row[0] >= first_bad)
        self._verified_seq, self._verified_hash, self._chain_error = 0, _GENESIS_HASH, None
        self._invalidate()
        return {"dropped": dropped, "last_valid_seq": last_valid}

    def _invalidate(self) -> None:
        self._cached_state = None
        self._cached_seq = -1
        self._idempotency_index = {}
        self._state_validated = False

    # ----------------------------------------------------------------- fold

    def last_journal_seq(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(journal_seq), 0) FROM graph_events").fetchone()
        return int(row[0])

    def _fold(self, *, from_snapshot: bool = False) -> dict[str, Any]:
        """Return the internal folded state. Callers must not mutate it."""
        self._ensure_verified()
        current_max = self.last_journal_seq()
        if not from_snapshot and self._cached_state is not None and self._cached_seq == current_max:
            return self._cached_state

        start_seq = 0
        if from_snapshot:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(journal_seq), 0) FROM graph_events WHERE event_type='snapshot'"
            ).fetchone()
            start_seq = int(row[0]) - 1 if row[0] else 0

        state: dict[str, Any] | None = None
        index: dict[str, dict[str, Any]] = {}
        placeholders = ",".join("?" for _ in _FOLD_EVENTS)
        rows = self._conn.execute(
            "SELECT journal_seq, event_type, payload FROM graph_events"
            f" WHERE event_type IN ({placeholders}) AND journal_seq > ? ORDER BY journal_seq",
            (*_FOLD_EVENTS, start_seq),
        )
        for seq, event_type, payload_text in rows:
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError as exc:
                raise JournalError(f"unparseable payload at journal_seq={seq}: {exc}") from exc
            if event_type == "init":
                state = _empty_snapshot(payload["graph_id"])
            elif event_type == "snapshot":
                embedded = payload["snapshot"]
                if state is None:
                    state = copy.deepcopy(embedded)
                elif _digest(embedded) != _digest(state):
                    # A snapshot never wins a disagreement with its own prefix.
                    raise JournalError(
                        f"snapshot at journal_seq={seq} disagrees with a replay of its journal prefix"
                    )
            elif event_type == "mutation_accepted":
                if state is None:
                    raise JournalError(f"accepted mutation at journal_seq={seq} without a prior init")
                envelope = payload["envelope"]
                for operation in envelope.get("operations", []):
                    _apply_operation(state, operation)
                _derive_readiness(state)
                state["graph_revision"] += 1
                state["mutations"].append(envelope)
                index[envelope["idempotency_key"]] = {
                    "digest": _digest(envelope),
                    "journal_seq": seq,
                    "graph_revision": state["graph_revision"],
                }

        if state is None:
            state = _empty_snapshot("")
        if not from_snapshot:
            self._cached_state = state
            self._cached_seq = current_max
            self._idempotency_index = index
        return state

    def replay(self, *, from_snapshot: bool = False) -> dict[str, Any]:
        """Reconstruct graph state from the journal.

        Returns a deep copy: the fold's own state is cached, and handing out an
        aliased view would let any caller silently corrupt it.
        """
        return copy.deepcopy(self._fold(from_snapshot=from_snapshot))

    def graph_revision(self) -> int:
        return int(self._fold()["graph_revision"])

    def hash(self, *, from_snapshot: bool = False) -> dict[str, str]:
        state = self._fold(from_snapshot=from_snapshot)
        if not state.get("graph_id"):
            return {"schedule_hash": _GENESIS_HASH, "audit_state_hash": _GENESIS_HASH}
        return {
            "schedule_hash": _compute_schedule_hash(state),
            "audit_state_hash": _compute_audit_state_hash(state),
        }

    def ready_set(self) -> list[str]:
        """Execution nodes the scheduler may claim right now.

        Readiness itself is materialised by the fold (_derive_readiness); this
        applies the control machine on top of it.
        """
        state = self._fold()
        if any(
            record.get("state") in ("paused", "pause_requested", "cancelled", "cancel_requested")
            for record in state["control_records"]
        ):
            return []
        return sorted(
            node["execution_node_id"]
            for node in state["execution_nodes"]
            if node.get("lifecycle_state") == "ready" and node.get("control_state") == "active"
        )

    # -------------------------------------------------------------- writing

    def _append_event(self, graph_id: str, event_type: str, payload: Mapping[str, Any]) -> int:
        """Append one record. Must be called inside a write transaction.

        Does NOT drop the cached fold: refolding from genesis after every write
        makes a run of N proposals cost O(N^2). Callers advance the cache
        instead — they already hold the post-state they just computed.
        """
        last_row = self._conn.execute(
            "SELECT event_hash FROM graph_events ORDER BY journal_seq DESC LIMIT 1"
        ).fetchone()
        previous_hash = last_row[0] if last_row else _GENESIS_HASH
        payload_bytes = canonical_json_bytes(payload)
        event_hash = _sha256(payload_bytes)
        cursor = self._conn.execute(
            "INSERT INTO graph_events (graph_id, previous_record_hash, event_hash, event_type, payload)"
            " VALUES (?,?,?,?,?)",
            (graph_id, previous_hash, event_hash, event_type, payload_bytes.decode()),
        )
        if cursor.lastrowid is None:  # pragma: no cover - sqlite always sets it on INSERT
            raise JournalError("insert did not yield a journal_seq")
        # Deliberately does NOT mark the new record pre-verified: _ensure_verified
        # already checks each record exactly once per connection, and trusting a
        # record because we wrote it would hide tampering that lands afterwards.
        return int(cursor.lastrowid)

    def _advance_cache(self, seq: int, state: dict[str, Any] | None = None) -> None:
        """Carry the cached fold forward past a record that just landed."""
        if self._cached_state is None and state is None:
            return
        if state is not None:
            self._cached_state = state
        self._cached_seq = seq

    def init_graph(self, graph_id: str) -> None:
        with self._write_transaction():
            existing = self._conn.execute(
                "SELECT COUNT(*) FROM graph_events WHERE event_type='init'"
            ).fetchone()[0]
            if existing:
                raise JournalError("journal already initialised with a different graph")
            self._append_event(graph_id, "init", {"graph_id": graph_id})
            self._invalidate()

    def snapshot(self) -> Snapshot:
        """Write a verifiable checkpoint of the current fold."""
        with self._write_transaction():
            state = self._fold()
            if not state.get("graph_id"):
                raise JournalError("cannot snapshot an uninitialised journal")
            data = copy.deepcopy(state)
            payload = {
                "graph_id": data["graph_id"],
                "snapshot": data,
                "journal_seq": self.last_journal_seq(),
                "schedule_hash": _compute_schedule_hash(data),
                "audit_state_hash": _compute_audit_state_hash(data),
            }
            seq = self._append_event(data["graph_id"], "snapshot", payload)
            # A checkpoint restates the fold; it does not change it.
            self._advance_cache(seq)
            return Snapshot(
                graph_id=data["graph_id"],
                graph_revision=data["graph_revision"],
                journal_seq=seq,
                schedule_hash=payload["schedule_hash"],
                audit_state_hash=payload["audit_state_hash"],
                snapshot_data=data,
            )

    def propose(self, envelope: Mapping[str, Any], *, actor_note: str = "") -> Decision:
        """Decide one mutation envelope atomically.

        The whole compare-and-swap — read the current revision, validate the
        envelope against it, append the decision — happens under one write
        lock. Splitting it would let two proposers pass the same base_revision
        check and both commit.
        """
        with self._write_transaction():
            return self._decide(envelope, actor_note=actor_note)

    def reject(
        self, envelope: Mapping[str, Any], *, reason: str, actor: str = "actor:operator"
    ) -> Decision:
        """Record an operator's refusal of an envelope, without applying it.

        Carries no ContractViolation: this is a human decision, not a contract
        failure, and inventing an error code for it would pollute the #384
        vocabulary.
        """
        with self._write_transaction():
            return self._journal_rejection(envelope, (), f"operator rejection by {actor}: {reason}")

    def _decide(self, envelope: Mapping[str, Any], *, actor_note: str = "") -> Decision:
        schema_errors = validate_record(envelope, "mutation_envelope")
        if schema_errors:
            return self._journal_rejection(envelope, schema_errors, "schema validation failed")

        state = self._fold()
        known_graph_id = state.get("graph_id", "")
        if not known_graph_id:
            return self._journal_rejection(
                envelope,
                (
                    ContractViolation(
                        ErrorCode.GRAPH_ID_MISMATCH, "journal has no initialised graph", "/graph_id"
                    ),
                ),
                "unknown graph",
            )
        if envelope["graph_id"] != known_graph_id:
            return self._journal_rejection(
                envelope,
                (
                    ContractViolation(
                        ErrorCode.GRAPH_ID_MISMATCH,
                        f"envelope graph_id {envelope['graph_id']!r} does not match"
                        f" journal graph {known_graph_id!r}",
                        "/graph_id",
                    ),
                ),
                "unknown graph",
            )

        prior = self._idempotency_index.get(envelope["idempotency_key"])
        if prior is not None:
            if prior["digest"] == _digest(envelope):
                # Replay of a seen key: return the ORIGINAL decision unchanged.
                return Decision(
                    accepted=True,
                    journal_seq=prior["journal_seq"],
                    graph_revision=prior["graph_revision"],
                    reason="idempotent no-op",
                    idempotent=True,
                )
            return self._journal_rejection(
                envelope,
                (
                    ContractViolation(
                        ErrorCode.IDEMPOTENCY_KEY_DIVERGENT,
                        "idempotency key was previously used with a different canonical envelope",
                        "/idempotency_key",
                    ),
                ),
                "idempotency key collision",
            )

        violations = validate_mutation(
            state,
            envelope,
            history=state["mutations"],
            snapshot_validated=self._state_validated,
        )
        if violations:
            return self._journal_rejection(envelope, violations, "semantic validation failed")
        self._state_validated = True

        # Apply to a copy so an envelope is all-or-nothing, then validate the
        # RESULTING graph. Cycle rejection has to look at the post-state: a
        # mutation that introduces a cycle leaves the pre-state acyclic.
        candidate = copy.deepcopy(state)
        try:
            for operation in envelope["operations"]:
                _apply_operation(candidate, operation)
        except JournalError as exc:
            return self._journal_rejection(
                envelope,
                (ContractViolation(ErrorCode.SCHEMA_INVALID, str(exc), "/operations"),),
                "operation could not be applied",
            )
        _derive_readiness(candidate)
        candidate["graph_revision"] += 1
        # Deep copy: this candidate may be installed as the cached fold, and a
        # caller that mutates its own envelope afterwards must not be able to
        # change what the journal believes it recorded.
        candidate["mutations"].append(copy.deepcopy(dict(envelope)))

        # Every record entering here was schema-validated as part of the
        # envelope, so only the referential/identity/acyclicity checks are new.
        post_errors = validate_snapshot(candidate, schema=False)
        if post_errors:
            return self._journal_rejection(
                envelope, post_errors, "mutation would produce an invalid graph"
            )

        payload = {
            "graph_id": known_graph_id,
            "envelope": dict(envelope),
            "schedule_hash": _compute_schedule_hash(candidate),
            "audit_state_hash": _compute_audit_state_hash(candidate),
            "actor_note": actor_note,
        }
        seq = self._append_event(known_graph_id, "mutation_accepted", payload)
        # The candidate IS the new fold; installing it keeps propose O(1) in the
        # length of the journal instead of refolding from genesis.
        self._advance_cache(seq, candidate)
        self._idempotency_index[envelope["idempotency_key"]] = {
            "digest": _digest(envelope),
            "journal_seq": seq,
            "graph_revision": candidate["graph_revision"],
        }
        return Decision(
            accepted=True,
            journal_seq=seq,
            graph_revision=candidate["graph_revision"],
            reason="accepted",
        )

    def _journal_rejection(
        self,
        envelope: Mapping[str, Any],
        violations: Sequence[ContractViolation],
        summary: str,
    ) -> Decision:
        """Journal a rejection. Must be called inside a write transaction."""
        # Attribute the record to THIS journal's graph, never to an unvalidated
        # graph_id taken from the envelope.
        row = self._conn.execute(
            "SELECT payload FROM graph_events WHERE event_type='init' ORDER BY journal_seq LIMIT 1"
        ).fetchone()
        graph_id = json.loads(row[0])["graph_id"] if row else ""
        revision = self._fold()["graph_revision"] if row else 0
        payload = {
            "graph_id": graph_id,
            "mutation_id": envelope.get("mutation_id"),
            "idempotency_key": envelope.get("idempotency_key"),
            "base_revision": envelope.get("base_revision"),
            "graph_revision": revision,
            "reason": summary,
            "violations": [violation.to_dict() for violation in violations],
            "envelope_digest": _digest(envelope),
        }
        seq = self._append_event(graph_id, "mutation_rejected", payload)
        # A rejection changes no graph state, so the cached fold still stands.
        self._advance_cache(seq)
        return Decision(
            accepted=False,
            journal_seq=seq,
            graph_revision=revision,
            reason=summary,
            violations=tuple(violations),
        )
