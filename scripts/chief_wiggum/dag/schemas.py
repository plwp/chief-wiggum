"""Offline Draft 2020-12 schema catalog for DAG records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource

from .errors import ContractViolation, ErrorCode

SCHEMA_VERSION = "1.0.0"
SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas" / "dag" / "v1"


@lru_cache(maxsize=1)
def _catalog_manifest() -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / "schema-catalog.json").read_text())


@lru_cache(maxsize=1)
def schema_catalog() -> dict[str, dict[str, Any]]:
    return {
        record_type: json.loads((SCHEMA_DIR / filename).read_text())
        for record_type, filename in _catalog_manifest()["records"].items()
    }


@lru_cache(maxsize=1)
def _registry() -> Registry:
    schemas = [
        json.loads(path.read_text())
        for path in sorted(SCHEMA_DIR.glob("*.schema.json"))
    ]
    return Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas
    )


def load_authority_matrix() -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / "authority-matrix.json").read_text())


def _pointer(parts: object) -> str:
    return "".join(f"/{str(part).replace('~', '~0').replace('/', '~1')}" for part in parts)


def validate_record(
    record: Mapping[str, Any], expected_type: str | None = None
) -> tuple[ContractViolation, ...]:
    version = record.get("schema_version")
    if version is None:
        return (
            ContractViolation(
                ErrorCode.SCHEMA_VERSION_MISSING,
                "schema_version is required",
                "/schema_version",
                "version",
            ),
        )
    if version != SCHEMA_VERSION:
        return (
            ContractViolation(
                ErrorCode.SCHEMA_VERSION_UNSUPPORTED,
                f"unsupported DAG schema version {version!r}; expected {SCHEMA_VERSION!r}",
                "/schema_version",
                "version",
                {"actual": version, "supported": [SCHEMA_VERSION]},
            ),
        )
    actual_type = record.get("record_type")
    record_type = expected_type or actual_type
    if expected_type is not None and actual_type != expected_type:
        return (
            ContractViolation(
                ErrorCode.RECORD_TYPE_MISMATCH,
                f"record_type {actual_type!r} does not match expected {expected_type!r}",
                "/record_type",
                "record_type",
            ),
        )
    schema = schema_catalog().get(str(record_type))
    if schema is None:
        return (
            ContractViolation(
                ErrorCode.RECORD_TYPE_MISMATCH,
                f"unknown DAG record type {record_type!r}",
                "/record_type",
                "record_type",
            ),
        )
    validator = jsonschema.Draft202012Validator(schema, registry=_registry())
    errors = sorted(validator.iter_errors(record), key=lambda item: list(item.absolute_path))
    return tuple(
        ContractViolation(
            ErrorCode.SCHEMA_INVALID,
            error.message,
            _pointer(error.absolute_path),
            "schema",
            {"validator": error.validator},
        )
        for error in errors
    )
