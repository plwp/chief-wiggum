"""Shared stable-hash helpers (#160).

``stable_hash`` originated in ``ratchet.py`` (contract-definition hashing) and is
now the single home for it, imported back into ``ratchet.py`` so there is one
implementation, not a copy. ``scanner_version`` builds on it for a
hash-derived scanner version: the version of a gate script IS the hash of its
own source plus its ``chief_wiggum`` dependencies, so a forgotten manual bump
(the previous failure mode — a hand-edited constant nobody remembers to touch)
is structurally impossible. Any edit to the scanner or a dependency changes the
version automatically.

``hash_epic_definitions`` (#169) is the per-ID **contract-block hashing**
``ratchet.py`` uses to detect weakened/removed contracts, relocated here so
``check_traceability.py`` can reuse the exact same hashes for its suspect-link
propagation (a link is SUSPECT when the ID it was verified against has a
definition hash that no longer matches what was recorded at verification
time) — one hashing implementation, not a parallel copy that could drift.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from chief_wiggum.trace_ids import ID_RE, canonical_id, near_miss_ids
from chief_wiggum.trace_ids import MD_DEFINE_RE as DEFINE_RE
from chief_wiggum.trace_links import is_justification_path

# Artifacts /architect authors that MUST carry stable IDs — their presence
# (with content) is what separates "nothing to measure" (inapplicable) from
# "artifacts exist, the scanner saw none of them" (error, chief-wiggum#281/
# #295: a broken instrument, never a clean pass). Single home so
# check_traceability.py's own copy and ratchet.py's (#295) can never drift
# apart on what counts. adr.md/integration-tests.md/traceability.md/
# retrospective.md are deliberately excluded: they legitimately carry no
# declarations.
ID_BEARING_ARTIFACTS = frozenset({
    "contracts.md", "contracts.json",
    "invariants.md",
    "state-machines.md", "state-machines.json",
    "architecture.json",
})


def find_id_bearing_artifacts(root: str | Path) -> list[str]:
    """Root-relative paths of ID-bearing artifacts under ``root`` that exist
    AND have content (#295; mirrors check_traceability.find_id_bearing_artifacts
    — ``root`` here is typically ratchet's whole ``epic_docs`` tree, which may
    hold several epics, rather than a single epic dir). Empty-but-present
    files do NOT count: nothing to parse yet is "nothing to measure", not a
    broken instrument."""
    root = Path(root)
    found: list[str] = []
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name not in ID_BEARING_ARTIFACTS:
            continue
        if is_justification_path(root, path):
            continue
        try:
            if path.read_text().strip():
                found.append(str(path.relative_to(root)))
        except OSError:
            continue
    return found


def scan_malformed_ids(root: str | Path) -> list[dict]:
    """Declaration-position near-misses across every ``.md``/``.json`` file
    under ``root`` (#295) — reuses ``near_miss_ids`` (chief_wiggum.trace_ids,
    landed by #281), NOT a second detector; this is only the directory walk
    around the one shared regex complement, so ratchet.py and
    check_traceability.py can never disagree on what's malformed."""
    root = Path(root)
    out: list[dict] = []
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".md", ".json") or not path.is_file():
            continue
        if is_justification_path(root, path):
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        for token in near_miss_ids(text):
            line = next(
                (i for i, ln in enumerate(text.splitlines(), 1) if token in ln), 0
            )
            out.append({
                "file": str(path.relative_to(root)),
                "line": line,
                "token": token,
                "expected": "KIND-SLUG-NNN (e.g. INV-order-001)",
            })
    return out


def stable_hash(*parts: str) -> str:
    """Deterministic hash of one or more string parts (order-sensitive, NUL-joined)."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def scanner_version(*module_paths: str | Path) -> str:
    """Hash-derived version for a scanner: ``stable_hash`` over the raw source text
    of ``module_paths`` (the scanner module itself plus its ``chief_wiggum`` deps),
    in the order given. Order matters — callers pass a fixed, deterministic list
    (their own file first, then each imported dependency module) so the same
    inputs always produce the same version, and any source change (in the
    scanner OR a dependency) changes it."""
    texts = [Path(p).read_text() for p in module_paths]
    return stable_hash(*texts)


def hash_markdown_defs(text: str) -> dict[str, list[str]]:
    """Map each stable ID declared in markdown ``text`` to the hash of its block.

    A block runs from the declaring line to the next line that declares another
    ID (or EOF), whitespace-normalized — so reformatting doesn't read as
    weakening, but any wording change to the REQUIRES/ENSURES does.

    Keys are in **canonical form** (``canonical_id``: uppercase kind, lowercase
    slug) so they join cleanly against the traceability scanner's canonicalized
    annotation targets — a raw-cased key (e.g. ``CTR-BIL-001``) on one side of
    that join silently records no sidecar link and can never go suspect
    (PR #181 review). The hash VALUES are unaffected: they cover the block
    text, not the key.
    """
    lines = text.splitlines()
    decls: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = DEFINE_RE.search(line)
        if m:
            decls.append((i, m.group(1)))
    out: dict[str, list[str]] = {}
    for idx, (start, cid) in enumerate(decls):
        end = decls[idx + 1][0] if idx + 1 < len(decls) else len(lines)
        block = "\n".join(ln.rstrip() for ln in lines[start:end]).strip()
        out.setdefault(canonical_id(cid), []).append(stable_hash(block))
    return out


def walk_json_ids(node, out: dict[str, list[str]]) -> None:
    """Recursively hash every JSON object carrying a stable-ID ``id`` field.

    Keys are canonicalized (see ``hash_markdown_defs``); hash values cover the
    node's JSON content and are unaffected."""
    if isinstance(node, dict):
        cid = node.get("id")
        if isinstance(cid, str) and ID_RE.fullmatch(cid):
            out.setdefault(canonical_id(cid), []).append(
                stable_hash(json.dumps(node, sort_keys=True))
            )
        for v in node.values():
            walk_json_ids(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_json_ids(v, out)


def hash_epic_definitions(root: str | Path) -> dict[str, str]:
    """Map stable ID -> definition hash across every ``.md``/``.json`` file under
    ``root`` (an epic docs root, or a single epic directory).

    An ID declared in several places hashes as the sorted combination, so the
    result is deterministic and any one declaration changing is visible. A
    missing ``root`` degrades gracefully to an empty map. The ``justifications/``
    subtree (waiver records, #169) is excluded — a waiver's own ``"id"`` field
    names the CTR/INV it waives and must never be misread as a NEW declaration.
    """
    root = Path(root)
    collected: dict[str, list[str]] = {}
    if root.is_dir():
        for f in sorted(root.rglob("*.md")):
            if is_justification_path(root, f):
                continue
            for cid, hashes in hash_markdown_defs(f.read_text(errors="replace")).items():
                collected.setdefault(cid, []).extend(hashes)
        for f in sorted(root.rglob("*.json")):
            if is_justification_path(root, f):
                continue
            try:
                doc = json.loads(f.read_text(errors="replace"))
            except json.JSONDecodeError:
                continue
            walk_json_ids(doc, collected)
    return {cid: stable_hash(*sorted(hs)) for cid, hs in collected.items()}
