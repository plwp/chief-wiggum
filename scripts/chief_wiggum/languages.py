"""Loader + helpers for ``config/languages.json`` (#162): the declared
per-language support matrix.

Three consumers read this single artifact instead of each hand-rolling its
own idea of "which languages/extensions are supported":

- ``check_single_writer.py`` / ``check_traceability.py`` derive their
  ``SOURCE_EXTS`` allow-list from :func:`all_known_extensions` and their
  "recognized but unsupported" coverage warning from
  :func:`unsupported_extensions` (via ``scripts/emitters``).
- ``scripts/check_deps.py`` maps a language's ``dep_profile`` to the
  dependency profile that installs its LSP/toolchain (e.g. Go -> ``go-lsp``).
- ``scripts/render_languages_doc.py`` renders the whole matrix into
  ``docs/languages.md`` so the doc can never silently drift from the artifact.

See ``docs/languages.md`` for the rendered, human-readable matrix.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "languages.json"


@dataclass(frozen=True)
class Language:
    """One entry in ``config/languages.json``'s ``languages`` map."""

    name: str
    tier: str  # "1" for a built tier-1 emitter, or a maturity label e.g. "designed"
    status: str
    extensions: tuple[str, ...]
    lsp: str | None
    emitters: tuple[str, ...]
    test_parser: str | None
    extractor: str | None
    func_regex: bool
    dep_profile: str | None = None
    trigger: str | None = None
    requires: tuple[str, ...] = ()
    note: str | None = None

    @property
    def built(self) -> bool:
        """True for an actually-built tier-1 emitter (Go/Python/TypeScript
        today) — False for a documented-but-unbuilt slot (Rust)."""
        return self.tier == "1"


@lru_cache(maxsize=8)
def _load_cached(path_str: str) -> dict:
    return json.loads(Path(path_str).read_text())


def load(path: Path | str = DEFAULT_PATH) -> dict:
    """Raw parsed ``config/languages.json``. Cached per path (the matrix is
    read-only artifact data, not runtime state)."""
    return _load_cached(str(path))


def languages(path: Path | str = DEFAULT_PATH) -> dict[str, Language]:
    """``{name: Language}`` for every entry in the matrix, in file order."""
    data = load(path)
    out: dict[str, Language] = {}
    for name, entry in (data.get("languages") or {}).items():
        out[name] = Language(
            name=name,
            tier=str(entry.get("tier", "")),
            status=str(entry.get("status", "")),
            extensions=tuple(entry.get("extensions", [])),
            lsp=entry.get("lsp"),
            emitters=tuple(entry.get("emitters", [])),
            test_parser=entry.get("test_parser"),
            extractor=entry.get("extractor"),
            func_regex=bool(entry.get("func_regex", False)),
            dep_profile=entry.get("dep_profile"),
            trigger=entry.get("trigger"),
            requires=tuple(entry.get("requires", [])),
            note=entry.get("note"),
        )
    return out


def extension_to_language(path: Path | str = DEFAULT_PATH) -> dict[str, str]:
    """``suffix -> language name`` for BUILT tier-1 languages only (a dedicated
    ``scripts/emitters/<name>.py`` module exists). A designed-but-unbuilt slot
    (Rust) is deliberately excluded here — its extension falls through to
    :func:`generic_tier_extensions` instead, matching current runtime behavior."""
    out: dict[str, str] = {}
    for lang in languages(path).values():
        if not lang.built:
            continue
        for ext in lang.extensions:
            out[ext] = lang.name
    return out


def generic_tier_extensions(path: Path | str = DEFAULT_PATH) -> frozenset[str]:
    """Extensions scanned by the generic (language-agnostic) regex tier — no
    dedicated per-language emitter module, but still covered."""
    data = load(path)
    return frozenset((data.get("generic_tier") or {}).get("extensions", []))


def unsupported_extensions(path: Path | str = DEFAULT_PATH) -> frozenset[str]:
    """Curated set of recognized programming-language extensions with NO
    emitter coverage at all — encountering one during a scan must produce an
    explicit warning, never a silent skip (see check_single_writer.py /
    check_traceability.py ``unsupported_extension_counts``)."""
    data = load(path)
    return frozenset((data.get("unsupported_extensions") or {}).get("extensions", []))


def all_known_extensions(path: Path | str = DEFAULT_PATH) -> frozenset[str]:
    """Union of tier-1 (built) + generic-tier extensions — the full set of
    extensions a scanner actually walks. This is the single source of truth
    ``check_single_writer.SOURCE_EXTS`` / ``check_traceability.SOURCE_EXTS``
    derive from (the latter appends its own non-language verification-artifact
    extensions on top: ``.rego``/``.yaml``/``.yml``)."""
    return frozenset(set(extension_to_language(path)) | generic_tier_extensions(path))
