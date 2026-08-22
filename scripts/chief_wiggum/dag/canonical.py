"""Canonical byte encoding for replayable DAG records (INV-dag-004)."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from typing import Any

from .errors import ContractViolation, ErrorCode

_ID_KEYS = ("node_id", "intent_node_id", "execution_node_id", "edge_id", "relation_id", "evidence_id", "approval_id", "lease_id", "control_id", "mutation_id")
_SET_FIELDS = {"capabilities", "derived_from", "evidence_refs"}


class _DuplicateKey(ValueError):
    pass


class _NonInteger(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_float(value: str) -> None:
    raise _NonInteger(value)


def _sort_key(item: Any) -> str:
    if isinstance(item, Mapping):
        for key in _ID_KEYS:
            if key in item:
                return str(item[key])
    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize(value: Any, *, field: str = "") -> Any:
    if isinstance(value, float):
        raise _NonInteger(str(value))
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {unicodedata.normalize("NFC", str(key)): _normalize(item, field=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        items = [_normalize(item) for item in value]
        if field in _SET_FIELDS or (items and all(isinstance(item, Mapping) and any(key in item for key in _ID_KEYS) for item in items)):
            items.sort(key=_sort_key)
        return items
    return value


def canonical_json_bytes(record: Mapping[str, Any]) -> bytes:
    normalized = _normalize(record)
    return (json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def validate_canonical_bytes(raw: bytes) -> tuple[ContractViolation, ...]:
    try:
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ValueError("UTF-8 BOM is forbidden")
        decoded = raw.decode("utf-8")
        parsed = json.loads(decoded, object_pairs_hook=_pairs, parse_float=_reject_float)
        expected = canonical_json_bytes(parsed)
        if raw != expected:
            raise ValueError("bytes are not canonical compact NFC JSON with exactly one LF")
    except _DuplicateKey as exc:
        return (ContractViolation(ErrorCode.DUPLICATE_JSON_KEY, f"duplicate JSON key {exc}", phase="canonical"),)
    except (UnicodeDecodeError, json.JSONDecodeError, _NonInteger, ValueError) as exc:
        return (ContractViolation(ErrorCode.CANONICAL_ENCODING_VIOLATION, str(exc), phase="canonical"),)
    return ()
