"""Continuous ready-set scheduler.

The behavioural change in one line: a blocked node blocks its dependants, not
the wave. There is no barrier, because the dependency graph already carries
everything a barrier was protecting.

Two independent readiness computations live here on purpose. `full_ready_set`
derives readiness from first principles, and `ReadyIndex` maintains it
incrementally along the affected frontier. They must agree with each other and
with the journal's own derived `ready` state, which is what makes "incremental"
safe to rely on rather than merely fast.

Edge direction follows docs/dag-contract.md: `source` runs AFTER `target`, so a
node's predecessors are the targets of the edges it sources.

Lease timing lives in this module's store rather than in the graph: the v1
lease_record schema is closed and carries no expiry, so putting deadlines in the
graph would mean amending #384's contract to add a scheduler detail.

@cw-trace guards INV-dag-008 INV-dag-009
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .admission import LivenessFailure

TERMINAL = frozenset({"succeeded", "failed", "superseded", "cancelled"})
BLOCKING_CONTROL = frozenset({"paused", "pause_requested", "cancelled", "cancel_requested"})
RUNNABLE = frozenset({"admitted", "ready"})


class ClaimRefusal(StrEnum):
    STALE_REVISION = "STALE_REVISION"
    NOT_READY = "NOT_READY"
    ALREADY_LEASED = "ALREADY_LEASED"
    GRAPH_PAUSED = "GRAPH_PAUSED"
    UNKNOWN_NODE = "UNKNOWN_NODE"


@dataclass(frozen=True)
class SchedulerPolicy:
    lease_seconds: float = 3600.0
    heartbeat_seconds: float = 300.0
    max_retries_per_node: int = 2
    max_retries_per_graph: int = 10
    max_concurrent: int = 4
    risk_rank: Mapping[str, int] = field(default_factory=dict)
    declared_priority: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Claim:
    granted: bool
    execution_node_id: str = ""
    lease_id: str = ""
    fencing_token: str = ""
    graph_revision: int = 0
    refusal: ClaimRefusal | None = None
    detail: str = ""


@dataclass(frozen=True)
class LivenessReport:
    running: tuple[str, ...]
    ready: tuple[str, ...]
    blocked: tuple[str, ...]
    outstanding: tuple[str, ...]

    @property
    def stalled(self) -> bool:
        """Nothing running, nothing ready, yet work remains."""
        return not self.running and not self.ready and bool(self.outstanding)


def _nodes_by_id(state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {node["execution_node_id"]: node for node in state.get("execution_nodes", [])}


def _predecessors(state: Mapping[str, Any]) -> dict[str, list[str]]:
    """target runs BEFORE source, so the targets of a node's edges gate it."""
    predecessors: dict[str, list[str]] = {node: [] for node in _nodes_by_id(state)}
    for edge in state.get("schedulable_edges", []):
        source, target = str(edge.get("source")), str(edge.get("target"))
        if source in predecessors:
            predecessors[source].append(target)
    return predecessors


def _dependants(state: Mapping[str, Any]) -> dict[str, list[str]]:
    dependants: dict[str, list[str]] = {node: [] for node in _nodes_by_id(state)}
    for edge in state.get("schedulable_edges", []):
        source, target = str(edge.get("source")), str(edge.get("target"))
        if target in dependants:
            dependants[target].append(source)
    return dependants


def graph_paused(state: Mapping[str, Any]) -> bool:
    return any(
        record.get("state") in BLOCKING_CONTROL for record in state.get("control_records", [])
    )


def _node_is_ready(
    node: Mapping[str, Any], predecessors: Sequence[str], nodes: Mapping[str, Mapping[str, Any]]
) -> bool:
    if node.get("lifecycle_state") not in RUNNABLE:
        return False
    if node.get("control_state") != "active":
        return False
    return all(
        (nodes.get(predecessor) or {}).get("lifecycle_state") == "succeeded"
        for predecessor in predecessors
    )


def full_ready_set(state: Mapping[str, Any]) -> list[str]:
    """Readiness from first principles. The reference the incremental index must match."""
    if graph_paused(state):
        return []
    nodes = _nodes_by_id(state)
    predecessors = _predecessors(state)
    return sorted(
        node_id
        for node_id, node in nodes.items()
        if _node_is_ready(node, predecessors.get(node_id, ()), nodes)
    )


class ReadyIndex:
    """Incrementally maintained ready set.

    Recomputing the whole topological order on every event is wasteful, so a
    change invalidates only the node itself and its direct dependants: readiness
    depends on nothing else. A control change that pauses the graph invalidates
    everything, because it gates every node at once.
    """

    def __init__(self, state: Mapping[str, Any]) -> None:
        self._ready: set[str] = set(full_ready_set(state))
        self._paused = graph_paused(state)

    def apply(self, state: Mapping[str, Any], dirty: Iterable[str]) -> None:
        paused = graph_paused(state)
        if paused:
            self._ready.clear()
            self._paused = True
            return
        nodes = _nodes_by_id(state)
        predecessors = _predecessors(state)
        if self._paused:
            # Unpausing re-opens every node, so the frontier is the whole graph.
            self._paused = False
            frontier = set(nodes)
        else:
            dependants = _dependants(state)
            frontier = set()
            for node_id in dirty:
                frontier.add(node_id)
                frontier.update(dependants.get(node_id, ()))
        for node_id in frontier:
            node = nodes.get(node_id)
            if node is not None and _node_is_ready(node, predecessors.get(node_id, ()), nodes):
                self._ready.add(node_id)
            else:
                self._ready.discard(node_id)
        self._ready.intersection_update(nodes)

    def ready(self) -> list[str]:
        return sorted(self._ready)


def critical_path_lengths(state: Mapping[str, Any]) -> dict[str, int]:
    """Longest chain of remaining work downstream of each node."""
    nodes = _nodes_by_id(state)
    dependants = _dependants(state)
    lengths: dict[str, int] = {}

    def visit(node_id: str, seen: frozenset[str]) -> int:
        if node_id in lengths:
            return lengths[node_id]
        if node_id in seen:  # pragma: no cover - the engine rejects cycles
            return 0
        best = 0
        for dependant in dependants.get(node_id, ()):
            best = max(best, 1 + visit(dependant, seen | {node_id}))
        lengths[node_id] = best
        return best

    for node_id in nodes:
        visit(node_id, frozenset())
    return lengths


def priority_key(
    node_id: str,
    state: Mapping[str, Any],
    policy: SchedulerPolicy,
    *,
    critical_path: Mapping[str, int] | None = None,
    resource_available: Mapping[str, bool] | None = None,
) -> tuple:
    """Declared priority, critical path, risk, age, resources, then the node id.

    The final id tie-break is what makes two replays of one journal schedule
    identically. Without it, deterministic replay is unprovable at this layer.
    """
    nodes = list(_nodes_by_id(state))
    node = _nodes_by_id(state).get(node_id, {})
    lengths = critical_path if critical_path is not None else critical_path_lengths(state)
    age = nodes.index(node_id) if node_id in nodes else len(nodes)
    available = (resource_available or {}).get(node_id, True)
    return (
        -int(policy.declared_priority.get(node_id, 0)),
        -int(lengths.get(node_id, 0)),
        -int(policy.risk_rank.get(str(node.get("node_type", "")), 0)),
        age,
        0 if available else 1,
        node_id,
    )


class SchedulerStore:
    """Lease timing, heartbeats and the dispatch log.

    Separate from the graph because the v1 lease record is closed and carries no
    deadline. The graph owns lease STATE; this owns lease TIME.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._conn = sqlite3.connect(str(self._path), isolation_level=None, timeout=30.0)
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leases (
                lease_id TEXT PRIMARY KEY,
                graph_id TEXT NOT NULL,
                execution_node_id TEXT NOT NULL,
                worker TEXT NOT NULL,
                fencing_token TEXT NOT NULL,
                granted_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                progress TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL DEFAULT 'active'
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispatches (
                dispatch_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                graph_id TEXT NOT NULL,
                execution_node_id TEXT NOT NULL,
                lease_id TEXT NOT NULL,
                graph_revision INTEGER NOT NULL,
                dispatched_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS retries (
                retry_id TEXT PRIMARY KEY,
                graph_id TEXT NOT NULL,
                original_node_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )

    def close(self) -> None:
        self._conn.close()

    def grant(
        self,
        *,
        lease_id: str,
        graph_id: str,
        node_id: str,
        worker: str,
        fencing_token: str,
        now: float,
        expires_at: float,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO leases (lease_id, graph_id, execution_node_id, worker,"
            " fencing_token, granted_at, expires_at, heartbeat_at, progress, state)"
            " VALUES (?,?,?,?,?,?,?,?,'{}','active')",
            (lease_id, graph_id, node_id, worker, fencing_token, now, expires_at, now),
        )

    def active_lease_for(self, graph_id: str, node_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT lease_id, worker, expires_at, state FROM leases"
            " WHERE graph_id=? AND execution_node_id=? AND state='active'",
            (graph_id, node_id),
        ).fetchone()
        if row is None:
            return None
        return {"lease_id": row[0], "worker": row[1], "expires_at": row[2], "state": row[3]}

    def lease(self, lease_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT lease_id, graph_id, execution_node_id, worker, fencing_token, expires_at,"
            " heartbeat_at, progress, state FROM leases WHERE lease_id=?",
            (lease_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "lease_id": row[0],
            "graph_id": row[1],
            "execution_node_id": row[2],
            "worker": row[3],
            "fencing_token": row[4],
            "expires_at": row[5],
            "heartbeat_at": row[6],
            "progress": json.loads(row[7]),
            "state": row[8],
        }

    def heartbeat(self, lease_id: str, *, now: float, progress: Mapping[str, Any],
                  expires_at: float) -> bool:
        cursor = self._conn.execute(
            "UPDATE leases SET heartbeat_at=?, progress=?, expires_at=?"
            " WHERE lease_id=? AND state='active'",
            (now, json.dumps(dict(progress), sort_keys=True), expires_at, lease_id),
        )
        return cursor.rowcount > 0

    def expire(self, lease_id: str) -> None:
        self._conn.execute(
            "UPDATE leases SET state='expired' WHERE lease_id=? AND state='active'", (lease_id,)
        )

    def release(self, lease_id: str) -> None:
        self._conn.execute(
            "UPDATE leases SET state='released' WHERE lease_id=? AND state='active'", (lease_id,)
        )

    def expired(self, graph_id: str, now: float) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT lease_id, execution_node_id, worker, expires_at FROM leases"
            " WHERE graph_id=? AND state='active' AND expires_at <= ? ORDER BY lease_id",
            (graph_id, now),
        )
        return [
            {"lease_id": r[0], "execution_node_id": r[1], "worker": r[2], "expires_at": r[3]}
            for r in rows
        ]

    def active(self, graph_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT lease_id, execution_node_id, worker FROM leases"
            " WHERE graph_id=? AND state='active' ORDER BY lease_id",
            (graph_id,),
        )
        return [{"lease_id": r[0], "execution_node_id": r[1], "worker": r[2]} for r in rows]

    def record_dispatch(self, *, graph_id: str, node_id: str, lease_id: str,
                        revision: int, now: float) -> None:
        self._conn.execute(
            "INSERT INTO dispatches (graph_id, execution_node_id, lease_id, graph_revision,"
            " dispatched_at) VALUES (?,?,?,?,?)",
            (graph_id, node_id, lease_id, revision, now),
        )

    def dispatch_order(self, graph_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT execution_node_id FROM dispatches WHERE graph_id=? ORDER BY dispatch_seq",
            (graph_id,),
        )
        return [row[0] for row in rows]

    def retry_count(self, graph_id: str, node_id: str | None = None) -> int:
        if node_id is None:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM retries WHERE graph_id=?", (graph_id,)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM retries WHERE graph_id=? AND original_node_id=?",
                (graph_id, node_id),
            ).fetchone()
        return int(row[0])

    def record_retry(self, *, retry_id: str, graph_id: str, node_id: str, attempt: int,
                     now: float) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO retries (retry_id, graph_id, original_node_id, attempt,"
            " created_at) VALUES (?,?,?,?,?)",
            (retry_id, graph_id, node_id, attempt, now),
        )


class Scheduler:
    """Decides what is safe to start now, and refuses to pretend when nothing is."""

    def __init__(
        self,
        journal: Any,
        store: SchedulerStore,
        policy: SchedulerPolicy | None = None,
        *,
        clock: Callable[[], float] = time.time,
        shadow: bool = False,
    ) -> None:
        self._journal = journal
        self._store = store
        self._policy = policy or SchedulerPolicy()
        self._clock = clock
        self._shadow = shadow
        state = journal.replay()
        self._graph_id = str(state.get("graph_id", ""))
        self._index = ReadyIndex(state)

    @property
    def shadow(self) -> bool:
        return self._shadow

    def refresh(self, dirty: Iterable[str] | None = None) -> list[str]:
        """Advance the incremental index. Passing None forces a full rebuild."""
        state = self._journal.replay()
        if dirty is None:
            self._index = ReadyIndex(state)
        else:
            self._index.apply(state, dirty)
        return self._index.ready()

    def ready(self) -> list[str]:
        return self._index.ready()

    def _leased(self) -> set[str]:
        return {entry["execution_node_id"] for entry in self._store.active(self._graph_id)}

    def dispatchable(self, limit: int | None = None) -> list[str]:
        """Ready nodes in deterministic priority order, minus those already leased."""
        state = self._journal.replay()
        lengths = critical_path_lengths(state)
        leased = self._leased()
        candidates = [node for node in self._index.ready() if node not in leased]
        candidates.sort(key=lambda node: priority_key(node, state, self._policy,
                                                      critical_path=lengths))
        room = self._policy.max_concurrent - len(leased)
        if room <= 0:
            return []
        return candidates[: min(room, limit if limit is not None else room)]

    def claim(self, node_id: str, *, worker: str, base_revision: int) -> Claim:
        """Claim a node against a graph revision. A moved graph refuses the claim."""
        state = self._journal.replay()
        revision = int(state.get("graph_revision", 0))
        if base_revision != revision:
            return Claim(
                granted=False,
                refusal=ClaimRefusal.STALE_REVISION,
                detail=f"claim was computed against revision {base_revision}, graph is at {revision}",
            )
        if graph_paused(state):
            return Claim(granted=False, refusal=ClaimRefusal.GRAPH_PAUSED)
        nodes = _nodes_by_id(state)
        if node_id not in nodes:
            return Claim(granted=False, refusal=ClaimRefusal.UNKNOWN_NODE)
        if node_id not in full_ready_set(state):
            return Claim(
                granted=False,
                refusal=ClaimRefusal.NOT_READY,
                detail=f"{node_id} is {nodes[node_id].get('lifecycle_state')}",
            )
        if self._store.active_lease_for(self._graph_id, node_id) is not None:
            return Claim(granted=False, refusal=ClaimRefusal.ALREADY_LEASED)

        now = self._clock()
        suffix = f"{abs(hash((node_id, worker, revision))) % 10**9:09d}"
        lease_id = f"LSE-claim-{suffix}"
        fencing_token = f"{revision}:{suffix}"
        if not self._shadow:
            self._store.grant(
                lease_id=lease_id,
                graph_id=self._graph_id,
                node_id=node_id,
                worker=worker,
                fencing_token=fencing_token,
                now=now,
                expires_at=now + self._policy.lease_seconds,
            )
            self._store.record_dispatch(
                graph_id=self._graph_id, node_id=node_id, lease_id=lease_id,
                revision=revision, now=now,
            )
        return Claim(
            granted=True,
            execution_node_id=node_id,
            lease_id=lease_id,
            fencing_token=fencing_token,
            graph_revision=revision,
        )

    def heartbeat(self, lease_id: str, *, progress: Mapping[str, Any] | None = None) -> bool:
        """Extend a lease. Objective progress is carried, not just wall-clock."""
        now = self._clock()
        return self._store.heartbeat(
            lease_id,
            now=now,
            progress=progress or {},
            expires_at=now + self._policy.lease_seconds,
        )

    def release(self, lease_id: str) -> None:
        self._store.release(lease_id)

    def commit_allowed(self, lease_id: str) -> bool:
        """A worker whose lease expired may not commit its claim afterwards."""
        lease = self._store.lease(lease_id)
        if lease is None or lease["state"] != "active":
            return False
        return lease["expires_at"] > self._clock()

    def expire_leases(self) -> list[dict[str, Any]]:
        """Expiry is evidence, never a silent takeover."""
        now = self._clock()
        expired = self._store.expired(self._graph_id, now)
        for entry in expired:
            self._store.expire(entry["lease_id"])
        return expired

    def retry_budget_remaining(self, node_id: str) -> int:
        per_node = self._policy.max_retries_per_node - self._store.retry_count(
            self._graph_id, node_id
        )
        per_graph = self._policy.max_retries_per_graph - self._store.retry_count(self._graph_id)
        return max(0, min(per_node, per_graph))

    def register_retry(self, node_id: str, retry_node_id: str) -> bool:
        """Record a retry attempt. Returns False when the budget is spent."""
        if self.retry_budget_remaining(node_id) <= 0:
            return False
        attempt = self._store.retry_count(self._graph_id, node_id) + 1
        self._store.record_retry(
            retry_id=retry_node_id,
            graph_id=self._graph_id,
            node_id=node_id,
            attempt=attempt,
            now=self._clock(),
        )
        return True

    def liveness(self) -> LivenessReport:
        state = self._journal.replay()
        nodes = _nodes_by_id(state)
        running = tuple(sorted(self._leased()))
        ready = tuple(self._index.ready())
        outstanding = tuple(
            sorted(
                node_id
                for node_id, node in nodes.items()
                if node.get("lifecycle_state") not in TERMINAL
            )
        )
        blocked = tuple(
            node_id for node_id in outstanding if node_id not in ready and node_id not in running
        )
        return LivenessReport(running=running, ready=ready, blocked=blocked,
                              outstanding=outstanding)

    def assert_progress(self) -> LivenessReport:
        """A graph that cannot progress fails loudly rather than spinning or exiting 0."""
        report = self.liveness()
        if report.stalled:
            raise LivenessFailure(
                "no node is running and none can become ready; blocking set: "
                + ", ".join(report.blocked)
            )
        return report

    def resume(self) -> dict[str, Any]:
        """Reconstruct after a crash: rebuild the index, reconcile in-flight leases."""
        self.refresh(None)
        expired = self.expire_leases()
        return {
            "graph_revision": int(self._journal.replay().get("graph_revision", 0)),
            "ready": self._index.ready(),
            "expired_leases": [entry["lease_id"] for entry in expired],
            "still_running": [entry["execution_node_id"] for entry in self._store.active(self._graph_id)],
        }
