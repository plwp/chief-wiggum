"""Deterministic admission policy for graph-mutation proposals.

The security property this module owns: **a model proposal is data, never an
instruction.** It travels the same envelope as a rule proposal and is checked at
least as hard. Admission reads only typed fields (operation types, target refs,
actor, authority class, budget delta, evidence refs). It never reads, evaluates,
or branches on a prose field, so instruction-shaped text in `expected_effect`
cannot change any outcome.

Order of checks is deliberate. Cheap structural facts first, then authority,
then evidence, then budget and throttling, and only then #385's engine, which
owns structural validation. Every outcome is journaled with a closed reason code.

@cw-trace guards INV-dag-006 INV-dag-007
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes
from .journal import Decision, GraphJournal, JournalError
from .schemas import load_authority_matrix

# Actors whose proposals are never trusted with privileged operations. A model
# actor is identified structurally, by prefix, not by anything it asserts.
MODEL_ACTOR_PREFIX = "actor:model."


class AdmissionReason(StrEnum):
    """Closed enumeration. Every decision carries exactly one."""

    ADMITTED = "ADMITTED"
    QUEUED_FOR_APPROVAL = "QUEUED_FOR_APPROVAL"
    AUTHORITY_DENIED = "AUTHORITY_DENIED"
    ACTOR_IMPERSONATION = "ACTOR_IMPERSONATION"
    EVIDENCE_UNRESOLVABLE = "EVIDENCE_UNRESOLVABLE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    DUPLICATE_PROPOSAL = "DUPLICATE_PROPOSAL"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    SUBJECT_COOLDOWN = "SUBJECT_COOLDOWN"
    THRASH_DETECTED = "THRASH_DETECTED"
    STRUCTURALLY_INVALID = "STRUCTURALLY_INVALID"
    MALFORMED_PROPOSAL = "MALFORMED_PROPOSAL"


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: AdmissionReason
    detail: str = ""
    journal_seq: int | None = None
    graph_revision: int | None = None
    queue_id: str | None = None
    engine: Decision | None = None


@dataclass(frozen=True)
class AdmissionPolicy:
    """Deterministic limits. All windows are in seconds."""

    budget_unit: str = "tokens"
    budget_envelope: int | None = None
    budget_window_seconds: float = 3600.0
    max_mutations_per_window: int | None = None
    rate_window_seconds: float = 3600.0
    subject_cooldown_seconds: float = 0.0
    thrash_window_seconds: float = 3600.0
    oscillation_threshold: int = 2


class LivenessFailure(Exception):
    """Raised when thrash detection trips. Loud by design, never a silent drop."""


def is_model_actor(actor: str) -> bool:
    return str(actor).startswith(MODEL_ACTOR_PREFIX)


def _operation_subjects(envelope: Mapping[str, Any]) -> list[str]:
    return sorted({str(op.get("target_ref", "")) for op in envelope.get("operations", [])})


def content_hash(envelope: Mapping[str, Any]) -> str:
    """Identity of a proposal's EFFECT, not of its wording.

    Deliberately excludes mutation_id, idempotency_key, actor and every prose
    field: two proposals that would do the same thing are duplicates even if
    their narration differs, and changing the narration must not smuggle a
    duplicate past suppression.
    """
    payload = {
        "graph_id": envelope.get("graph_id"),
        "operations": envelope.get("operations", []),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _transition_signature(envelope: Mapping[str, Any]) -> list[str]:
    """A→B edges this envelope would create, for oscillation detection."""
    signature = []
    for operation in envelope.get("operations", []):
        if operation.get("operation_type") == "transition_node":
            signature.append(
                f"{operation.get('target_ref')}:{operation.get('from_state')}->{operation.get('to_state')}"
            )
    return signature


class AdmissionStore:
    """Durable admission history and approval queue.

    Lives in its own tables alongside the journal so a run's rejection set and
    pending approvals survive a crash and are inspectable after the fact.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._conn = sqlite3.connect(str(self._path), isolation_level=None, timeout=30.0)
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admission_log (
                admission_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                graph_id TEXT NOT NULL,
                mutation_id TEXT,
                actor TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                subjects TEXT NOT NULL,
                transitions TEXT NOT NULL,
                budget_unit TEXT,
                budget_value INTEGER NOT NULL DEFAULT 0,
                admitted INTEGER NOT NULL,
                reason TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                observed_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approval_queue (
                queue_id TEXT PRIMARY KEY,
                graph_id TEXT NOT NULL,
                envelope TEXT NOT NULL,
                evidence TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                requested_at REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                decided_by TEXT,
                decided_at REAL,
                decision_note TEXT
            )
            """
        )

    def close(self) -> None:
        self._conn.close()

    def record(
        self,
        envelope: Mapping[str, Any],
        *,
        admitted: bool,
        reason: AdmissionReason,
        detail: str,
        now: float,
    ) -> None:
        delta = envelope.get("budget_delta") or {}
        self._conn.execute(
            "INSERT INTO admission_log (graph_id, mutation_id, actor, content_hash, subjects,"
            " transitions, budget_unit, budget_value, admitted, reason, detail, observed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(envelope.get("graph_id", "")),
                envelope.get("mutation_id"),
                str(envelope.get("actor", "")),
                content_hash(envelope),
                json.dumps(_operation_subjects(envelope)),
                json.dumps(_transition_signature(envelope)),
                str(delta.get("unit", "")),
                int(delta.get("value", 0) or 0),
                1 if admitted else 0,
                str(reason),
                detail,
                now,
            ),
        )

    def admitted_since(self, graph_id: str, since: float) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT content_hash, subjects, transitions, budget_unit, budget_value, observed_at"
            " FROM admission_log WHERE graph_id=? AND admitted=1 AND observed_at >= ?"
            " ORDER BY admission_seq",
            (graph_id, since),
        )
        return [
            {
                "content_hash": row[0],
                "subjects": json.loads(row[1]),
                "transitions": json.loads(row[2]),
                "budget_unit": row[3],
                "budget_value": row[4],
                "observed_at": row[5],
            }
            for row in rows
        ]

    def rejections(self, graph_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT admission_seq, mutation_id, actor, reason, detail, observed_at"
            " FROM admission_log WHERE graph_id=? AND admitted=0 ORDER BY admission_seq",
            (graph_id,),
        )
        return [
            {
                "admission_seq": row[0],
                "mutation_id": row[1],
                "actor": row[2],
                "reason": row[3],
                "detail": row[4],
                "observed_at": row[5],
            }
            for row in rows
        ]

    def enqueue(
        self,
        envelope: Mapping[str, Any],
        *,
        evidence: Sequence[Mapping[str, Any]],
        requested_by: str,
        now: float,
    ) -> str:
        queue_id = f"APQ-{content_hash(envelope)[:16]}"
        self._conn.execute(
            "INSERT OR IGNORE INTO approval_queue (queue_id, graph_id, envelope, evidence,"
            " requested_by, requested_at) VALUES (?,?,?,?,?,?)",
            (
                queue_id,
                str(envelope.get("graph_id", "")),
                canonical_json_bytes(dict(envelope)).decode(),
                canonical_json_bytes({"records": list(evidence)}).decode(),
                requested_by,
                now,
            ),
        )
        return queue_id

    def pending(self, graph_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT queue_id, envelope, evidence, requested_by, requested_at FROM approval_queue"
            " WHERE graph_id=? AND state='pending' ORDER BY requested_at, queue_id",
            (graph_id,),
        )
        return [
            {
                "queue_id": row[0],
                "envelope": json.loads(row[1]),
                "evidence": json.loads(row[2])["records"],
                "requested_by": row[3],
                "requested_at": row[4],
            }
            for row in rows
        ]

    def get_pending(self, queue_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT queue_id, graph_id, envelope, evidence, requested_by FROM approval_queue"
            " WHERE queue_id=? AND state='pending'",
            (queue_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "queue_id": row[0],
            "graph_id": row[1],
            "envelope": json.loads(row[2]),
            "evidence": json.loads(row[3])["records"],
            "requested_by": row[4],
        }

    def resolve(self, queue_id: str, *, state: str, actor: str, note: str, now: float) -> None:
        self._conn.execute(
            "UPDATE approval_queue SET state=?, decided_by=?, decided_at=?, decision_note=?"
            " WHERE queue_id=? AND state='pending'",
            (state, actor, now, note, queue_id),
        )


class AdmissionController:
    """Applies the deterministic policy, then delegates structure to the engine."""

    def __init__(
        self,
        journal: GraphJournal,
        store: AdmissionStore,
        policy: AdmissionPolicy | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._journal = journal
        self._store = store
        self._policy = policy or AdmissionPolicy()
        self._clock = clock
        self._matrix = {
            row["operation_type"]: row for row in load_authority_matrix()["operations"]
        }

    @property
    def policy(self) -> AdmissionPolicy:
        return self._policy

    def _reject(
        self, envelope: Mapping[str, Any], reason: AdmissionReason, detail: str, now: float
    ) -> AdmissionDecision:
        self._store.record(envelope, admitted=False, reason=reason, detail=detail, now=now)
        return AdmissionDecision(admitted=False, reason=reason, detail=detail)

    def _requires_approval(self, envelope: Mapping[str, Any]) -> bool:
        return any(
            self._matrix.get(str(op.get("operation_type")), {}).get("approval_required")
            for op in envelope.get("operations", [])
        )

    def propose(self, envelope: Mapping[str, Any]) -> AdmissionDecision:
        """Admit, reject, or refuse one proposal. Never raises on bad input."""
        now = self._clock()
        if not isinstance(envelope, Mapping) or not envelope.get("operations"):
            return self._reject(
                envelope if isinstance(envelope, Mapping) else {},
                AdmissionReason.MALFORMED_PROPOSAL,
                "proposal is not an envelope with operations",
                now,
            )

        actor = str(envelope.get("actor", ""))

        # 1. Authority. A model may not claim human authority, and no machine
        #    actor may perform an approval-required operation. Confidence,
        #    urgency and prose are irrelevant here by construction.
        if is_model_actor(actor) and envelope.get("authority_class") == "human":
            return self._reject(
                envelope,
                AdmissionReason.ACTOR_IMPERSONATION,
                "a model actor may not claim human authority",
                now,
            )
        if is_model_actor(actor) and self._requires_approval(envelope):
            return self._reject(
                envelope,
                AdmissionReason.AUTHORITY_DENIED,
                "a model proposal may never perform an approval-required operation",
                now,
            )
        if self._requires_approval(envelope) and envelope.get("authority_class") != "human":
            privileged = sorted(
                str(op.get("operation_type"))
                for op in envelope["operations"]
                if self._matrix.get(str(op.get("operation_type")), {}).get("approval_required")
            )
            return self._reject(
                envelope,
                AdmissionReason.AUTHORITY_DENIED,
                f"approval-required operations {privileged} need human authority",
                now,
            )

        # 2. Evidence must resolve, in THIS graph. The concrete defence against
        #    a proposal citing a justification it invented.
        unresolved = self._unresolved_evidence(envelope)
        if unresolved:
            return self._reject(
                envelope,
                AdmissionReason.EVIDENCE_UNRESOLVABLE,
                f"evidence refs do not resolve in this graph: {unresolved}",
                now,
            )

        # 3. Throttling, then budget. Both look at a window of admitted history.
        throttle = self._throttle(envelope, now)
        if throttle is not None:
            return throttle
        budget = self._budget(envelope, now)
        if budget is not None:
            return budget

        # 4. Structure is the engine's job, not a second implementation of it.
        try:
            decision = self._journal.propose(envelope)
        except JournalError as exc:
            return self._reject(
                envelope, AdmissionReason.STRUCTURALLY_INVALID, str(exc), now
            )
        if not decision.accepted:
            detail = "; ".join(violation.message for violation in decision.violations)
            self._store.record(
                envelope,
                admitted=False,
                reason=AdmissionReason.STRUCTURALLY_INVALID,
                detail=detail,
                now=now,
            )
            return AdmissionDecision(
                admitted=False,
                reason=AdmissionReason.STRUCTURALLY_INVALID,
                detail=detail,
                journal_seq=decision.journal_seq,
                graph_revision=decision.graph_revision,
                engine=decision,
            )

        self._store.record(
            envelope, admitted=True, reason=AdmissionReason.ADMITTED, detail="", now=now
        )
        return AdmissionDecision(
            admitted=True,
            reason=AdmissionReason.ADMITTED,
            journal_seq=decision.journal_seq,
            graph_revision=decision.graph_revision,
            engine=decision,
        )

    def _unresolved_evidence(self, envelope: Mapping[str, Any]) -> list[str]:
        refs = list(envelope.get("evidence_refs", []) or [])
        if not refs:
            return []
        state = self._journal.replay()
        if state.get("graph_id") != envelope.get("graph_id"):
            return sorted(refs)
        known = {record.get("evidence_id") for record in state.get("evidence_records", [])}
        return sorted(ref for ref in refs if ref not in known)

    def _throttle(self, envelope: Mapping[str, Any], now: float) -> AdmissionDecision | None:
        policy = self._policy
        graph_id = str(envelope.get("graph_id", ""))
        window_start = now - max(
            policy.rate_window_seconds, policy.thrash_window_seconds, policy.subject_cooldown_seconds
        )
        history = self._store.admitted_since(graph_id, window_start)
        digest = content_hash(envelope)

        if any(entry["content_hash"] == digest for entry in history):
            return self._reject(
                envelope,
                AdmissionReason.DUPLICATE_PROPOSAL,
                "an identical proposal was already admitted in this window",
                now,
            )

        if policy.max_mutations_per_window is not None:
            recent = [
                entry for entry in history if entry["observed_at"] >= now - policy.rate_window_seconds
            ]
            if len(recent) >= policy.max_mutations_per_window:
                return self._reject(
                    envelope,
                    AdmissionReason.RATE_LIMIT_EXCEEDED,
                    f"{len(recent)} mutations already admitted in this window",
                    now,
                )

        if policy.subject_cooldown_seconds > 0:
            subjects = set(_operation_subjects(envelope))
            for entry in history:
                if entry["observed_at"] < now - policy.subject_cooldown_seconds:
                    continue
                overlap = subjects.intersection(entry["subjects"])
                if overlap:
                    return self._reject(
                        envelope,
                        AdmissionReason.SUBJECT_COOLDOWN,
                        f"subjects {sorted(overlap)} are in cooldown",
                        now,
                    )

        # Oscillation: this envelope would reverse a transition already made in
        # the window. A→B→A is a liveness failure, not a retry.
        proposed = _transition_signature(envelope)
        if proposed:
            seen: list[str] = []
            for entry in history:
                if entry["observed_at"] >= now - policy.thrash_window_seconds:
                    seen.extend(entry["transitions"])
            for signature in proposed:
                node, _, states = signature.partition(":")
                before, _, after = states.partition("->")
                reverse = f"{node}:{after}->{before}"
                if seen.count(reverse) >= policy.oscillation_threshold - 1 and reverse in seen:
                    detail = f"oscillation on {node}: {before}<->{after}"
                    self._store.record(
                        envelope,
                        admitted=False,
                        reason=AdmissionReason.THRASH_DETECTED,
                        detail=detail,
                        now=now,
                    )
                    raise LivenessFailure(detail)
        return None

    def _budget(self, envelope: Mapping[str, Any], now: float) -> AdmissionDecision | None:
        policy = self._policy
        if policy.budget_envelope is None:
            return None
        delta = envelope.get("budget_delta") or {}
        if str(delta.get("unit", policy.budget_unit)) != policy.budget_unit:
            return None
        value = int(delta.get("value", 0) or 0)
        window_start = now - policy.budget_window_seconds
        history = self._store.admitted_since(str(envelope.get("graph_id", "")), window_start)
        # Accumulated, not per-mutation: an overrun split across several small
        # mutations inside one window is still an overrun.
        spent = sum(
            entry["budget_value"]
            for entry in history
            if entry["budget_unit"] == policy.budget_unit
        )
        if spent + value > policy.budget_envelope:
            return self._reject(
                envelope,
                AdmissionReason.BUDGET_EXCEEDED,
                f"{spent} + {value} exceeds envelope {policy.budget_envelope}",
                now,
            )
        return None

    # ------------------------------------------------------- approval queue

    def request_approval(
        self,
        envelope: Mapping[str, Any],
        *,
        evidence: Sequence[Mapping[str, Any]] = (),
        requested_by: str = "actor:proposer",
    ) -> AdmissionDecision:
        """Park a privileged proposal for a human, without touching the graph."""
        now = self._clock()
        queue_id = self._store.enqueue(
            envelope, evidence=evidence, requested_by=requested_by, now=now
        )
        self._store.record(
            envelope,
            admitted=False,
            reason=AdmissionReason.QUEUED_FOR_APPROVAL,
            detail=queue_id,
            now=now,
        )
        return AdmissionDecision(
            admitted=False, reason=AdmissionReason.QUEUED_FOR_APPROVAL, queue_id=queue_id
        )

    def approve(self, queue_id: str, *, approver: str, note: str = "") -> AdmissionDecision:
        """Approve a queued proposal. The admitted mutation's actor is the human."""
        now = self._clock()
        entry = self._store.get_pending(queue_id)
        if entry is None:
            return AdmissionDecision(
                admitted=False,
                reason=AdmissionReason.MALFORMED_PROPOSAL,
                detail=f"no pending approval {queue_id}",
            )
        if is_model_actor(approver):
            return AdmissionDecision(
                admitted=False,
                reason=AdmissionReason.ACTOR_IMPERSONATION,
                detail="a model actor may not approve",
            )
        envelope = dict(entry["envelope"])
        envelope["actor"] = approver
        envelope["authority_class"] = "human"
        decision = self.propose(envelope)
        self._store.resolve(
            queue_id,
            state="approved" if decision.admitted else "approval_failed",
            actor=approver,
            note=note,
            now=now,
        )
        return AdmissionDecision(
            admitted=decision.admitted,
            reason=decision.reason,
            detail=decision.detail,
            journal_seq=decision.journal_seq,
            graph_revision=decision.graph_revision,
            queue_id=queue_id,
            engine=decision.engine,
        )

    def reject_queued(self, queue_id: str, *, approver: str, note: str = "") -> bool:
        if is_model_actor(approver):
            return False
        if self._store.get_pending(queue_id) is None:
            return False
        self._store.resolve(
            queue_id, state="rejected", actor=approver, note=note, now=self._clock()
        )
        return True
