"""Python emitter (#162): write-site + trace-annotation facts for ``.py`` files.

See ``scripts/emitters/go.py`` for the delegation rationale — identical
pattern, just the ``.py`` extension and suffix.
"""

from __future__ import annotations

from chief_wiggum.trace_emission import emit_source_annotations
from chief_wiggum.write_emission import emit_write_sites

from .base import Fact

language = "python"
extensions: tuple[str, ...] = (".py",)


def fact_kinds() -> tuple[str, ...]:
    return ("write_site", "trace_annotation")


def emit(path: str, content: str) -> list[Fact]:
    facts: list[Fact] = [Fact("write_site", s) for s in emit_write_sites(path, content)]
    facts += [
        Fact("trace_annotation", a)
        for a in emit_source_annotations(path, content, ".py")
    ]
    return facts
