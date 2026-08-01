#!/usr/bin/env python3
"""population.py — the shared file population the debt engines scan (#214).

One definition of "which files are in play" for the dead-code / test-health /
marker engines, so the four #214 engines and ``debt_inventory.py`` agree with
each other AND with the existing quality engines:

  - tracked files only (``git ls-files`` — the same base as
    ``complexity.tracked_files``), with ``complexity.EXCLUDE_RE`` applied
    (docs/vendor/node_modules/dist/migrations/…),
  - plus the generated-file patterns no engine should ever report on
    (``*_pb2.py``, ``*.min.js``, ``*_gen.go``, lockfiles),
  - plus the caller's ``path_filter`` (the #213 ``Resolver.in_scope``
    predicate) — engines receive the same population the hotspot/quality
    layer ranks, computed WITHIN scope, never over the full repo.

Language identity comes from ``complexity.EXT_LANG`` (py/go/ts/tsx/js/jsx) so
"unscanned language" counts line up with what the complexity battery calls a
language.
"""

from __future__ import annotations

import re

from . import complexity

# Generated / minified artifacts that survive complexity.EXCLUDE_RE because
# they sit next to product code: protobuf stubs, minified bundles, go:generate
# output, lockfiles. Reported on by NO debt engine.
GENERATED_RE = re.compile(
    r"(_pb2(_grpc)?\.py$)|(\.min\.(js|css)$)|(\.generated\.)|(_gen\.go$)|"
    r"(\.pb\.go$)|((^|/)(package-lock\.json|go\.sum|yarn\.lock|pnpm-lock\.yaml)$)",
    re.IGNORECASE,
)

# Test-file identity, per language — the conservative name conventions the
# ecosystem actually uses (same shapes as complexity.TEST_RE, kept importable
# per-language so test_health can report which mapping it applied).
TEST_FILE_RES = {
    "python": re.compile(r"(^|/)test_[^/]+\.py$|_test\.py$"),
    "go": re.compile(r"_test\.go$"),
    "typescript": re.compile(r"\.(test|spec)\.tsx?$"),
    "javascript": re.compile(r"\.(test|spec)\.jsx?$"),
}


def lang_of(path: str) -> str | None:
    """Language name for a path per ``complexity.EXT_LANG``, else None."""
    dot = path.rfind(".")
    if dot < 0:
        return None
    return complexity.EXT_LANG.get(path[dot:])


def is_test_file(path: str) -> bool:
    lang = lang_of(path)
    if lang is None:
        return False
    if TEST_FILE_RES[lang].search(path):
        return True
    # Directory conventions (tests/, __tests__/, e2e/) count for any language.
    return bool(re.search(r"(^|/)(tests?|__tests__|e2e)/", path))


def unknown_language_files(repo: str, path_filter=None) -> dict[str, int]:
    """Tracked, non-excluded, non-generated files whose extension maps to NO
    known language — counted by extension so engines can surface them as
    unscanned instead of silently dropping them (codex review, #214). Files
    with no extension are keyed "(none)". Doc/asset extensions that are
    never source (md, json, yml, txt, images, lock) are not counted."""
    NON_SOURCE = {".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml",
                  ".ini", ".cfg", ".lock", ".svg", ".png", ".jpg", ".gif",
                  ".ico", ".css", ".scss", ".html", ".xml", ".csv", ".sum",
                  ".mod", ".work", ".example", ".sample", ".gitignore", ""}
    counts: dict[str, int] = {}
    for f in complexity.tracked_files(repo):
        if GENERATED_RE.search(f):
            continue
        if lang_of(f) is not None:
            continue
        if path_filter is not None and not path_filter(f):
            continue
        ext = ("." + f.rsplit(".", 1)[1].lower()) if "." in f.rsplit("/", 1)[-1] else ""
        if ext in NON_SOURCE:
            continue
        counts[ext or "(none)"] = counts.get(ext or "(none)", 0) + 1
    return counts


def tracked_source(repo: str, path_filter=None) -> list[str]:
    """Repo-relative (git-style, forward-slash) source files in the scan
    population: tracked, non-excluded, non-generated, a known language, and
    inside the caller's #213 scope filter. Sorted for determinism."""
    out = []
    for f in complexity.tracked_files(repo):
        if GENERATED_RE.search(f):
            continue
        if lang_of(f) is None:
            continue
        if path_filter is not None and not path_filter(f):
            continue
        out.append(f)
    return sorted(out)
