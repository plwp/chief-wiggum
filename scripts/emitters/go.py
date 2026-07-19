"""Go emitter (#162): write-site + trace-annotation facts for ``.go`` files.

Delegates to ``chief_wiggum.write_emission.emit_write_sites`` and
``chief_wiggum.trace_emission.emit_source_annotations`` — the same
golden-parity-tested regex families ``check_single_writer.py`` and
``check_traceability.py`` have always used. This module is the per-language
SEAM (config/languages.json's ``go`` entry -> here), not a reimplementation:
moving the regex bodies out of the shared modules would risk drift for no
benefit.
"""

from __future__ import annotations

from chief_wiggum.trace_emission import emit_source_annotations
from chief_wiggum.write_emission import emit_write_sites

from .base import Fact

language = "go"
extensions: tuple[str, ...] = (".go",)


def fact_kinds() -> tuple[str, ...]:
    return ("write_site", "trace_annotation")


def emit(path: str, content: str) -> list[Fact]:
    facts: list[Fact] = [Fact("write_site", s) for s in emit_write_sites(path, content)]
    facts += [
        Fact("trace_annotation", a)
        for a in emit_source_annotations(path, content, ".go")
    ]
    return facts
