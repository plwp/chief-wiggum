"""Per-language emitter interface (#162).

A "fact kind" is one of the checkers' field-agnostic emission outputs:

- ``"write_site"`` — a candidate write-shaped token
  (``chief_wiggum.write_emission.WriteSite``), consumed by
  ``check_single_writer.py``'s single-write-path audit.
- ``"trace_annotation"`` — an ``@cw-trace`` annotation
  (``chief_wiggum.trace_emission.Annotation``), consumed by
  ``check_traceability.py``'s BR/contract/test coverage graph.

Each language module under ``scripts/emitters/`` implements ``emit(path,
content) -> list[Fact]`` for whichever fact kind(s) it supports (advertised
via ``fact_kinds()``), by delegating to the shared regex families in
``chief_wiggum.write_emission`` / ``chief_wiggum.trace_emission`` — the
existing, golden-parity-tested logic behind #160's emission/claim split.
Nothing here re-implements those regexes; the module boundary is the new
seam #162 asks for, not a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Fact:
    """One language-agnostic fact emitted from a single file.

    ``payload`` is the underlying dataclass instance —
    ``chief_wiggum.write_emission.WriteSite`` for ``kind="write_site"``,
    ``chief_wiggum.trace_emission.Annotation`` for ``kind="trace_annotation"``.
    Kept generic (rather than a tagged union) so a future fact kind (e.g. a
    ``"guard"`` kind for #93's third @cw-* tag family) can be added without
    changing this dataclass's shape.
    """

    kind: str
    payload: object


class LanguageEmitter(Protocol):
    """The per-language emitter interface. A module implementing this
    protocol need not support every fact kind — ``fact_kinds()`` advertises
    what ``emit()`` actually produces."""

    language: str
    extensions: tuple[str, ...]

    def fact_kinds(self) -> tuple[str, ...]:
        ...

    def emit(self, path: str, content: str) -> list[Fact]:
        ...


def facts_of_kind(facts: list[Fact], kind: str) -> list[object]:
    """Unwrap the ``payload`` of every fact of ``kind``, preserving order —
    the common "give me just the WriteSites" / "just the Annotations" query."""
    return [f.payload for f in facts if f.kind == kind]
