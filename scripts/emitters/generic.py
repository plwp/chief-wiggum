"""Generic (language-agnostic) regex-tier emitter (#162): rung 2 of the
fallback chain — language-specific emitter -> **generic regex tier** ->
skip-with-warning.

This is exactly the CURRENT (pre-#162) behavior of
``check_single_writer.py`` / ``check_traceability.py`` for any extension in
``config/languages.json``'s ``generic_tier`` (Java, Ruby, and Rust's ``.rs``
today — see the ``rust`` entry's note: Rust IS scanned by this tier already,
just without a dedicated func-regex for enclosing-symbol resolution). The
underlying emission functions are already suffix-driven (comment markers,
symbol regexes), so this module's ``emit()`` is identical in shape to the
tier-1 language modules — the only difference is that no dedicated
``scripts/emitters/<language>.py`` module exists for these extensions.
"""

from __future__ import annotations

from pathlib import Path

from chief_wiggum import languages as cw_languages
from chief_wiggum.trace_emission import emit_source_annotations
from chief_wiggum.write_emission import emit_write_sites

from .base import Fact

language = "generic"

# Computed at import time from config/languages.json's generic_tier — a
# module-level tuple (not a function) so the registry can treat every emitter
# module (tier-1 or generic) uniformly: `mod.extensions`.
extensions: tuple[str, ...] = tuple(sorted(cw_languages.generic_tier_extensions()))


def fact_kinds() -> tuple[str, ...]:
    return ("write_site", "trace_annotation")


def emit(path: str, content: str) -> list[Fact]:
    suffix = Path(path).suffix
    facts: list[Fact] = [Fact("write_site", s) for s in emit_write_sites(path, content)]
    facts += [
        Fact("trace_annotation", a)
        for a in emit_source_annotations(path, content, suffix)
    ]
    return facts
