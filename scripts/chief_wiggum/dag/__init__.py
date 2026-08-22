"""Versioned dynamic-DAG contracts and pure validators."""

from .canonical import canonical_json_bytes, validate_canonical_bytes
from .compatibility import ProjectionResult, dependency_block_to_intent_graph, project_legacy_waves
from .errors import ContractViolation, ErrorCode
from .journal import Decision, GraphJournal, JournalError, Snapshot
from .schemas import SCHEMA_VERSION, load_authority_matrix, schema_catalog, validate_record
from .semantics import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    validate_mutation,
    validate_snapshot,
    validate_transition,
)

__all__ = [
    "ContractViolation",
    "ErrorCode",
    "Decision",
    "GraphJournal",
    "JournalError",
    "LEGAL_TRANSITIONS",
    "ProjectionResult",
    "SCHEMA_VERSION",
    "Snapshot",
    "TERMINAL_STATES",
    "canonical_json_bytes",
    "dependency_block_to_intent_graph",
    "load_authority_matrix",
    "project_legacy_waves",
    "schema_catalog",
    "validate_canonical_bytes",
    "validate_mutation",
    "validate_record",
    "validate_snapshot",
    "validate_transition",
]
