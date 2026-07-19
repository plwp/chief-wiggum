"""TypeScript/JavaScript emitter (#162): write-site + trace-annotation facts
for ``.ts``/``.tsx``/``.js``/``.jsx`` files.

See ``scripts/emitters/go.py`` for the delegation rationale. One module
covers all four extensions because the underlying regex families
(``chief_wiggum.write_emission`` / ``chief_wiggum.trace_emission``) already
treat them identically (same comment marker, same TS_FUNC_RE for enclosing
symbols) — splitting them into four modules would add files without adding
behavior.
"""

from __future__ import annotations

from chief_wiggum.trace_emission import emit_source_annotations
from chief_wiggum.write_emission import emit_write_sites

from .base import Fact

language = "typescript"
extensions: tuple[str, ...] = (".ts", ".tsx", ".js", ".jsx")


def fact_kinds() -> tuple[str, ...]:
    return ("write_site", "trace_annotation")


def emit(path: str, content: str) -> list[Fact]:
    suffix = path[path.rfind("."):] if "." in path else ""
    facts: list[Fact] = [Fact("write_site", s) for s in emit_write_sites(path, content)]
    facts += [
        Fact("trace_annotation", a)
        for a in emit_source_annotations(path, content, suffix)
    ]
    return facts
