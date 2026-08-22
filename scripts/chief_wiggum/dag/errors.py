"""Stable, provider-neutral validation errors for the DAG contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    SCHEMA_VERSION_MISSING = "SCHEMA_VERSION_MISSING"
    SCHEMA_VERSION_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"
    RECORD_TYPE_MISMATCH = "RECORD_TYPE_MISMATCH"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    STATE_TRANSITION_INVALID = "STATE_TRANSITION_INVALID"
    TERMINAL_STATE_IMMUTABLE = "TERMINAL_STATE_IMMUTABLE"
    READINESS_DERIVED_NOT_MUTABLE = "READINESS_DERIVED_NOT_MUTABLE"
    SCHEDULABLE_CYCLE = "SCHEDULABLE_CYCLE"
    DANGLING_NODE_REFERENCE = "DANGLING_NODE_REFERENCE"
    DANGLING_EVIDENCE_REFERENCE = "DANGLING_EVIDENCE_REFERENCE"
    AUTHORITY_APPROVAL_REQUIRED = "AUTHORITY_APPROVAL_REQUIRED"
    REQUIRES_APPROVAL_MISMATCH = "REQUIRES_APPROVAL_MISMATCH"
    COMPENSATION_TARGET_MISSING = "COMPENSATION_TARGET_MISSING"
    COMPENSATION_TARGET_INVALID = "COMPENSATION_TARGET_INVALID"
    IDEMPOTENCY_KEY_DIVERGENT = "IDEMPOTENCY_KEY_DIVERGENT"
    CANONICAL_ENCODING_VIOLATION = "CANONICAL_ENCODING_VIOLATION"
    DUPLICATE_JSON_KEY = "DUPLICATE_JSON_KEY"
    GRAPH_ID_MISMATCH = "GRAPH_ID_MISMATCH"
    BASE_REVISION_MISMATCH = "BASE_REVISION_MISMATCH"
    DUPLICATE_RECORD_ID = "DUPLICATE_RECORD_ID"
    COMPILATION_REFERENCE_INVALID = "COMPILATION_REFERENCE_INVALID"


@dataclass(frozen=True)
class ContractViolation:
    code: ErrorCode
    message: str
    path: str = ""
    phase: str = "semantic"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "path": self.path,
            "phase": self.phase,
            "details": self.details,
        }
