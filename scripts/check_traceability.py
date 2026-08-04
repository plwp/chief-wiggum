#!/usr/bin/env python3
"""Traceability graph checker (#36): business rule -> contract -> code -> test.

Builds a machine-readable traceability graph from stable IDs in the epic docs
plus ``@cw-trace`` annotations in source/tests, and reports:

- **orphan business rules** — a ``BR-*`` with no ``realizes`` link.
- **uncovered contracts** — a ``CTR-*``/``INV-*`` with no ``guards``/``ensures``
  code annotation.
- **untested contracts** — a ``CTR-*``/``INV-*`` with no ``verifies`` test annotation.
- **dangling annotations** — an annotation referencing an ID that isn't defined.
- **invalid links** — a verb whose source/target node types violate the TIM schema.

Annotation grammar (uniform across languages, LOBSTER-style namespaced tag):

    @cw-trace <verb> <ID> [<ID> ...]      verbs: realizes|guards|ensures|verifies

The checker is a *separate pass* (not compile-time enforcement) and degrades
gracefully: a repo/epic with no annotations reports absence rather than crashing.

Mirrors ``check_unresolved.py``. Gates:
    --gate soundness  -> fail on orphan BRs + dangling refs + invalid links (/architect)
    --gate coverage   -> fail on uncovered + untested contracts (/close-epic)

Internally, scanning is split into per-file EMISSION (``emit_epic_annotations``,
``emit_source_annotations``: every ``@cw-trace`` annotation in one file) and
report-time joins against the defined-ID set (``build_report``) — see
``docs/traceability.md``. ``--changed-since <ref>`` scopes the ``--source`` scan
to files changed since ``ref`` (never used by /close-epic's coverage gate, which
must see the whole repo). ``--scanner-version`` prints a hash of this module's
source plus its ``chief_wiggum`` deps.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Meta-location resolver (chief-wiggum#213): routes the DEFAULT trace-links
# sidecar location through the target's elected footprint mode (embedded:
# <repo>/docs/quality — the status quo; sidecar: the external quality dir).
# --links keeps precedence.
import artifacts  # noqa: E402

# The per-language emitter registry (#162): language-specific emitter -> generic
# regex tier -> skip-with-warning. Used by scan_source/unsupported_extension_counts
# below to surface files with NO emitter coverage instead of dropping them silently.
import emitters  # noqa: E402

# External trace-link store (#213 Phase C): in sidecar mode, in-source
# @cw-trace annotations are replaced by symbol-anchored entries in
# <meta_root>/quality/external-links.json. A verified-ok entry joins the
# annotation set (an external `verifies` satisfies a contract exactly like an
# in-source `@cw-trace verifies`); a suspect entry (anchored-symbol hash drift)
# does NOT satisfy coverage and is surfaced with the existing suspect links;
# an unresolved entry is surfaced as a warning, never dropped.
#
# Per-file emission cache (#327): every gate-scanned file's annotations are
# memoized keyed by (rel, blob_sha, scanner_hash) — see the module docstring
# for why both halves of the key are load-bearing. Kept on ONE line (not
# ruff's multi-line parenthesized form) — the CTR-fh-041 dep-completeness
# test's regex parses `from chief_wiggum import ...` as a single line.
from chief_wiggum import external_links, findings_cache  # noqa: E402

# Grandfather waivers (#215 F5): `adopt.py grandfather` records pre-adoption
# baseline findings in <meta root>/adoption/grandfathered.json — for THIS gate,
# keyed `check_traceability:uncovered:<STABLE-ID>` /
# `check_traceability:untested:<STABLE-ID>` (see chief_wiggum.grandfather for
# the full key grammar). Mirrors the JUSTIFIED-waiver mechanics: a gap matching
# a NON-EXPIRED entry moves into `grandfathered_contracts` (reported, never a
# silent pass) and does not block coverage; an EXPIRED entry does NOT waive —
# the gap goes back to blocking, labeled "EXPIRED grandfather".
from chief_wiggum import grandfather as cw_grandfather  # noqa: E402

# The declared language support matrix (#162) — SOURCE_EXTS below is derived
# from it (tier-1 + generic-tier extensions) plus this checker's own
# verification-artifact extensions. The emitter fallback chain (scripts/emitters/)
# reports files with no coverage at all as an explicit warning rather than a
# silent skip. See config/languages.json + docs/languages.md.
from chief_wiggum import languages as cw_languages  # noqa: E402

# Shared with check_single_writer.py: the hash-derived --scanner-version and
# the git-native manifest helper behind --changed-since (#160). walk_source_files
# prunes submodules/nested git checkouts from the FULL scan so both scan modes
# agree on the file universe (the manifest never surfaces submodule blobs).
# hash_epic_definitions (#169) is the same contract-block hashing ratchet.py
# uses for weakening detection — one implementation, not a parallel copy.
from chief_wiggum.hashing import (  # noqa: E402
    ID_BEARING_ARTIFACTS,
    hash_epic_definitions,
    scanner_version,
)
from chief_wiggum.manifest import (  # noqa: E402
    ManifestError,
    build_manifest,
    changed_paths,
    walk_source_files,
)

# Decode-defensive bulk-source read (#282): a bare path.read_text() crashes
# scan_source with UnicodeDecodeError on a UTF-16 (or otherwise non-UTF-8)
# file — the surrounding `except OSError` at the read site does NOT catch it
# (UnicodeDecodeError is not an OSError). Shared with check_single_writer.py
# so the two scanners can't drift on decode policy. See chief_wiggum/textio.py.
from chief_wiggum.textio import read_text_safe  # noqa: E402

# The @cw-trace annotation emission family (Annotation, classify_source_kind,
# canonical_id, kind_of, parse_annotations, emit_source_annotations) moved to
# chief_wiggum.trace_emission (#162) so scripts/emitters/*.py can sit BEHIND
# the same per-file emission logic this checker uses — re-exported here
# unchanged so every existing `check_traceability.X` reference keeps working
# (golden parity; see tests/test_traceability_golden.py). canonical_id's HOME
# is chief_wiggum.trace_ids (#181: definition-hash keys and annotation targets
# must join on the same canonical form); trace_emission re-exports it.
from chief_wiggum.trace_emission import (  # noqa: E402, F401
    PROBE_DIR_PARTS,
    Annotation,
    canonical_id,
    classify_source_kind,
    emit_source_annotations,
    kind_of,
    parse_annotations,
)

# The ID grammar and verb set are shared with ratchet.py and the TIM schema —
# a kind added in one place but not the others is silently dropped, so all
# three build from chief_wiggum.trace_ids (cross-checked in tests). TRACE_RE
# isn't called directly by every function here anymore (parse_annotations
# moved to chief_wiggum.trace_emission, which imports it itself) but stays
# re-exported as `check_traceability.TRACE_RE` for backward compatibility
# (see tests/test_trace_ids.py's identity checks).
from chief_wiggum.trace_ids import DEFINE_RE, ID_RE, TRACE_RE, near_miss_ids  # noqa: E402, F401

# Suspect-link propagation (#169): a link is SUSPECT when the ID it was
# verified against has a definition hash that no longer matches the hash
# recorded in docs/quality/trace-links.json at the time the link last passed
# a gate. JUSTIFIED waivers (docs/epics/<slug>/justifications/*.json) let an
# uncovered/untested contract satisfy coverage with a committed, ticket-backed
# reason instead of a false "guards"/"verifies" annotation. See
# chief_wiggum.trace_links and docs/traceability.md.
from chief_wiggum.trace_links import (  # noqa: E402
    SIDECAR_RELPATH,
    build_sidecar,
    find_suspect_links,
    is_justification_path,
    load_justifications,
    load_sidecar,
    write_sidecar,
)

DEFAULT_SCHEMA = Path(__file__).resolve().parents[1] / "templates" / "formal-models" / "tim-schema.json"

# Code/test annotations live in code/test files — not markdown. Prose docs
# (including this checker's own examples and the epic's realizes lines) are
# handled only by scan_epic_annotations, so they aren't double-counted.
# .rego/.yaml/.yml are verification artifacts (policy/probe/telemetry — #166),
# not a programming language in config/languages.json, so they're appended here
# rather than folded into the shared matrix. Backward-compatible: identical to
# the pre-#162 hardcoded set.
VERIFICATION_EXTS = {".rego", ".yaml", ".yml"}
SOURCE_EXTS = cw_languages.all_known_extensions() | VERIFICATION_EXTS

# Artifacts /architect authors that MUST carry stable IDs. Their presence (with
# content) is what separates "no epic yet" (inapplicable) from "epic present,
# nothing parseable" (error — a broken instrument, #281/#289). adr.md,
# integration-tests.md, traceability.md and retrospective.md are deliberately
# NOT here: they legitimately carry no declarations.
# Homed in chief_wiggum.hashing (#295) so ratchet.py's own vacuous-contract-hash
# detection uses the SAME set — imported above alongside hash_epic_definitions.


@dataclass
class TraceReport:
    defined: dict[str, str] = field(default_factory=dict)  # id -> kind
    orphan_business_rules: list[str] = field(default_factory=list)
    uncovered_contracts: list[str] = field(default_factory=list)
    untested_contracts: list[str] = field(default_factory=list)
    dangling: list[dict] = field(default_factory=list)
    invalid_links: list[dict] = field(default_factory=list)
    # Suspect propagation (#169): links recorded in docs/quality/trace-links.json
    # whose target's definition hash has since changed. Report-only — does NOT
    # affect soundness_ok/coverage_ok (see docs/gate-rollout.md). Distinct from
    # dangling (target gone) and uncovered/untested (no link at all): here a
    # link DOES exist, its claim is just stale.
    suspect_links: list[dict] = field(default_factory=list)
    suspect_contracts: list[str] = field(default_factory=list)
    # JUSTIFIED waivers (#169): an uncovered/untested contract with a valid,
    # non-expired, ticket-backed justification record is moved out of
    # uncovered_contracts/untested_contracts and reported here instead — a
    # third status, neither a clean pass nor a silent gap.
    justified_contracts: list[dict] = field(default_factory=list)
    expired_justifications: list[dict] = field(default_factory=list)
    invalid_justifications: list[dict] = field(default_factory=list)
    # Grandfather waivers (#215 F5): a coverage gap matching a NON-EXPIRED
    # adoption-grandfather entry moves here (out of uncovered/untested — it
    # does not block under --gate coverage). An EXPIRED entry does NOT waive:
    # its gap STAYS in uncovered/untested (blocks again — visible pressure)
    # and is additionally listed here so the report can label it.
    grandfathered_contracts: list[dict] = field(default_factory=list)
    expired_grandfathers: list[dict] = field(default_factory=list)
    # Source files that could not be READ at all during scan_source
    # (chief-wiggum#282) — permissions, a race where the file vanished
    # mid-walk, a broken symlink, ... A UTF-16/non-UTF-8 file is NOT here:
    # read_text_safe BOM-sniffs and falls back to a lossy decode rather than
    # skipping, so it stays fully scanned. Report-only by design: an unscanned
    # file is visible (with its path) but never flips soundness_ok/coverage_ok
    # by itself — a repo full of binary-ish files must not become unusable.
    unscanned: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # #281: a declaration that ALMOST matches the grammar (two-segment
    # `INV-001`). The scanner cannot see it, so it silently shrinks the
    # measured graph — the partial-drift case (contracts.json fine,
    # invariants.md two-segment) that is completely silent today.
    malformed_ids: list[dict] = field(default_factory=list)
    # #281: ID-bearing artifacts that exist, have content, and yielded ZERO
    # parseable IDs.
    unparsed_artifacts: list[dict] = field(default_factory=list)
    # #313 item 3: set (non-None) when the external link store has entries
    # and NOT ONE of them anchored — the SECOND way this checker's own
    # measurement can be broken, distinct from #281's zero-parseable-IDs case
    # (that one is about the epic docs; this one is about the external
    # store's regex/LSP anchoring). Both drive applicability to "error", but
    # they need different explanations (the #281 banner text is specifically
    # about stable IDs, which is wrong here) — this field lets callers tell
    # them apart without string-sniffing warnings.
    external_link_store_error: str | None = None
    # #289 item 5: the measured denominator (how many ID-bearing artifacts
    # were actually scanned) — so a zero is visible even inside an otherwise
    # green report, not just inferred from an empty finding list.
    id_bearing_artifacts_scanned: int = 0
    # Vacuous-pass fix (chief-wiggum#213 Phase E, widened to three states by
    # #281): "inapplicable" when there is nothing to measure (no ID-bearing
    # artifact with content anywhere); "error" when ID-bearing artifacts exist
    # WITH CONTENT but the scanner parsed ZERO stable IDs out of them — a
    # BROKEN INSTRUMENT, never a pass. Vocabulary is #289's standard outcome
    # model (pass | findings | inapplicable | error).
    #   "applicable"   — the graph was measured
    #   "inapplicable" — nothing exists to measure
    #   "error"        — artifacts exist and the scanner saw none of them
    applicability: str = "applicable"

    @property
    def outcome(self) -> str:
        """The standard four-state gate outcome (#289): pass | findings |
        inapplicable | error.

        Derived, never stored — "failed to run" and "found nothing" must not
        be able to drift apart into two fields that disagree.
        """
        if self.applicability == "error":
            return "error"
        if self.applicability == "inapplicable":
            return "inapplicable"
        return "pass" if (self.soundness_ok and self.coverage_ok) else "findings"

    @property
    def counts(self) -> dict:
        return {
            "defined": len(self.defined),
            "orphan_business_rules": len(self.orphan_business_rules),
            "uncovered_contracts": len(self.uncovered_contracts),
            "untested_contracts": len(self.untested_contracts),
            "dangling": len(self.dangling),
            "invalid_links": len(self.invalid_links),
            "suspect_links": len(self.suspect_links),
            "justified_contracts": len(self.justified_contracts),
            "expired_justifications": len(self.expired_justifications),
            "invalid_justifications": len(self.invalid_justifications),
            "grandfathered": len(self.grandfathered_contracts),
            "expired_grandfathers": len(self.expired_grandfathers),
            "malformed_ids": len(self.malformed_ids),
            "unparsed_artifacts": len(self.unparsed_artifacts),
            "unscanned": len(self.unscanned),
        }

    @property
    def soundness_ok(self) -> bool:
        # #281: a broken measurement is a soundness failure, not a pass. An
        # epic whose declarations the scanner cannot see has an EMPTY graph
        # by breakage, not by cleanliness. `unscanned` (#282) deliberately does
        # NOT participate: report-only by binding decision — a repo full of
        # binary-ish files must not become unusable under --gate.
        return not (self.orphan_business_rules or self.dangling or self.invalid_links
                    or self.malformed_ids or self.unparsed_artifacts)

    @property
    def coverage_ok(self) -> bool:
        return not (self.uncovered_contracts or self.untested_contracts)

    def to_dict(self) -> dict:
        return {
            "defined": self.defined,
            "counts": self.counts,
            "applicability": self.applicability,
            "outcome": self.outcome,
            "measured": {
                "id_bearing_artifacts": self.id_bearing_artifacts_scanned,
                "defined_ids": len(self.defined),
            },
            "soundness_ok": self.soundness_ok,
            "coverage_ok": self.coverage_ok,
            "orphan_business_rules": self.orphan_business_rules,
            "uncovered_contracts": self.uncovered_contracts,
            "untested_contracts": self.untested_contracts,
            "dangling": self.dangling,
            "invalid_links": self.invalid_links,
            "malformed_ids": self.malformed_ids,
            "unparsed_artifacts": self.unparsed_artifacts,
            "external_link_store_error": self.external_link_store_error,
            "unscanned": self.unscanned,
            "suspect_links": self.suspect_links,
            "suspect_contracts": self.suspect_contracts,
            "justified_contracts": self.justified_contracts,
            "expired_justifications": self.expired_justifications,
            "invalid_justifications": self.invalid_justifications,
            "grandfathered_contracts": self.grandfathered_contracts,
            "expired_grandfathers": self.expired_grandfathers,
            "warnings": self.warnings,
        }


def load_schema(path: Path = DEFAULT_SCHEMA) -> dict:
    return json.loads(Path(path).read_text())


def extract_defined_ids(epic_dir: str | Path) -> dict[str, str]:
    """Collect IDs *declared* in the epic's prose + model artifacts.

    The ``justifications/`` subtree (waiver records, #169) is excluded: a
    waiver's own ``"id"`` field names the CTR/INV it waives and must never be
    misread as a new stable-ID declaration.
    """
    root = Path(epic_dir)
    defined: dict[str, str] = {}
    if not root.exists():
        return defined
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".md", ".json") or not path.is_file():
            continue
        if is_justification_path(root, path):
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        for m in DEFINE_RE.finditer(text):
            node_id = canonical_id(m.group(1))
            defined[node_id] = kind_of(node_id)
    return defined


def find_id_bearing_artifacts(epic_dir: str | Path) -> list[str]:
    """Epic-relative paths of ID-bearing artifacts that exist AND have content.

    Empty-but-present files do NOT count: an epic dir holding a zero-byte
    contracts.md has nothing to parse yet and stays *inapplicable* — the
    error state (#281) is reserved for "there is text, and the scanner saw
    nothing in it".
    """
    root = Path(epic_dir)
    if not root.exists():
        return []
    found: list[str] = []
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


def scan_malformed_ids(epic_dir: str | Path) -> list[dict]:
    """Declaration-position near-misses (#281) across the epic's .md/.json
    artifacts.

    Same walk as ``extract_defined_ids`` (including the ``justifications/``
    exclusion) so the two can never disagree about what was read.
    """
    root = Path(epic_dir)
    out: list[dict] = []
    if not root.exists():
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


def _collect_coverage_requirements(node, out: dict[str, list[str]]) -> None:
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


def extract_coverage_requirements(epic_dir: str | Path) -> dict[str, list[str]]:
    """Per-contract coverage-requirement alternatives (LOBSTER pattern, #169).

    A JSON model entry may declare ``"coverage_requires": ["unit-test", "probe"]``
    alongside its ``"id"``: the contract is tested only by a ``verifies``
    annotation whose ``source_kind`` is ONE of the listed alternatives (an "A
    OR B" requirement), instead of the default "any verifying kind counts".
    Absent for a given ID, behavior is unchanged. Degrades gracefully on a
    missing epic dir or unparsable JSON (skipped, not raised).
    """
    root = Path(epic_dir)
    out: dict[str, list[str]] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*.json")):
        if is_justification_path(root, path):
            continue
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        _collect_coverage_requirements(doc, out)
    return out


def emit_epic_annotations(rel: str, text: str) -> list[Annotation]:
    """Per-file EMISSION: every ``@cw-trace`` annotation declared in one epic doc's
    ``text``, attributed to the nearest stable ID declared above it. Pure function
    of file content — no knowledge of the full defined-ID set (that join is
    query-time, in ``build_report``).

    A realizes/derive annotation is attributed to the nearest stable ID
    *declared above it* in the same file, so it is tied to a real source.
    Any kind that can be a link SOURCE qualifies (BUD-/EDG-/... declare
    derive links the same way CTR-/INV- declare realizes — #166). BR is
    only ever a link target, so a BR declaration RESETS attribution: an
    annotation under a BR heading must not inherit an earlier contract
    (that would let a stray realizes clear the BR's own orphan status).
    """
    annotations: list[Annotation] = []
    nearest_contract: str | None = None
    for i, line in enumerate(text.splitlines(), start=1):
        for dm in DEFINE_RE.finditer(line):
            if kind_of(dm.group(1)) == "BR":
                nearest_contract = None
            else:
                nearest_contract = canonical_id(dm.group(1))
        for verb, ids in parse_annotations(line):
            src_kind = kind_of(nearest_contract) if nearest_contract else "CTR"
            for target in ids:
                annotations.append(
                    Annotation(verb, target, rel, i, src_kind, source_id=nearest_contract)
                )
    return annotations


def scan_epic_annotations(epic_dir: str | Path) -> list[Annotation]:
    """Walk the epic docs, emitting ``@cw-trace realizes`` (and other)
    annotations per file via ``emit_epic_annotations``.

    Annotations authored in the contract/invariant docs originate from a contract
    (source kind ``CTR``) — this is how a contract declares which business
    rule(s) it realizes.
    """
    root = Path(epic_dir)
    annotations: list[Annotation] = []
    if not root.exists():
        return annotations
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".md", ".json") or not path.is_file():
            continue
        if is_justification_path(root, path):
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        rel = str(path.relative_to(root))
        annotations.extend(emit_epic_annotations(rel, text))
    return annotations


SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv"}


def _file_predicate(rel: str) -> bool:
    """The scanner's EXACT file-selection rule (extension allow-list + skipped
    directories) — the same predicate ``scan_source`` applies during its own
    walk (see ``chief_wiggum.manifest``)."""
    p = Path(rel)
    if p.suffix not in SOURCE_EXTS:
        return False
    if any(part in SKIP_PARTS for part in p.parts):
        return False
    return True


def _changed_since_predicate(rel: str) -> bool:
    """Manifest predicate for ``--changed-since``: the scanner's own rule
    WIDENED with the recognized-but-unsupported extensions (#162), so a changed
    ``.php``/``.cpp`` file still reaches ``unsupported_extension_counts`` and
    triggers the coverage warning in scoped mode — scoping must never make a
    coverage gap silent. ``scan_source`` itself still filters candidates
    through ``_file_predicate``, so the extra paths never affect the
    annotation scan."""
    p = Path(rel)
    if any(part in SKIP_PARTS for part in p.parts):
        return False
    return p.suffix in SOURCE_EXTS or p.suffix in emitters.unsupported_extensions()


def unsupported_extension_counts(
    source_root: str | Path, only_files: set[str] | None = None
) -> dict[str, int]:
    """Count files with a RECOGNIZED-but-unsupported extension (#162) — one
    ``scripts/emitters`` has no emitter for at all (language-specific or
    generic tier) — among the same candidate set ``scan_source`` would walk.
    Never silent: this feeds a ``coverage`` warning in ``check()`` instead of
    the file simply disappearing from the scan with no trace."""
    root = Path(source_root)
    counts: dict[str, int] = {}
    if not root.exists():
        return counts
    candidates = sorted(only_files) if only_files is not None else walk_source_files(root)
    unsupported = emitters.unsupported_extensions()
    for rel in candidates:
        suffix = Path(rel).suffix
        if suffix not in unsupported:
            continue
        if any(part in SKIP_PARTS for part in Path(rel).parts):
            continue
        counts[suffix] = counts.get(suffix, 0) + 1
    return counts


def scan_source(source_root: str | Path, only_files: set[str] | None = None) -> list[Annotation]:
    """Walk source/test/verification files, emitting ``@cw-trace`` annotations
    per file. Public, backward-compatible entry point (code_query.py and many
    existing tests call this expecting a plain ``list[Annotation]``) — a thin
    wrapper over ``_scan_source_and_unscanned``, which additionally tracks
    files that could not be READ at all (chief-wiggum#282); ``check()`` calls
    that directly so it can surface them, never silently."""
    annotations, _unscanned = _scan_source_and_unscanned(source_root, only_files=only_files)
    return annotations


def _scan_source_and_unscanned(
    source_root: str | Path, only_files: set[str] | None = None
) -> tuple[list[Annotation], list[dict]]:
    """Language files route through the per-language emitter registry
    (``scripts/emitters`` — the gate consumes the SAME dispatch path the
    emitters expose, so a per-language emitter can never drift from what the
    gate actually scans); verification artifacts (``.rego``/``.yaml``/``.yml``
    — not a programming language in the matrix) keep the direct
    ``emit_source_annotations`` path. ``only_files`` (repo-relative paths),
    when given, restricts the walk to that set instead of the whole tree —
    used by ``--changed-since``.

    Returns ``(annotations, unscanned)``: ``unscanned`` lists
    ``{"file", "reason"}`` for every candidate that could not be READ at all
    (chief-wiggum#282) — decode failures alone can no longer land here
    (``read_text_safe`` BOM-sniffs and falls back to a lossy decode rather
    than skipping), so this is genuinely "could not open this file", never a
    silent drop.

    Per-file EMISSION is cached (#327), keyed by ``(rel, blob_sha,
    scanner_hash)`` — never the CLAIM (the orphan/uncovered/untested/dangling
    join against the defined-ID set, computed fresh in ``build_report`` over
    every annotation returned here, cached or not). ``candidates`` is ALWAYS
    the full walk (or the ``--changed-since`` set) — caching only decides
    whether a given file's annotations are RECOMPUTED or SERVED from a prior
    run, never whether the file is visited at all. The manifest that supplies
    ``blob_sha`` requires git; a non-git ``--source`` (or a path the manifest
    legitimately excludes, e.g. a gitignored-but-present file) degrades that
    file to a live scan, exactly as before this cache existed — never a
    dropped file.
    """
    root = Path(source_root)
    annotations: list[Annotation] = []
    unscanned: list[dict] = []
    if not root.exists():
        return annotations, unscanned
    if only_files is not None:
        candidates = sorted(only_files)
    else:
        # walk_source_files prunes submodules/nested git checkouts, keeping the
        # full scan's file universe identical to the manifest's (--changed-since).
        candidates = walk_source_files(root)

    manifest: dict[str, str] | None = None
    scanner_hash: str | None = None
    if not findings_cache.disabled():
        try:
            manifest = build_manifest(root)
        except ManifestError:
            manifest = None
        if manifest is not None:
            scanner_hash = _scanner_version()

    for rel in candidates:
        if not _file_predicate(rel):
            continue
        path = root / rel
        blob_sha = manifest.get(rel) if manifest is not None else None
        if blob_sha is not None and scanner_hash is not None:
            cached = findings_cache.load(str(root), "check_traceability", rel, blob_sha, scanner_hash)
            if cached is not None:
                annotations.extend(Annotation(**d) for d in cached)
                continue
        text, skip_reason = read_text_safe(path)
        if skip_reason is not None:
            unscanned.append({"file": rel, "reason": skip_reason})
            continue
        if path.suffix in VERIFICATION_EXTS:
            file_annotations = emit_source_annotations(rel, text, path.suffix)
        else:
            facts, _tier = emitters.emit(rel, text)
            file_annotations = emitters.facts_of_kind(facts, "trace_annotation")
        annotations.extend(file_annotations)
        if blob_sha is not None and scanner_hash is not None:
            findings_cache.store(
                str(root), "check_traceability", rel, blob_sha, scanner_hash,
                [a.to_dict() for a in file_annotations],
            )
    return annotations, unscanned


def _scanner_version() -> str:
    """Hash-derived ``--scanner-version``: the source of this module plus its
    ``chief_wiggum`` dependencies. No hand-bumped constant to forget
    (INV-fh-005). trace_links.py carries the suspect-link/sidecar/justification
    logic — finding-affecting, so it is a hash input (its omission was the exact
    CTR-fh-041 silent-staleness defect, caught by the #184 dep-completeness
    test). external_links.py (#213 Phase C) decides which external store
    entries count as annotations vs suspect/unresolved — finding-affecting for
    the same reason.
    @cw-trace guards CTR-fh-041"""
    here = Path(__file__).resolve()
    cw_dir = here.parent / "chief_wiggum"
    return scanner_version(
        here,
        here.parent / "artifacts.py",
        cw_dir / "trace_ids.py",
        cw_dir / "trace_emission.py",
        cw_dir / "trace_links.py",
        cw_dir / "external_links.py",
        cw_dir / "grandfather.py",
        cw_dir / "manifest.py",
        cw_dir / "hashing.py",
        cw_dir / "languages.py",
        # The per-file findings cache (#327) — finding-affecting by
        # construction: a bug in HOW a cache hit is validated or served would
        # change what a cached run reports vs a fresh scan, so any edit here
        # must invalidate every previously-cached entry, not just the ones
        # whose file content changed.
        cw_dir / "findings_cache.py",
        # Decode-defensive bulk-source read (#282) — finding-affecting: a
        # decode-policy change here changes what a file's annotations look
        # like once read, or whether it lands in `unscanned` at all.
        cw_dir / "textio.py",
        # The DATA the loader reads, not just the loader (#259): moving an
        # extension between generic_tier / unsupported_extensions changes
        # exactly which files this scanner walks, with no code change at
        # all — the CTR-fh-041 silent-staleness class, one layer out.
        here.parent.parent / "config" / "languages.json",
    )


def build_report(
    defined: dict[str, str],
    annotations: list[Annotation],
    schema: dict,
    *,
    coverage_requirements: dict[str, list[str]] | None = None,
    id_bearing_artifacts: tuple[str, ...] | list[str] = (),
    malformed_ids: tuple[dict, ...] | list[dict] = (),
) -> TraceReport:
    """Build the trace report. ``coverage_requirements`` (#169, optional) maps a
    contract ID to a list of ``source_kind`` alternatives (e.g.
    ``["test", "probe"]``) — when present, a contract is tested only by a
    ``verifies`` link whose kind is one of those alternatives ("A OR B");
    absent, any verifying kind counts (unchanged prior behavior).
    ``id_bearing_artifacts``/``malformed_ids`` (#281, both optional) are the
    outputs of ``find_id_bearing_artifacts``/``scan_malformed_ids`` — used to
    tell "nothing to measure" (inapplicable) apart from "measured and saw
    nothing despite artifacts existing" (error, a broken instrument)."""
    report = TraceReport(defined=dict(defined))
    link_types = schema.get("link_types", {})
    coverage_requirements = coverage_requirements or {}

    realized: set[str] = set()      # BR ids with an incoming realizes
    guarded: set[str] = set()       # CTR/INV with guards/ensures (code)
    verified_kinds: dict[str, set[str]] = {}  # CTR/INV -> set of verifying source_kinds

    for ann in annotations:
        # Dangling: references an ID that isn't defined.
        if ann.target not in defined:
            report.dangling.append(ann.to_dict())
            continue
        rule = link_types.get(ann.verb)
        if rule is None:
            report.invalid_links.append({**ann.to_dict(), "reason": f"unknown verb {ann.verb}"})
            continue
        # Validate source/target node types against the TIM schema.
        if ann.source_kind not in rule["from"]:
            report.invalid_links.append(
                {**ann.to_dict(), "reason": f"{ann.verb} cannot originate from {ann.source_kind}"}
            )
            continue
        if defined[ann.target] not in rule["to"]:
            report.invalid_links.append(
                {**ann.to_dict(), "reason": f"{ann.verb} cannot target {defined[ann.target]}"}
            )
            continue
        if ann.verb == "realizes":
            # Only a realizes from a *defined* contract/invariant counts; a stray
            # realizes with no declaring contract above it doesn't clear the orphan.
            if ann.source_id and ann.source_id in defined:
                realized.add(ann.target)
            else:
                report.invalid_links.append(
                    {**ann.to_dict(), "reason": "realizes has no declaring contract/invariant source"}
                )
        elif ann.verb in ("guards", "ensures"):
            guarded.add(ann.target)
        elif ann.verb == "verifies":
            verified_kinds.setdefault(ann.target, set()).add(ann.source_kind)

    contracts = [i for i, k in defined.items() if k in ("CTR", "INV")]
    business_rules = [i for i, k in defined.items() if k == "BR"]

    def _tested(cid: str) -> bool:
        kinds = verified_kinds.get(cid, set())
        required = coverage_requirements.get(cid)
        if required:
            return bool(kinds & set(required))
        return bool(kinds)

    report.orphan_business_rules = sorted(b for b in business_rules if b not in realized)
    report.uncovered_contracts = sorted(c for c in contracts if c not in guarded)
    report.untested_contracts = sorted(c for c in contracts if not _tested(c))

    if not annotations:
        report.warnings.append("no @cw-trace annotations found; reporting coverage as absent")
    report.malformed_ids = list(malformed_ids)
    report.id_bearing_artifacts_scanned = len(id_bearing_artifacts)
    if defined:
        # (a) measurement ran and saw the graph.
        report.applicability = "applicable"
    elif id_bearing_artifacts:
        # (b) #281: measurement ran and could see NOTHING despite ID-bearing
        # artifacts existing with content. This is a broken instrument, and
        # it must never render as a pass.
        report.applicability = "error"
        report.unparsed_artifacts = [
            {"file": f,
             "reason": "ID-bearing artifact present with content but ZERO parseable "
                       "stable IDs (expected KIND-SLUG-NNN, e.g. INV-order-001)"}
            for f in id_bearing_artifacts
        ]
        report.warnings.append(
            f"{len(id_bearing_artifacts)} epic artifact(s) present but ZERO stable IDs "
            "parsed — the traceability graph is EMPTY BY BREAKAGE, not by cleanliness")
    else:
        # (c) nothing exists to measure.
        report.warnings.append("no contract/invariant/BR IDs defined in epic artifacts")
        # Vacuous-pass fix (#213 Phase E, tightened by F9): with ZERO contracts
        # defined there is nothing for coverage to be true OF — coverage is
        # inapplicable even when annotations exist. Annotations without
        # definitions remain a SOUNDNESS matter: they are all dangling, and
        # soundness findings/exit codes are unchanged by this classification.
        report.applicability = "inapplicable"
    return report


def apply_justifications(
    report: TraceReport,
    justifications: dict,
    invalid: list[dict],
    *,
    today: date | None = None,
) -> None:
    """Apply JUSTIFIED waivers (#169) to an already-built ``report``, in place.

    A valid (ticket-backed, non-expired) justification for a currently
    uncovered/untested contract moves it OUT of those lists and into
    ``justified_contracts`` — satisfying coverage without a fake guard/verify
    annotation. An expired justification is reported but does NOT satisfy
    coverage. A justification referencing an ID that isn't even defined, or
    that had no gap to waive in the first place, is not silently accepted.
    """
    today = today or date.today()
    report.invalid_justifications = list(invalid)
    justified: list[dict] = []
    expired: list[dict] = []
    for cid in sorted(justifications):
        j = justifications[cid]
        if cid not in report.defined:
            report.invalid_justifications.append(
                {"source": j.source, "reason": f"references undefined id {cid}"}
            )
            continue
        if j.is_expired(today):
            expired.append(j.to_dict())
            continue
        moved = False
        if cid in report.uncovered_contracts:
            report.uncovered_contracts.remove(cid)
            moved = True
        if cid in report.untested_contracts:
            report.untested_contracts.remove(cid)
            moved = True
        if moved:
            justified.append(j.to_dict())
    report.justified_contracts = justified
    report.expired_justifications = expired


def apply_grandfathers(
    report: TraceReport,
    entries: dict[str, dict],
    *,
    today: date | None = None,
) -> None:
    """Apply adoption-grandfather waivers (#215 F5) to a built report, in place.

    ``entries`` is the ``id -> entry`` map from
    ``<meta root>/adoption/grandfathered.json`` (``chief_wiggum.grandfather``).
    Keys for this gate: ``check_traceability:uncovered:<ID>`` /
    ``check_traceability:untested:<ID>``. Mirrors ``apply_justifications``: a
    NON-EXPIRED entry moves the gap out of uncovered/untested into
    ``grandfathered_contracts``; an EXPIRED entry does NOT waive — the gap
    stays blocking and is listed in ``expired_grandfathers`` for labeling.
    """
    today = today or date.today()
    for gap, gaps in (("uncovered", report.uncovered_contracts),
                      ("untested", report.untested_contracts)):
        for cid in list(gaps):
            entry = entries.get(f"check_traceability:{gap}:{cid}")
            if entry is None:
                continue
            record = {"id": cid, "gap": gap, "expiry": entry.get("expiry"),
                      "owner": entry.get("owner"), "reason": entry.get("reason")}
            if cw_grandfather.is_expired(entry, today):
                report.expired_grandfathers.append(record)  # stays blocking
            else:
                gaps.remove(cid)
                report.grandfathered_contracts.append(record)


def check(
    epic_dir: str | Path,
    source_root: str | Path | None = None,
    *,
    schema: dict | None = None,
    changed_since: str | None = None,
    links_path: str | Path | None = None,
    external_links_path: str | Path | None = None,
    grandfather_path: str | Path | None = None,
    today: date | None = None,
) -> TraceReport:
    """Build the trace report. ``links_path`` (#169, optional), when given, is
    the ``docs/quality/trace-links.json`` sidecar to compare current contract
    definition hashes against for suspect-link detection — omitted, no sidecar
    is read and ``suspect_links`` stays empty (nothing to compare against yet,
    e.g. the very first validation). ``external_links_path`` (#213 Phase C,
    optional) is the symbol-anchored external link store: verified-ok entries
    join the annotation set for coverage math; suspect entries are surfaced
    (never satisfying coverage); unresolved entries become warnings.
    ``grandfather_path`` (#215 F5, optional) is the adoption grandfather file
    (``<meta root>/adoption/grandfathered.json``) — non-expired entries waive
    matching coverage gaps into ``grandfathered_contracts``, expired entries
    block again (see ``apply_grandfathers``). ``today`` (optional) overrides
    the clock used for justification/grandfather-expiry checks; defaults to
    the real today."""
    schema = schema or load_schema()
    defined = extract_defined_ids(epic_dir)
    id_bearing = find_id_bearing_artifacts(epic_dir)
    malformed = scan_malformed_ids(epic_dir)
    coverage_requirements = extract_coverage_requirements(epic_dir)
    # Contract->BR realizes links live in the epic docs; code/test links in source.
    annotations = scan_epic_annotations(epic_dir)
    unsupported: dict[str, int] = {}
    unscanned: list[dict] = []
    if source_root:
        only_files = None
        if changed_since:
            # Ticket-scoped speed-up ONLY — never used by /close-epic's coverage
            # gate, which must see the whole repo to be authoritative. The
            # predicate is widened with unsupported extensions so a changed
            # .php/.cpp still triggers the coverage warning (scan_source
            # filters back down through _file_predicate itself).
            only_files = changed_paths(source_root, changed_since, predicate=_changed_since_predicate)
        source_annotations, unscanned = _scan_source_and_unscanned(source_root, only_files=only_files)
        annotations += source_annotations
        unsupported = unsupported_extension_counts(source_root, only_files=only_files)
    external = None
    if external_links_path is not None:
        # Re-anchor every stored link against the CURRENT source. Only ok
        # entries join the annotation set — they then face the exact same
        # dangling/schema validation and coverage joins as in-source
        # annotations (an external link to an undefined ID is dangling, an
        # external `verifies` from a code-kind file obeys coverage_requires).
        external = external_links.verify_links(
            external_links_path, Path(source_root) if source_root else Path(".")
        )
        for entry in external["ok"]:
            source_kind = classify_source_kind(entry["file"], Path(entry["file"]).suffix)
            for cid in entry["ids"]:
                annotations.append(Annotation(
                    entry["verb"], canonical_id(cid), entry["file"],
                    entry.get("line", 0), source_kind,
                ))
    report = build_report(
        defined, annotations, schema, coverage_requirements=coverage_requirements,
        id_bearing_artifacts=id_bearing, malformed_ids=malformed,
    )
    report.unscanned = unscanned
    if unscanned:
        # Never silent (#282): a file the scanner could not even read is
        # visible with its path, same treatment as the unsupported-extension
        # warning below — report-only, does not affect soundness_ok/coverage_ok.
        report.warnings.append(
            f"{len(unscanned)} file(s) could not be read during the scan (unscanned) — "
            "see the Unscanned files section"
        )
    if unsupported:
        # Coverage metadata (#162): a recognized-but-unsupported-language file is
        # NEVER silently dropped — surfaced as an explicit warning, same as any
        # other coverage gap this checker reports.
        total = sum(unsupported.values())
        detail = ", ".join(f"{ext} ({n})" for ext, n in sorted(unsupported.items()))
        report.warnings.append(
            f"{total} file(s) skipped: no emitter coverage for recognized-but-unsupported "
            f"extension(s) {detail} — see config/languages.json"
        )

    if links_path is not None:
        current_hashes = hash_epic_definitions(Path(epic_dir))
        sidecar = load_sidecar(links_path)
        report.suspect_links = find_suspect_links(sidecar, current_hashes)
        report.suspect_contracts = sorted({link["target"] for link in report.suspect_links})

    if external is not None:
        # Suspect external links (anchored-symbol hash drift) are surfaced with
        # the definition-drift suspects — AFTER the links_path block, which
        # (re)assigns report.suspect_links. They never satisfy coverage: their
        # entries were excluded from the annotation merge above.
        for entry in external["suspect"]:
            for cid in entry["ids"]:
                report.suspect_links.append({
                    "verb": entry["verb"],
                    "target": canonical_id(cid),
                    "file": entry["file"],
                    "line": entry.get("line", 0),
                    "symbol": entry.get("symbol"),
                    "source": "external-link-store",
                    "reason": "anchored symbol changed since this link was recorded",
                })
        report.suspect_contracts = sorted(
            set(report.suspect_contracts)
            | {canonical_id(c) for e in external["suspect"] for c in e["ids"]}
        )
        for entry in external["unresolved"]:
            report.warnings.append(
                f"external link {entry.get('file')}::{entry.get('symbol')} "
                f"({entry.get('verb')}) unresolved: {entry.get('reason')}"
            )
        # #313 item 3: a store with entries where NOT ONE anchors (ok/suspect
        # both empty) is always a defect — the language tier is missing, or
        # the recorded paths/symbols are wrong. Left as a warning, this looks
        # identical to an honestly empty store (both report zero coverage
        # contribution); the #289 outcome vocabulary this module already
        # implements (applicability: inapplicable | error) is the same
        # absence-reads-as-normal shape, so it is reused rather than inventing
        # a parallel signal. This OVERRIDES applicable/inapplicable — a broken
        # anchoring mechanism is a defect regardless of what else in the epic
        # happens to already be measured.
        total_external = sum(len(v) for v in external.values())
        if external_links.store_applicability(external) == "error":
            report.applicability = "error"
            anchored = len(external["ok"]) + len(external["suspect"])
            report.external_link_store_error = (
                f"external link store has {total_external} entries and NONE "
                f"anchored (anchored {anchored} of {total_external}) — a broken "
                "instrument: the language tier is missing or the recorded "
                "paths/symbols are wrong (chief-wiggum#313)"
            )
            report.warnings.append(report.external_link_store_error)

    justifications, invalid_justifications = load_justifications(epic_dir)
    apply_justifications(report, justifications, invalid_justifications, today=today)

    if grandfather_path is not None:
        gf_entries, gf_warning = cw_grandfather.load_entries(grandfather_path)
        if gf_warning:
            report.warnings.append(gf_warning)
        if gf_entries:
            apply_grandfathers(report, gf_entries, today=today)
    return report


def write_links_sidecar(
    epic_dir: str | Path,
    source_root: str | Path | None,
    path: str | Path,
) -> dict:
    """Write the ``docs/quality/trace-links.json`` sidecar (#169) from the
    CURRENT scan: every ``@cw-trace`` link's definition hash, at the moment
    this is called. Not hand-maintained — called by ``/architect``/
    ``/close-epic`` only once their respective gate has passed (see ``main``'s
    ``--write-links``), so a stale/failing state never gets recorded as
    validated.

    Always a FULL source scan, by construction: the sidecar is the global
    record of validated links, and rewriting it from a ``--changed-since``
    partial scan would silently drop every validated link in unchanged files
    (they'd then never be able to go suspect). ``main`` rejects the
    ``--write-links --changed-since`` combination as a usage error (PR #181
    review)."""
    annotations = scan_epic_annotations(epic_dir)
    if source_root:
        annotations += scan_source(source_root)
    current_hashes = hash_epic_definitions(Path(epic_dir))
    body = build_sidecar(annotations, current_hashes, scanner_version=_scanner_version())
    # Version binding (#213): stamp the target HEAD the sidecar was computed
    # against. ADDITIVE key — load_sidecar/find_suspect_links tolerate its
    # absence, so pre-existing sidecars keep loading. Suspect semantics remain
    # hash re-anchoring (definition-hash compare), which is strictly stronger
    # than a sha compare; the sha is provenance, not the verify mechanism.
    body["target_sha"] = artifacts.head_sha(
        Path(source_root) if source_root else Path(epic_dir)
    )
    write_sidecar(path, body)
    return body


def render_markdown(report: TraceReport) -> str:
    lines = ["# Traceability Audit", "", f"Defined IDs: {report.counts['defined']}", ""]
    lines.append(f"- Soundness (orphans/dangling/invalid): {'OK' if report.soundness_ok else 'FINDINGS'}")
    lines.append(f"- Coverage (uncovered/untested): {'OK' if report.coverage_ok else 'FINDINGS'}")
    lines.append(
        f"- Measured: {report.id_bearing_artifacts_scanned} ID-bearing artifact(s), "
        f"{report.counts['defined']} stable ID(s) parsed"
    )
    if report.applicability == "inapplicable":
        lines.append(
            "- Applicability: INAPPLICABLE — no contracts defined, so there is "
            "nothing for coverage to hold over (inapplicable, not passing); any "
            "annotations found are dangling and remain soundness findings"
        )
    elif report.applicability == "error" and report.external_link_store_error:
        lines.append(f"- Applicability: ERROR — {report.external_link_store_error}.")
    elif report.applicability == "error":
        lines.append(
            f"- Applicability: ERROR — {report.id_bearing_artifacts_scanned} epic artifact(s) "
            "present but ZERO stable IDs parsed. The measurement is BROKEN, not clean: "
            "this is a FAILURE, not a pass (chief-wiggum#281)."
        )
    for label, items in (
        ("Orphan business rules", report.orphan_business_rules),
        ("Uncovered contracts (no code guard)", report.uncovered_contracts),
        ("Untested contracts (no test)", report.untested_contracts),
    ):
        if items:
            lines += ["", f"## {label}", ""] + [f"- {i}" for i in items]
    if report.dangling:
        lines += ["", "## Dangling annotations", ""]
        lines += [f"- {d['file']}:{d['line']} {d['verb']} {d['target']} (undefined)" for d in report.dangling]
    if report.invalid_links:
        lines += ["", "## Invalid links", ""]
        lines += [f"- {d['file']}:{d['line']} {d['reason']}" for d in report.invalid_links]
    if report.unparsed_artifacts:
        lines += ["", "## Unparsed artifacts (present, zero stable IDs)", ""]
        lines += [f"- {u['file']}: {u['reason']}" for u in report.unparsed_artifacts]
    if report.unscanned:
        lines += [
            "",
            "## Unscanned files (could not be read — never blocking on their own)",
            "",
        ]
        lines += [f"- {u['file']}: {u['reason']}" for u in report.unscanned]
    if report.malformed_ids:
        lines += ["", "## Malformed stable IDs (near-miss declarations)", ""]
        lines += [f"- {m['file']}:{m['line']} {m['token']} → expected {m['expected']}"
                  for m in report.malformed_ids]
    if report.suspect_links:
        lines += ["", "## Suspect links (definition changed since verified)", ""]
        lines += [
            f"- {d['file']}:{d['line']} {d['verb']} {d['target']} "
            f"({d.get('reason', 'definition hash changed since this link was validated')})"
            for d in report.suspect_links
        ]
    if report.grandfathered_contracts:
        lines += ["", "## Grandfathered (pre-adoption baseline — waived, non-blocking)", ""]
        lines += [
            f"- {g['id']} ({g['gap']}) — {g.get('reason') or 'pre-adoption baseline'} "
            f"(owner {g.get('owner') or '?'}, expires {g.get('expiry') or '?'})"
            for g in report.grandfathered_contracts
        ]
    if report.expired_grandfathers:
        lines += ["", "## EXPIRED grandfathers (no longer waive — these gaps block again)", ""]
        lines += [
            f"- {g['id']} ({g['gap']}) — EXPIRED grandfather (expiry {g.get('expiry') or '?'}); "
            "re-triage or remediate (docs/adopt.md)"
            for g in report.expired_grandfathers
        ]
    if report.justified_contracts:
        lines += ["", "## Justified (waived, ticket-tracked)", ""]
        lines += [
            f"- {j['id']} — {j['reason']} (ticket {j['ticket']}, approver {j['approver']}, "
            f"expires {j['expiry']})"
            for j in report.justified_contracts
        ]
    if report.expired_justifications:
        lines += ["", "## Expired justifications (no longer satisfy coverage)", ""]
        lines += [
            f"- {j['id']} — expired {j['expiry']} (ticket {j['ticket']})"
            for j in report.expired_justifications
        ]
    if report.invalid_justifications:
        lines += ["", "## Invalid justifications", ""]
        lines += [f"- {d['source']}: {d['reason']}" for d in report.invalid_justifications]
    if report.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in report.warnings]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Traceability graph checker (TIM/DbC)")
    parser.add_argument(
        "epic_dir", nargs="?", default=None,
        help="docs/epics/<slug> directory with contract/invariant IDs; not required with --scanner-version",
    )
    parser.add_argument("--source", help="Repo root to scan for @cw-trace annotations")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--gate", choices=["soundness", "coverage"], help="Fail (exit 1) on this gate's findings")
    parser.add_argument(
        "--changed-since",
        metavar="REF",
        help="Scope the --source scan to files changed since REF (via git diff + the "
        "content-addressed manifest) instead of the whole tree. Ticket-scoped speed-up "
        "ONLY — /close-epic's coverage gate NEVER uses this; whole-repo remains the default.",
    )
    parser.add_argument(
        "--scanner-version",
        action="store_true",
        help="Print the hash-derived scanner version (source hash of this module + its "
        "chief_wiggum deps) and exit",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable the per-file findings cache (#327) for this run — every candidate "
        "file is re-emitted from scratch, even on an unchanged git blob. For the dual-run "
        "zero-diff validation check; not needed for correctness in normal use (a stale "
        "cache entry never serves — see chief_wiggum/findings_cache.py).",
    )
    parser.add_argument(
        "--links",
        metavar="PATH",
        help="Path to the trace-links.json sidecar (#169) used for suspect-link detection. "
        f"Defaults to <--source or cwd>/{SIDECAR_RELPATH}.",
    )
    parser.add_argument(
        "--external-links",
        metavar="PATH",
        help="Path to the symbol-anchored external link store (#213 Phase C). Defaults to "
        f"<meta quality dir>/{external_links.STORE_NAME} when the target's elected footprint "
        "mode is sidecar; unused otherwise. Verified-ok entries count as annotations; "
        "suspect entries are surfaced and never satisfy coverage.",
    )
    parser.add_argument(
        "--grandfather",
        metavar="PATH",
        help="Path to the adoption grandfather file (#215; adopt.py writes "
        f"<meta root>/{cw_grandfather.GRANDFATHER_RELPATH}). Defaults to that resolver "
        "location for the --source target. Keys for this gate: "
        "check_traceability:uncovered:<ID> / check_traceability:untested:<ID>. "
        "Non-expired entries waive matching gaps (reported under Grandfathered); "
        "EXPIRED entries block again.",
    )
    parser.add_argument(
        "--write-links",
        action="store_true",
        help="(Re)write the trace-links.json sidecar from a FULL scan's current link/definition "
        "hashes — but ONLY when the requested --gate passes (or no --gate was given). A failing "
        "gate leaves the sidecar untouched, so a stale/broken state is never recorded as "
        "validated. Incompatible with --changed-since (a partial scan would drop validated "
        "links for unchanged files). Not hand-maintained; see docs/traceability.md.",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    if args.scanner_version:
        print(_scanner_version())
        return 0

    if args.no_cache:
        os.environ[findings_cache.NO_CACHE_ENV] = "1"

    if not args.epic_dir:
        print("Error: epic_dir is required unless --scanner-version is given", file=sys.stderr)
        return 2

    if args.write_links and args.changed_since:
        print(
            "Error: --write-links cannot be combined with --changed-since — the sidecar is the "
            "global record of validated links and must be written from a FULL scan; a partial "
            "scan would silently drop validated links for unchanged files",
            file=sys.stderr,
        )
        return 2

    try:
        schema = load_schema(Path(args.schema))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error: cannot load TIM schema: {exc}", file=sys.stderr)
        return 2

    # A missing epic dir is a usage error (a typo), not graceful absence.
    if not Path(args.epic_dir).exists():
        print(f"Error: epic dir not found: {args.epic_dir}", file=sys.stderr)
        return 2

    source_root = Path(args.source or ".")
    resolver = artifacts.Resolver.resolve(source_root)
    if args.links:
        links_path = Path(args.links)
    else:
        # Default: the quality dir of the source repo's meta root (#213) —
        # byte-identical to <--source or cwd>/SIDECAR_RELPATH in embedded mode.
        links_path = resolver.quality_dir() / Path(SIDECAR_RELPATH).name

    if args.external_links:
        external_links_path = Path(args.external_links)
    elif resolver.mode == "sidecar":
        # Sidecar mode replaces in-source annotations with the external store
        # (#213 Phase C) — read it by default, beside trace-links.json.
        external_links_path = resolver.quality_dir() / external_links.STORE_NAME
    else:
        external_links_path = None

    if args.grandfather:
        grandfather_path = Path(args.grandfather)
    else:
        # Default: the --source target's resolved adoption grandfather file
        # (#215). Missing file degrades to graceful absence inside check().
        grandfather_path = resolver.meta_root / cw_grandfather.GRANDFATHER_RELPATH

    try:
        report = check(
            args.epic_dir, args.source, schema=schema, changed_since=args.changed_since,
            links_path=links_path, external_links_path=external_links_path,
            grandfather_path=grandfather_path,
        )
    except ManifestError as exc:
        # Bad --changed-since ref, non-git --source, missing HEAD, no git binary:
        # a usage error, reported concisely — never a traceback.
        print(f"Error: --changed-since manifest failed: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_markdown(report))

    if args.write_links:
        gate_passed = (
            # #281: never record a sidecar of "validated" links from a scan
            # that saw nothing — a broken instrument is never a validated
            # state, --gate or not.
            report.applicability != "error"
            and (args.gate is None
                 or (args.gate == "soundness" and report.soundness_ok)
                 or (args.gate == "coverage" and report.coverage_ok))
        )
        if gate_passed:
            write_links_sidecar(args.epic_dir, args.source, links_path)
        else:
            print(
                f"check_traceability: --write-links skipped — --gate {args.gate} did not pass "
                "(sidecar left untouched)",
                file=sys.stderr,
            )

    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from factory_log import emit_gate
        caught = (len(report.orphan_business_rules) + len(report.uncovered_contracts)
                  + len(report.untested_contracts) + len(report.dangling)
                  + len(report.invalid_links) + len(report.malformed_ids)
                  + len(report.unparsed_artifacts))
        emit_gate("check_traceability", "fail" if caught else "pass",
                  caught=caught, repo=_repo_from_epic_dir(args.epic_dir))
    except Exception:
        pass

    # Vacuous-pass fix (#213 Phase E, tightened by F9): a gate run with ZERO
    # contracts defined still exits per the unchanged soundness/coverage
    # semantics below — changing exit codes would break existing pipelines —
    # but it must never be a silent identical green: the banner prints and the
    # JSON carries "applicability": "inapplicable" so a wrapper skill can
    # distinguish. Annotations without definitions are dangling (soundness),
    # so --gate soundness still fails on them; coverage alone is the vacuous
    # surface.
    if args.gate and report.applicability == "inapplicable":
        print(
            "check_traceability: INAPPLICABLE — no contracts defined; "
            f"--gate {args.gate} has nothing to hold over "
            "(inapplicable, not passing)",
            file=sys.stderr,
        )

    # #281: a BROKEN measurement fails EITHER gate. "The scanner parsed
    # nothing" is not a clean soundness result and not a clean coverage
    # result — it is the absence of a result. Exit stays in the uniform 0/1/2
    # contract every gate shares (docs/gate-rollout.md); the state is carried
    # by the banner and by "applicability": "error" / "outcome": "error" in
    # the JSON.
    if args.gate and report.applicability == "error" and report.external_link_store_error:
        print(
            f"check_traceability: ERROR — {report.external_link_store_error}; "
            f"--gate {args.gate} measured nothing usable from the external store "
            "(FAILURE, not a pass).",
            file=sys.stderr,
        )
        return 1
    if args.gate and report.applicability == "error":
        print(
            "check_traceability: ERROR — "
            f"{report.id_bearing_artifacts_scanned} epic artifact(s) present but ZERO stable "
            f"IDs parsed; --gate {args.gate} measured nothing (FAILURE, not a pass). "
            "Stable IDs are KIND-SLUG-NNN, e.g. INV-order-001 (chief-wiggum#281)",
            file=sys.stderr,
        )
        return 1

    if args.gate == "soundness" and not report.soundness_ok:
        return 1
    if args.gate == "coverage" and not report.coverage_ok:
        return 1
    return 0


def _repo_from_epic_dir(epic_dir: str) -> str:
    """Best-effort repo name from an epic dir (<repo>/docs/epics/<slug>)."""
    import os
    parts = os.path.abspath(epic_dir).split(os.sep)
    if "docs" in parts and parts.index("docs") > 0:
        return parts[parts.index("docs") - 1]
    return os.path.basename(os.path.abspath(epic_dir))


if __name__ == "__main__":
    sys.exit(main())
