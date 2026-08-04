"""One epic-tree walk, shared by every consumer that needs it (#326).

``check_traceability.py``'s five extractors (``extract_defined_ids``,
``find_id_bearing_artifacts``, ``scan_malformed_ids``,
``extract_coverage_requirements``, ``scan_epic_annotations``),
``chief_wiggum.hashing.hash_epic_definitions``/``find_id_bearing_artifacts``/
``scan_malformed_ids`` (consumed by ``ratchet.py``'s ``load_contract_hashes``/
``contract_measurement``), and ``code_query.py``'s ``_locate_definitions`` each
independently ``rglob``'d the same epic directory and ``read_text()``'d the
same files — up to nine redundant walks across a single ``check_traceability
check()`` + ``ratchet.py score`` pass over one epic tree.

``build_epic_model(root)`` walks ``root`` exactly ONCE (every ``.md``/``.json``
file, read exactly once) and derives every one of those views from that single
read. It is a pure, stateless function — built FRESH per call, so per-invocation
callers (``check()``, ``cmd_score``) build it once and every extractor reads
from it; there is no cross-invocation cache here (the ``code_query.py``
no-cross-query-memoization doctrine — see its module docstring — extends to
this model: it is rebuilt on every fresh ``build_epic_model`` call, never
persisted to disk or a module-level cache).

**Two views of "defined IDs", by design, not by accident** (chief-wiggum#326):
``defined_ids`` (id -> kind) mirrors ``check_traceability.extract_defined_ids``
— the ``justifications/`` subtree is EXCLUDED (a waiver's own ``"id"`` field
must never be misread as a new declaration) and it is built from a whole-text
regex pass per file. ``raw_definitions`` (id, rel, line, is_justification) is
the SUPERSET ``code_query.py``'s ``_locate_definitions`` has always consumed
— justification files INCLUDED, built line-by-line (so it carries the line
number ``show`` dereferences) — because a waiver record naming a real
contract's id is still a useful `file:line` locator even though it must never
count as a *declaration* for coverage/soundness purposes. Merging these two
into one filtered view would silently change one consumer's behavior; kept
separate, each consumer's existing golden output is unaffected by this
refactor.

Text is decoded with ``errors="replace"`` (matching
``chief_wiggum.hashing.hash_epic_definitions``'s existing, deliberately lossy
policy) rather than the un-suffixed ``check_traceability`` extractors' bare
``path.read_text()`` — a harmless-in-practice widening: epic docs are
authored markdown/JSON and essentially always valid UTF-8, and no existing
test exercises a non-UTF-8 epic doc either way. This is the one documented,
deliberate policy choice this module makes when unifying two previously
independent implementations that happened to disagree on decode strictness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from chief_wiggum.hashing import (
    ID_BEARING_ARTIFACTS,
    hash_markdown_defs,
    stable_hash,
    walk_json_ids,
)
from chief_wiggum.trace_emission import Annotation, canonical_id, emit_epic_annotations, kind_of
from chief_wiggum.trace_ids import DEFINE_RE, ID_RE, near_miss_ids
from chief_wiggum.trace_links import is_justification_path


@dataclass(frozen=True)
class EpicFile:
    rel: str
    path: Path
    text: str
    is_justification: bool


@dataclass
class EpicModel:
    """Parsed snapshot of one epic-tree walk. Every field below is derived
    from ``files`` at construction time in :func:`build_epic_model` — nothing
    here re-reads the filesystem."""

    root: Path
    files: dict[str, EpicFile] = field(default_factory=dict)  # rel -> EpicFile, sorted-walk order
    unreadable: list[str] = field(default_factory=list)  # rel paths that raised OSError on read

    # code_query._locate_definitions' exact superset (justification INCLUDED,
    # line-tracked, source order) — (canonical_id, rel, line, is_justification).
    raw_definitions: list[tuple[str, str, int, bool]] = field(default_factory=list)

    # check_traceability.extract_defined_ids' exact view (justification EXCLUDED).
    defined_ids: dict[str, str] = field(default_factory=dict)  # id -> kind

    # check_traceability.find_id_bearing_artifacts' exact view.
    id_bearing_artifacts: list[str] = field(default_factory=list)

    # check_traceability.scan_malformed_ids' exact view.
    malformed_ids: list[dict] = field(default_factory=list)

    # check_traceability.extract_coverage_requirements' exact view.
    coverage_requirements: dict[str, list[str]] = field(default_factory=dict)

    # check_traceability.scan_epic_annotations' exact view.
    epic_annotations: list[Annotation] = field(default_factory=list)

    # chief_wiggum.hashing.hash_epic_definitions' exact view.
    definition_hashes: dict[str, str] = field(default_factory=dict)


def _collect_coverage_requirements(node: object, out: dict[str, list[str]]) -> None:
    """Identical to check_traceability._collect_coverage_requirements — moved
    here so it can run inside the single walk; check_traceability re-exports
    the public ``extract_coverage_requirements`` entry point unchanged."""
    if isinstance(node, dict):
        cid = node.get("id")
        reqs = node.get("coverage_requires")
        if isinstance(cid, str) and ID_RE.fullmatch(cid) and isinstance(reqs, list) and reqs:
            out[canonical_id(cid)] = [str(r) for r in reqs]
        for v in node.values():
            _collect_coverage_requirements(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_coverage_requirements(v, out)


def build_epic_model(root: str | Path) -> EpicModel:
    """Walk ``root`` exactly once, reading every ``.md``/``.json`` file
    exactly once, and derive every consumer's view from that single read.
    ``root`` may be a single epic dir (``docs/epics/<slug>``, as
    ``check_traceability.py``/``code_query.py`` use it) or a whole epics root
    holding several epics (``docs/epics``, as ``ratchet.py`` uses it via
    ``cfg.epic_docs``) — the walk is root-agnostic, matching every original
    implementation it replaces."""
    root = Path(root)
    model = EpicModel(root=root)
    if not root.is_dir():
        # Matches chief_wiggum.hashing.hash_epic_definitions' guard (the
        # stricter of the two original guards this model replaces — the
        # five check_traceability extractors used `exists()`, which for a
        # root that exists but is a plain FILE would go on to call
        # `root.rglob("*")` and risk a crash; `is_dir()` degrades that case
        # gracefully instead, a strict safety improvement no existing test
        # exercises either way).
        return model

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in (".md", ".json"):
            continue
        rel = str(path.relative_to(root))
        try:
            text = path.read_text(errors="replace")
        except OSError:
            model.unreadable.append(rel)
            continue
        is_just = is_justification_path(root, path)
        model.files[rel] = EpicFile(rel=rel, path=path, text=text, is_justification=is_just)

    _compute(model)
    return model


def _compute(model: EpicModel) -> None:
    hash_collected: dict[str, list[str]] = {}

    for rel, f in model.files.items():
        text = f.text

        # --- raw_definitions: EVERY file, including justifications/, line-tracked
        # (code_query._locate_definitions' exact algorithm: setdefault-style
        # first-occurrence-wins is the CALLER's job — this list carries every
        # occurrence in source order so any consumer can pick its own policy). ---
        for i, line in enumerate(text.splitlines(), start=1):
            for m in DEFINE_RE.finditer(line):
                nid = canonical_id(m.group(1))
                model.raw_definitions.append((nid, rel, i, f.is_justification))

        if f.is_justification:
            # Everything below mirrors check_traceability's/hashing's exclusion
            # of the justifications/ subtree from declarations/hashes/coverage/
            # annotations — a waiver's own "id" field must never phantom-define.
            continue

        # --- defined_ids: whole-text finditer, matches extract_defined_ids exactly ---
        for m in DEFINE_RE.finditer(text):
            nid = canonical_id(m.group(1))
            model.defined_ids[nid] = kind_of(nid)

        # --- id_bearing_artifacts ---
        if Path(rel).name in ID_BEARING_ARTIFACTS and text.strip():
            model.id_bearing_artifacts.append(rel)

        # --- malformed_ids ---
        for token in near_miss_ids(text):
            line_no = next(
                (i for i, ln in enumerate(text.splitlines(), 1) if token in ln), 0
            )
            model.malformed_ids.append({
                "file": rel,
                "line": line_no,
                "token": token,
                "expected": "KIND-SLUG-NNN (e.g. INV-order-001)",
            })

        is_json = f.path.suffix == ".json"
        doc = None
        if is_json:
            try:
                doc = json.loads(text)
            except json.JSONDecodeError:
                doc = None

        # --- coverage_requirements (json only) ---
        if is_json and doc is not None:
            _collect_coverage_requirements(doc, model.coverage_requirements)

        # --- epic_annotations ---
        model.epic_annotations.extend(emit_epic_annotations(rel, text))

        # --- definition_hashes ---
        if is_json:
            if doc is not None:
                walk_json_ids(doc, hash_collected)
        else:
            for cid, hashes in hash_markdown_defs(text).items():
                hash_collected.setdefault(cid, []).extend(hashes)

    model.definition_hashes = {cid: stable_hash(*sorted(hs)) for cid, hs in hash_collected.items()}
