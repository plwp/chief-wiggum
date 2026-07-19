"""Emitter registry + fallback chain (#162).

::

    language-specific emitter  ->  generic regex tier  ->  skip-with-warning

For a given file's extension: use the dedicated tier-1 module
(go/python/typescript) if one exists; otherwise the generic regex tier
(java/ruby/rust today — see ``config/languages.json``'s ``generic_tier``);
otherwise the file is unsupported. "Unsupported" is never silent — callers
(``check_single_writer.py`` / ``check_traceability.py``) turn
:func:`unsupported_extensions` into an explicit coverage warning rather than
letting the file vanish from the scan with no trace.

This module deliberately imports only ``chief_wiggum.*`` and its sibling
``scripts/emitters/*.py`` modules — never ``check_single_writer`` /
``check_traceability`` — so those checkers can import ``emitters`` back
without a circular import.
"""

from __future__ import annotations

from pathlib import Path

from chief_wiggum import languages as cw_languages

from . import generic as _generic
from . import go as _go
from . import python as _python
from . import typescript as _typescript
from .base import Fact, LanguageEmitter, facts_of_kind  # noqa: F401

# suffix -> emitter module, for every BUILT tier-1 language.
_LANGUAGE_EMITTERS: dict[str, object] = {}
for _mod in (_go, _python, _typescript):
    for _ext in _mod.extensions:
        _LANGUAGE_EMITTERS[_ext] = _mod


def tier_for_suffix(suffix: str) -> str:
    """"language" | "generic" | "unsupported" — the fallback-chain rung a
    file's extension lands on. Mirrors ``config/languages.json``'s tiers."""
    if suffix in _LANGUAGE_EMITTERS:
        return "language"
    if suffix in _generic.extensions:
        return "generic"
    return "unsupported"


def emitter_for_suffix(suffix: str):
    """The emitter module for ``suffix``, or ``None`` if unsupported."""
    if suffix in _LANGUAGE_EMITTERS:
        return _LANGUAGE_EMITTERS[suffix]
    if suffix in _generic.extensions:
        return _generic
    return None


def emit(path: str, content: str) -> tuple[list[Fact], str]:
    """Facts for one file, via the fallback chain, plus the tier that
    produced them (``"language"``/``"generic"``/``"unsupported"`` — the
    latter always returns an empty fact list)."""
    suffix = Path(path).suffix
    mod = emitter_for_suffix(suffix)
    if mod is None:
        return [], "unsupported"
    tier = "language" if suffix in _LANGUAGE_EMITTERS else "generic"
    return mod.emit(path, content), tier


def unsupported_extensions() -> frozenset[str]:
    """The curated set of recognized-but-unsupported extensions (#162) —
    used by the checkers to surface a coverage warning, never a silent skip."""
    return cw_languages.unsupported_extensions()


def is_recognized_unsupported(suffix: str) -> bool:
    return suffix in unsupported_extensions()


def registered_language_extensions() -> dict[str, str]:
    """``suffix -> language name`` for every REGISTERED tier-1 emitter module.
    Must equal ``chief_wiggum.languages.extension_to_language()`` — the
    declared matrix and the actual registry may never drift; validated by
    :func:`validate_registry_matches_matrix` (and pinned in
    ``tests/test_emitters.py``)."""
    return {ext: mod.language for ext, mod in _LANGUAGE_EMITTERS.items()}


def validate_registry_matches_matrix() -> list[str]:
    """Mechanical parity check between ``config/languages.json`` (the declared
    matrix) and this registry (the actual emitter modules). Returns a list of
    human-readable problems — empty means the two agree:

    - every BUILT matrix language must have a registered emitter module of the
      same name covering exactly its declared extensions;
    - every registered tier-1 extension must be declared by a built matrix
      language (no undeclared/unregistered strays in either direction);
    - the generic module's extensions must equal the matrix's generic tier.
    """
    problems: list[str] = []
    declared = cw_languages.extension_to_language()
    registered = registered_language_extensions()
    for ext, lang_name in declared.items():
        got = registered.get(ext)
        if got is None:
            problems.append(
                f"matrix declares built language {lang_name!r} for {ext!r} but no "
                f"emitter module is registered for it"
            )
        elif got != lang_name:
            problems.append(
                f"matrix declares {ext!r} -> {lang_name!r} but the registry maps it "
                f"to emitter {got!r}"
            )
    for ext, mod_name in registered.items():
        if ext not in declared:
            problems.append(
                f"emitter {mod_name!r} registers {ext!r} but the matrix does not "
                f"declare it for any built language"
            )
    matrix_generic = cw_languages.generic_tier_extensions()
    if set(_generic.extensions) != matrix_generic:
        problems.append(
            f"generic emitter covers {sorted(_generic.extensions)} but the matrix's "
            f"generic tier declares {sorted(matrix_generic)}"
        )
    return problems


__all__ = [
    "Fact",
    "LanguageEmitter",
    "emit",
    "emitter_for_suffix",
    "facts_of_kind",
    "is_recognized_unsupported",
    "registered_language_extensions",
    "tier_for_suffix",
    "unsupported_extensions",
    "validate_registry_matches_matrix",
]
