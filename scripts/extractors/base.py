#!/usr/bin/env python3
"""
Abstract base class and shared types for stitch-audit extractors.

Each language/framework extractor inherits from Extractor and implements
the four required methods: detect, discover, extract, scan_patterns.

Shared data types (Field, Schema) provide a stack-agnostic representation
that stitch_diff.py consumes without knowing the source language.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Field:
    """A single field extracted from a schema definition."""

    name: str  # Field name as it appears in this layer
    type: str | None = None  # Type (language-specific string)
    line: int = 0  # Line number in source file
    required: bool | None = None  # True/False/None if unknown
    tags: dict[str, str] = field(default_factory=dict)  # json_tag, bson_tag, validator, etc.

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Schema:
    """A schema extracted from a single file — struct, interface, Zod schema, etc."""

    file: str  # Relative path within repo
    layer: str  # frontend_forms | api_handlers | database_ops | admin_views
    schema_type: str  # go_struct | bson_m_op | ts_interface | zod_schema | form_fields
    name: str  # Struct/interface/schema name
    fields: list[Field] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# Standard layer names — extractors map their files to these.
LAYERS = frozenset({
    "frontend_forms",
    "api_handlers",
    "database_ops",
    "admin_views",
})


class Extractor(ABC):
    """Base class for language/framework-specific schema extractors."""

    @abstractmethod
    def name(self) -> str:
        """Short identifier, e.g. 'go_mongo', 'typescript'."""

    @abstractmethod
    def detect(self, repo_path: Path) -> bool:
        """Return True if this repo contains files this extractor handles."""

    @abstractmethod
    def discover(self, repo_path: Path, keyword: str) -> dict[str, list[Path]]:
        """Find files related to keyword.

        Returns dict of layer_name -> file list.
        Layer names must be from LAYERS.
        """

    @abstractmethod
    def extract(self, file_path: Path, keyword: str | None = None) -> list[Schema]:
        """Extract schemas from a single file. Returns list of Schema objects."""

    @abstractmethod
    def scan_patterns(self, repo_path: Path, scan_path: str | None = None) -> dict:
        """Scan for convention inconsistencies. Returns convention counts."""


def schemas_to_json(schemas: list[Schema]) -> str:
    """Serialize a list of Schema objects to JSON."""
    return json.dumps([s.to_dict() for s in schemas], indent=2)


def schemas_from_json(data: str) -> list[Schema]:
    """Deserialize JSON back into Schema objects."""
    raw = json.loads(data)
    result = []
    for item in raw:
        fields = [Field(**f) for f in item.pop("fields", [])]
        result.append(Schema(**item, fields=fields))
    return result
