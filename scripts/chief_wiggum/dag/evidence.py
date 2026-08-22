"""Typed evidence collectors: runtime observations normalised into records.

Every collector consumes an existing surface and never forks its logic. The
contract that matters here is #289's: a source that could not be read must not
look like a source that was read and found nothing. The v1 `evidence_state`
vocabulary already carries that distinction (`unscanned`, `unavailable`,
`malformed`), so a degraded source produces a record SAYING it was degraded
rather than producing nothing.

No collector reads model prose. @cw-trace guards INV-dag-005
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes
from .schemas import SCHEMA_VERSION


# Evidence classes named by the #386 source table. The value is the id slug and
# must satisfy the vocabulary's stable-id and evidence_type patterns.
class EvidenceClass(StrEnum):
    DEPENDENCY_READINESS = "dependency_readiness"
    UNRESOLVED_MARKER = "unresolved_marker"
    PATH_OVERLAP = "path_overlap"
    GATE_OUTCOME = "gate_outcome"
    PROVIDER_HEALTH = "provider_health"
    WORKER_LEASE = "worker_lease"
    BUDGET_CONSUMPTION = "budget_consumption"
    HUMAN_DECISION = "human_decision"


# Records the source was read and the observation stands.
OBSERVED_STATES = frozenset({"raw", "validated", "admitted"})
# Records the source could NOT be read. These are the loud-degradation states.
DEGRADED_STATES = frozenset({"unavailable", "malformed", "unscanned"})


@dataclass(frozen=True)
class Observation:
    """What one collector saw, including whether it could see at all."""

    evidence_class: EvidenceClass
    records: tuple[dict[str, Any], ...] = ()
    measured: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def degraded(self) -> bool:
        return any(record["status"] in DEGRADED_STATES for record in self.records)

    @property
    def outcome(self) -> str:
        """#289's four states. `inapplicable` is a real answer; `error` is not a pass."""
        if any(record["status"] in ("unavailable", "malformed") for record in self.records):
            return "error"
        if any(record["status"] == "unscanned" for record in self.records):
            return "inapplicable"
        return "findings" if self.records else "pass"


def _slug(value: str) -> str:
    cleaned = "".join(character if character.isalnum() else "-" for character in value.lower())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned or "unknown"


def evidence_id(evidence_class: str, payload: Any) -> str:
    """Content-addressed id, so re-observing the same fact is idempotent.

    A collector that runs twice over unchanged state must not manufacture a
    second evidence record; deriving the id from the payload digest makes that
    structural rather than a caller's responsibility.
    """
    digest = hashlib.sha256(canonical_json_bytes({"class": evidence_class, "payload": payload}))
    return f"EVD-{_slug(evidence_class)}-{int(digest.hexdigest()[:12], 16):015d}"


def make_record(
    evidence_class: EvidenceClass | str,
    *,
    source_ref: str,
    observed_at: str,
    payload: Any,
    status: str = "validated",
) -> dict[str, Any]:
    """Build one schema-valid evidence record (#384 evidence-record schema)."""
    if status not in OBSERVED_STATES | DEGRADED_STATES:
        raise ValueError(f"unknown evidence state {status!r}")
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "evidence_record",
        "evidence_id": evidence_id(str(evidence_class), payload),
        "evidence_type": str(evidence_class),
        "source_ref": source_ref,
        "content_digest": "sha256:"
        + hashlib.sha256(canonical_json_bytes({"payload": payload})).hexdigest(),
        "observed_at": observed_at,
        "status": status,
    }


def degraded_record(
    evidence_class: EvidenceClass | str,
    *,
    source_ref: str,
    observed_at: str,
    reason: str,
    status: str = "unscanned",
) -> dict[str, Any]:
    """A record asserting the source was NOT observed, and why."""
    return make_record(
        evidence_class,
        source_ref=source_ref,
        observed_at=observed_at,
        payload={"degraded": True, "reason": reason},
        status=status,
    )


# --------------------------------------------------------------- collectors


@dataclass(frozen=True)
class CollectorContext:
    """Everything a collector may read. Nothing here is model output."""

    observed_at: str
    repo: Path | None = None
    epic: str = ""
    # Injected surfaces. Each defaults to None meaning "this source is absent",
    # which is a documented degradation, not an error and not a silent pass.
    tracker_ready: Callable[[], Mapping[str, Any]] | None = None
    unresolved_report: Callable[[], Any] | None = None
    worktree_paths: Mapping[str, Sequence[str]] | None = None
    gate_reports: Sequence[Mapping[str, Any]] | None = None
    provider_preflight: Callable[[], Mapping[str, Any]] | None = None
    leases: Sequence[Mapping[str, Any]] | None = None
    budget_report: Mapping[str, Any] | None = None
    human_decisions: Sequence[Mapping[str, Any]] | None = None


def collect_dependency_readiness(context: CollectorContext) -> Observation:
    """Consumes #371's tracker seam. That seam is not built yet, so the normal
    path today is a loud degradation rather than an invented answer."""
    klass = EvidenceClass.DEPENDENCY_READINESS
    if context.tracker_ready is None:
        return Observation(
            klass,
            records=(
                degraded_record(
                    klass,
                    source_ref="tracker:ready",
                    observed_at=context.observed_at,
                    reason="tracker readiness seam (#371) is not available on this backend",
                ),
            ),
            note="no tracker `ready` verb; readiness is unknown, not empty",
        )
    try:
        payload = dict(context.tracker_ready())
    except Exception as exc:  # noqa: BLE001 - any backend failure is a loud degradation
        return Observation(
            klass,
            records=(
                degraded_record(
                    klass,
                    source_ref="tracker:ready",
                    observed_at=context.observed_at,
                    reason=f"tracker readiness call failed: {exc}",
                    status="unavailable",
                ),
            ),
            note="tracker readiness call failed",
        )
    records = tuple(
        make_record(
            klass,
            source_ref=f"tracker:ready:{ticket}",
            observed_at=context.observed_at,
            payload={"ticket": ticket, "ready": bool(ready)},
        )
        for ticket, ready in sorted(payload.items())
    )
    return Observation(klass, records=records, measured={"tickets": len(payload)})


def collect_unresolved_markers(context: CollectorContext) -> Observation:
    """Consumes check_unresolved.scan_report, preserving its four-state outcome."""
    klass = EvidenceClass.UNRESOLVED_MARKER
    if context.unresolved_report is None:
        return Observation(
            klass,
            records=(
                degraded_record(
                    klass,
                    source_ref="check_unresolved",
                    observed_at=context.observed_at,
                    reason="no unresolved-marker scan was supplied",
                ),
            ),
            note="unresolved markers not scanned",
        )
    report = context.unresolved_report()
    outcome = getattr(report, "outcome", "error")
    if outcome == "error":
        return Observation(
            klass,
            records=(
                degraded_record(
                    klass,
                    source_ref="check_unresolved",
                    observed_at=context.observed_at,
                    reason="scan reported unparsed artifacts; markers may exist unseen",
                    status="malformed",
                ),
            ),
            note="unresolved scan hit unparsed artifacts",
        )
    if outcome == "inapplicable":
        return Observation(
            klass,
            records=(
                degraded_record(
                    klass,
                    source_ref="check_unresolved",
                    observed_at=context.observed_at,
                    reason="no artifacts were in scope for the scan",
                ),
            ),
            note="unresolved scan had nothing in scope",
        )
    records = tuple(
        make_record(
            klass,
            source_ref=f"{finding.file}:{finding.location}",
            observed_at=context.observed_at,
            payload={
                "marker": finding.marker,
                "file": finding.file,
                "location": finding.location,
                "blocks": list(finding.tickets),
            },
        )
        for finding in getattr(report, "findings", [])
    )
    return Observation(klass, records=records, measured=dict(getattr(report, "measured", {})))


def collect_path_overlap(context: CollectorContext) -> Observation:
    """Two nodes writing the same path. A proven overlap, not a plan-time guess."""
    klass = EvidenceClass.PATH_OVERLAP
    if context.worktree_paths is None:
        return Observation(
            klass,
            records=(
                degraded_record(
                    klass,
                    source_ref="git:diff",
                    observed_at=context.observed_at,
                    reason="no per-worktree changed-path listing was supplied",
                ),
            ),
            note="path overlap not measured",
        )
    owners: dict[str, list[str]] = {}
    for node, paths in context.worktree_paths.items():
        for path in paths:
            owners.setdefault(path, []).append(node)
    records = tuple(
        make_record(
            klass,
            source_ref=f"path:{path}",
            observed_at=context.observed_at,
            payload={"path": path, "nodes": sorted(nodes)},
        )
        for path, nodes in sorted(owners.items())
        if len(set(nodes)) > 1
    )
    return Observation(klass, records=records, measured={"paths": len(owners)})


def collect_gate_outcomes(context: CollectorContext) -> Observation:
    """Consumes gate JSON. Gates own their logic; this only normalises results."""
    klass = EvidenceClass.GATE_OUTCOME
    if context.gate_reports is None:
        return Observation(
            klass,
            records=(
                degraded_record(
                    klass,
                    source_ref="gates",
                    observed_at=context.observed_at,
                    reason="no gate reports were supplied",
                ),
            ),
            note="gate outcomes not collected",
        )
    records = []
    for report in context.gate_reports:
        outcome = str(report.get("outcome", "error"))
        status = "malformed" if outcome == "error" else "validated"
        records.append(
            make_record(
                klass,
                source_ref=f"gate:{report.get('gate', 'unknown')}",
                observed_at=context.observed_at,
                payload={
                    "gate": report.get("gate"),
                    "outcome": outcome,
                    "subject": report.get("subject"),
                    "measured": report.get("measured", {}),
                },
                status=status,
            )
        )
    return Observation(klass, records=tuple(records), measured={"gates": len(records)})


def collect_provider_health(context: CollectorContext) -> Observation:
    """Consumes #375's preflight. Absent today, so it degrades loudly."""
    klass = EvidenceClass.PROVIDER_HEALTH
    if context.provider_preflight is None:
        return Observation(
            klass,
            records=(
                degraded_record(
                    klass,
                    source_ref="providers:preflight",
                    observed_at=context.observed_at,
                    reason="provider preflight (#375) is not available",
                ),
            ),
            note="provider health unknown; not assumed healthy",
        )
    payload = dict(context.provider_preflight())
    records = tuple(
        make_record(
            klass,
            source_ref=f"provider:{provider}",
            observed_at=context.observed_at,
            payload={"provider": provider, "state": state},
        )
        for provider, state in sorted(payload.items())
    )
    return Observation(klass, records=records, measured={"providers": len(payload)})


def collect_worker_leases(context: CollectorContext) -> Observation:
    """Lease expiry and objective progress, replacing the prose wall-clock timeout."""
    klass = EvidenceClass.WORKER_LEASE
    if context.leases is None:
        return Observation(
            klass,
            records=(
                degraded_record(
                    klass,
                    source_ref="leases",
                    observed_at=context.observed_at,
                    reason="no lease or heartbeat data was supplied",
                ),
            ),
            note="worker liveness unknown",
        )
    records = tuple(
        make_record(
            klass,
            source_ref=f"lease:{lease.get('lease_id')}",
            observed_at=context.observed_at,
            payload={
                "lease_id": lease.get("lease_id"),
                "execution_node_id": lease.get("execution_node_id"),
                "expired": bool(lease.get("expired")),
                "progress": lease.get("progress"),
            },
        )
        for lease in context.leases
    )
    return Observation(klass, records=records, measured={"leases": len(records)})


def collect_budget_consumption(context: CollectorContext) -> Observation:
    klass = EvidenceClass.BUDGET_CONSUMPTION
    if context.budget_report is None:
        return Observation(
            klass,
            records=(
                degraded_record(
                    klass,
                    source_ref="ticket_cost",
                    observed_at=context.observed_at,
                    reason="no cost report was supplied",
                ),
            ),
            note="budget consumption unknown; not assumed zero",
        )
    report = dict(context.budget_report)
    records = (
        make_record(
            klass,
            source_ref="ticket_cost",
            observed_at=context.observed_at,
            payload={
                "consumed": report.get("consumed"),
                "envelope": report.get("envelope"),
                "unit": report.get("unit", "tokens"),
            },
        ),
    )
    return Observation(klass, records=records, measured=report)


def collect_human_decisions(context: CollectorContext) -> Observation:
    klass = EvidenceClass.HUMAN_DECISION
    decisions = context.human_decisions or ()
    records = tuple(
        make_record(
            klass,
            source_ref=f"human:{decision.get('actor', 'unknown')}",
            observed_at=context.observed_at,
            payload={
                "decision": decision.get("decision"),
                "subject": decision.get("subject"),
                "actor": decision.get("actor"),
            },
        )
        for decision in decisions
    )
    return Observation(klass, records=records, measured={"decisions": len(records)})


COLLECTORS: dict[EvidenceClass, Callable[[CollectorContext], Observation]] = {
    EvidenceClass.DEPENDENCY_READINESS: collect_dependency_readiness,
    EvidenceClass.UNRESOLVED_MARKER: collect_unresolved_markers,
    EvidenceClass.PATH_OVERLAP: collect_path_overlap,
    EvidenceClass.GATE_OUTCOME: collect_gate_outcomes,
    EvidenceClass.PROVIDER_HEALTH: collect_provider_health,
    EvidenceClass.WORKER_LEASE: collect_worker_leases,
    EvidenceClass.BUDGET_CONSUMPTION: collect_budget_consumption,
    EvidenceClass.HUMAN_DECISION: collect_human_decisions,
}


def collect_all(context: CollectorContext) -> list[Observation]:
    """Run every collector. Order is stable so a replay is deterministic."""
    return [COLLECTORS[klass](context) for klass in EvidenceClass]


def changed_paths(worktree: Path, base: str = "HEAD") -> list[str]:
    """Changed paths in a worktree, for the overlap collector.

    Returns an empty list only when git answered. A git failure raises, so the
    caller degrades loudly instead of recording "no overlap".
    """
    result = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--name-only", base],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed in {worktree}: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def evidence_ids(observations: Iterable[Observation]) -> list[str]:
    return [record["evidence_id"] for observation in observations for record in observation.records]
