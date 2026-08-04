#!/usr/bin/env python3
"""Single-writer / mutator-inventory checker.

Some invariants declare a **single write path** / **single source of truth** on a
specific field or state: exactly one sanctioned code path may mutate it. Prose and
the existing traceability/ratchet checks cannot catch a *second* writer — they
verify contract↔code↔test *links* and the pass-set, not "who writes this field".

Real incident this catches: an epic declared ``INV-BIL-001`` ("single atomic
Stripe→plan write") and the reconcile feature honoured it — but a pre-existing
admin control (``ChangePlan``) was a SECOND writer of the same
``provider.stripe_plan`` field, and nothing flagged it.

This checker:

1. Parses an epic's artifacts (structured ``state-machines.json`` invariants AND
   prose ``invariants.md``) for invariants carrying single-write-path metadata:
   the controlled field(s) and the sanctioned writer(s).
2. Scans the target repo for ALL writers of each controlled field — Go/general
   assignments (``x.Plan =``), struct-literal sets (``Plan: v``), and Mongo bson
   mutations (``$set``/``{Key: "plan"``, ``"plan":`` in an update) and SQL
   ``UPDATE ... SET plan``.
3. Flags any writer NOT in the sanctioned set as a violation.

Convention (mirrors ``@cw-trace``; see ``docs/single-writer.md``):

- **Structured** — a ``state-machines.json`` ``invariant`` object gains two
  optional arrays::

      { "id": "INV-bil-001",
        "description": "single atomic Stripe→plan write",
        "controls_field": ["provider.plan", "provider.stripe_plan"],
        "sanctioned_writers": ["ReconcileStripe", "internal/billing/reconcile.go"],
        "sink": "db" }

- **Prose** — an ``invariants.md`` invariant gains a namespaced tag on/near its
  ``**INV-...**`` line::

      **INV-bil-001**: single atomic Stripe→plan write
      <!-- @cw-writes INV-bil-001 controls_field=provider.plan,provider.stripe_plan
           sanctioned_writers=ReconcileStripe,internal/billing/reconcile.go sink=db -->

A ``sanctioned_writer`` is either a **symbol** (a function/method name, matched
against the nearest enclosing ``func`` above a write) or a **file path** (matched
as a suffix of the writer's file). A field path ``provider.stripe_plan`` matches
writes to its leaf token (``stripe_plan`` / ``StripePlan``) — see ``field_tokens``.

``sink=db`` (a.k.a. ``write_kind=persistence`` / structured ``"sink": "db"``) narrows
matching to **persistence sinks only** — DB updates (``$set``/``UpdateOne``, SQL
``UPDATE ... SET``) — ignoring in-memory Go assignments, struct/map literals, reads,
response DTOs, and TS interface fields. Use it for a single-write-path invariant on a
*persisted* field (the question is who writes the row, not who assigns a struct). For a
purely in-memory single-owner field, omit it and every assignment is considered.
``--exclude <glob>`` (repeatable) skips whole subtrees (e.g. a TS frontend that never
persists the field) as belt-and-suspenders on a polyglot repo.

``--scope <path>|auto`` (chief-wiggum#213) is the DOMAIN-AUTHORITY split, complementing
``--exclude``: exclusion removes files from detection entirely; scope never narrows
detection — the scan stays repo-wide — it classifies findings. Writers inside the scope
are **in-domain** (blocking-eligible, exactly today's gate semantics); writers outside
are **boundary** findings (``boundary: true`` in JSON, a clearly-labeled report section,
NEVER the exit code — file them to the owning team). ``auto`` reads scope.json from the
``--source`` target's resolved meta root (``scripts/artifacts.py``). An epic with no
single-write-path invariants reports ``"applicability": "inapplicable"`` — exit codes
unchanged, but the pass is visibly vacuous, never a silent identical green.

Known limitations (regex, not a type checker): even with ``sink=db`` two residual false
positives remain because they need collection/type awareness the scanner doesn't have —
(1) a same-named field written to a DIFFERENT collection in a mutation context (e.g. an
audit-log ``bson.M{"plan": …}``), and (2) a FILTER clause with a literal value
(``bson.M{"plan": ""}`` — a ``$``-operator filter IS skipped, a bare-literal one is not).
Mitigate with precise ``sanctioned_writers`` and ``--exclude``. Because of this, wire a
new single-write-path invariant on a common field as **report-only first** (no ``--gate``)
and confirm the finding set is clean before making it a ``coverage`` blocker.

**Adoption grandfathers (#215 F5)**: ``adopt.py grandfather`` records pre-adoption
baseline violations in ``<meta root>/adoption/grandfathered.json``, keyed for this
gate as ``check_single_writer:<INV-id>:<field>:<file>`` (canonical invariant id,
controlled-field path, repo-relative file — see ``chief_wiggum.grandfather``).
Mirroring the JUSTIFIED-waiver mechanics: an in-domain violation matching a
NON-EXPIRED entry is reported under ``grandfathered`` (never silently dropped) and
does NOT count toward the ``--gate coverage`` exit; an EXPIRED entry does NOT waive
— the violation blocks again, labeled "EXPIRED grandfather". ``--grandfather PATH``
overrides the resolver default.

Backward-compatible: invariants without the metadata are skipped (degrade
gracefully), exactly like ``check_traceability.py`` when IDs are absent.

Gates (mirrors ``check_traceability.py``):
    --gate soundness  -> /architect: fail on malformed metadata; surface writers.
    --gate coverage   -> /close-epic: hard-fail on any unsanctioned writer.

Internally, scanning is split into per-file EMISSION (``emit_write_sites``: every
field-agnostic candidate write site) and query-time CLAIM (``match_writers``: is
this site's token one of THIS invariant's controlled fields?) — see
``docs/single-writer.md``. ``--changed-since <ref>`` scopes ``--source`` to files
changed since ``ref`` (never used by /close-epic's coverage gate, which must see
the whole repo). ``--scanner-version`` prints a hash of this module's source plus
its ``chief_wiggum`` deps.

Exit codes: 0 = ok, 1 = gate violation, 2 = usage error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Domain-scope authority split (chief-wiggum#213 Phase D): --scope classifies
# repo-wide findings as in-domain (blocking-eligible) vs boundary (visible,
# never blocking). The scope document + matching rule live in artifacts.py —
# the same resolver/scope machinery every other meta surface routes through.
import artifacts  # noqa: E402

# The per-language emitter registry (#162): language-specific emitter -> generic
# regex tier -> skip-with-warning. Used by scan_writers/unsupported_extension_counts
# below to surface files with NO emitter coverage instead of dropping them silently.
import emitters  # noqa: E402

# Per-file emission cache (#327): every gate-scanned file's write sites are
# memoized keyed by (rel, blob_sha, scanner_hash) — see the module docstring
# for why both halves of the key are load-bearing.
from chief_wiggum import findings_cache  # noqa: E402

# Adoption-grandfather waivers (#215 F5) — the shared reader both blocking
# gates use; key grammar + expiry posture documented there.
from chief_wiggum import grandfather as cw_grandfather  # noqa: E402

# The declared language support matrix (#162) — SOURCE_EXTS below is derived
# from it (tier-1 + generic-tier extensions), and the emitter fallback chain
# (scripts/emitters/) reports files with no coverage at all as an explicit
# warning rather than a silent skip. See config/languages.json + docs/languages.md.
from chief_wiggum import languages as cw_languages  # noqa: E402

# The @cw-writes tag grammar is shared (#170: a third @cw-* tag, @cw-emits,
# joins it) — see chief_wiggum/annotations.py. Re-exported under these names
# for backward compatibility with any existing `check_single_writer.WRITES_TAG_RE`
# references.
from chief_wiggum.annotations import ATTR_RE, WRITES_TAG_RE  # noqa: E402, F401

# Shared with check_traceability.py: the hash-derived --scanner-version and the
# git-native manifest helper behind --changed-since (#160). walk_source_files
# prunes submodules/nested git checkouts from the FULL scan so both scan modes
# agree on the file universe (the manifest never surfaces submodule blobs).
from chief_wiggum.hashing import scanner_version  # noqa: E402
from chief_wiggum.manifest import (  # noqa: E402
    ManifestError,
    build_manifest,
    changed_paths,
    walk_source_files,
)

# Decode-defensive bulk-source read (#282): a bare path.read_text() crashes the
# ENTIRE gate with UnicodeDecodeError on a UTF-16 (or otherwise non-UTF-8)
# file — no verdict, and the traceback names the reader, not the file. Shared
# with check_traceability.py's scan_source so the two scanners can't drift on
# decode policy. See chief_wiggum/textio.py.
from chief_wiggum.textio import read_text_safe  # noqa: E402

# The write-site emission family (regexes, WriteSite, emit_write_sites) moved to
# chief_wiggum.write_emission (#162) so scripts/emitters/*.py can sit BEHIND the
# same per-file emission logic this checker uses — re-exported here unchanged
# so every existing `check_single_writer.X` reference keeps working (golden
# parity; see tests/test_single_writer_golden.py).
from chief_wiggum.write_emission import (  # noqa: E402, F401
    ASSIGN_RE,
    FILTER_OPERATOR_RE,
    GO_FUNC_RE,
    KIND_ASSIGN,
    KIND_QUOTED,
    KIND_SQL,
    KIND_STRUCT,
    MUTATION_CONTEXT_RE,
    PY_FUNC_RE,
    QUOTED_RE,
    SQL_FIELD_RE,
    SQL_SET_KEYWORD_RE,
    STRUCT_RE,
    TS_FUNC_RE,
    WriteSite,
    _enclosing_symbol,
    _is_test_path,
    _strip_line_comment,
    emit_write_sites,
)

# Same INV- shape as check_traceability.py (case-insensitive slug segment).
INV_ID_RE = re.compile(r"\bINV-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3}(?![A-Za-z0-9-])", re.IGNORECASE)

# Prose invariant declaration (bold label), same as check_traceability's DEFINE_RE
# but scoped to INV- and capturing the description for reporting.
INV_DEFINE_RE = re.compile(r"\*\*\s*(INV-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3})\s*\*\*\s*:?\s*(.*)")

# The set of extensions this checker scans — every tier-1 (Go/Python/TypeScript)
# and generic-tier (Java/Ruby/Rust today) extension declared in
# config/languages.json. Backward-compatible: identical to the pre-#162
# hardcoded set. See chief_wiggum.languages + docs/languages.md.
SOURCE_EXTS = cw_languages.all_known_extensions()
SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", "vendor", "dist", "build"}

# The epic artifacts this gate DECLARES as its metadata sources (module
# docstring, "Contract"). A parse failure in one of these is a broken
# instrument (#289); a parse failure anywhere else in the epic dir is another
# gate's business.
DECLARED_METADATA_SOURCES = {"state-machines.json", "invariants.md"}


def canonical_id(node_id: str) -> str:
    kind, _, rest = node_id.partition("-")
    return f"{kind.upper()}-{rest.lower()}"


def _excluded(rel: str, patterns: list[str]) -> bool:
    """True if repo-relative path ``rel`` matches any ``--exclude`` pattern. A bare
    token (``ui``) matches that directory and everything under it; a glob
    (``ui/*``, ``**/*.gen.ts``) matches via fnmatch. Belt-and-suspenders for polyglot
    repos where a whole subtree (e.g. the TS frontend) never persists the field."""
    for g in patterns:
        g = g.rstrip("/")
        if not g:
            continue
        if rel == g or rel.startswith(g + "/"):
            return True
        if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g + "/*"):
            return True
    return False


@dataclass
class SingleWriterInvariant:
    """An invariant that declares a single write path on one or more fields."""

    id: str
    description: str
    controls_field: list[str]
    sanctioned_writers: list[str]
    source: str  # where the metadata was declared (file:line or file)
    # When True (metadata `sink=db` / `write_kind=persistence`), only PERSISTENCE
    # sinks count as writers — DB updates (`$set`/`UpdateOne`, SQL `UPDATE ... SET`) —
    # not in-memory Go assignments or struct/map literals. This is the right lens for a
    # single-write-path invariant on a *persisted* field: the question is who writes the
    # ROW, not who assigns the in-memory struct. Skips the false positives (reads,
    # DTO/response copies, other structs' same-named fields, TS interface fields).
    persistence_only: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def field_tokens(self) -> set[str]:
        """Leaf identifiers that a write to a controlled field would use.

        ``provider.stripe_plan`` -> {``stripe_plan``, ``stripeplan``, ``StripePlan``}.
        We compare case-insensitively on the token, plus a camelCase form, so Go
        (``StripePlan``), snake bson (``stripe_plan``), and JSON tags all match.
        """
        tokens: set[str] = set()
        for fpath in self.controls_field:
            leaf = fpath.split(".")[-1].strip()
            if not leaf:
                continue
            tokens.add(leaf.lower())
            # snake_case -> CamelCase (stripe_plan -> stripeplan for compaction)
            tokens.add(leaf.replace("_", "").lower())
        return tokens


@dataclass
class Writer:
    invariant_id: str
    field: str
    file: str
    line: int
    text: str
    symbol: str | None  # nearest enclosing function/method, if resolvable
    sanctioned: bool
    is_test: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SingleWriterReport:
    invariants: list[dict] = field(default_factory=list)
    writers: list[dict] = field(default_factory=list)      # in-domain production writers
    violations: list[dict] = field(default_factory=list)   # unsanctioned IN-DOMAIN writers
    # Boundary findings (chief-wiggum#213 Phase D): writers of a controlled field
    # found OUTSIDE the domain scope. Detection scans repo-wide; authority stops
    # at the boundary — these are reported (each entry carries `boundary: true`),
    # optionally filed to the owning team, and NEVER affect the exit code. The
    # motivating incident: an out-of-domain writer of our controlled field must
    # stay VISIBLE without blocking our merges. Empty unless --scope was given.
    boundary: list[dict] = field(default_factory=list)
    # Adoption-grandfathered violations (#215 F5): in-domain violations whose
    # key matches a NON-EXPIRED grandfather entry move here — reported, never
    # blocking. An EXPIRED entry does NOT waive: its violation STAYS in
    # `violations` (blocks again) carrying grandfather_expired/expiry labels.
    grandfathered: list[dict] = field(default_factory=list)
    malformed: list[dict] = field(default_factory=list)     # bad metadata (soundness)
    # Files that could not be READ at all during the scan (chief-wiggum#282) —
    # permissions, a race where the file vanished mid-walk, a broken symlink,
    # ... A UTF-16/non-UTF-8 file is NOT here: read_text_safe BOM-sniffs and
    # falls back to a lossy decode rather than skipping, so it stays fully
    # scanned. Report-only by design (binding decision, #282): an unscanned
    # file is visible (with its path) but never flips soundness_ok/coverage_ok
    # by itself — a repo full of binary-ish files must not become unusable.
    unscanned: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # #289: a DECLARED metadata source (state-machines.json / invariants.md)
    # that exists with content and could not be read or parsed at all. Distinct
    # from `unscanned` (source files) and from `malformed` (metadata that
    # parsed but is incomplete): this is the instrument failing to see its own
    # input, so it forces the `error` state instead of collapsing to
    # "no invariants found → inapplicable".
    unparsed_artifacts: list[dict] = field(default_factory=list)
    # #289 item 5: the measured denominator — how many source files the scan
    # actually READ. A zero here is what separates "no unsanctioned writer
    # exists" from "the scanner never opened anything", which were the same
    # green report before.
    source_files_scanned: int = 0
    # Vacuous-pass fix (chief-wiggum#213 Phase E, widened to three states by
    # #289): "inapplicable" when the epic declares NO single-write-path
    # invariants — there was nothing to check, so a green exit is vacuous, not
    # evidence. "error" when there WAS something to measure (invariants
    # declared, a source root given) and the scan read zero files, or when a
    # declared metadata source could not be parsed — a broken instrument,
    # never a pass. Vocabulary is #289's standard outcome model.
    #   "applicable"   — the inventory was measured
    #   "inapplicable" — nothing exists to measure
    #   "error"        — inputs exist and the scanner saw none of them
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
    def measured(self) -> dict:
        return {
            "source_files_scanned": self.source_files_scanned,
            "invariants": len(self.invariants),
        }

    @property
    def counts(self) -> dict:
        return {
            "invariants": len(self.invariants),
            "writers": len(self.writers),
            "violations": len(self.violations),
            "boundary": len(self.boundary),
            "grandfathered": len(self.grandfathered),
            "malformed": len(self.malformed),
            "unscanned": len(self.unscanned),
            "unparsed_artifacts": len(self.unparsed_artifacts),
        }

    @property
    def soundness_ok(self) -> bool:
        # Design-time: metadata must be well-formed. Existing writers are surfaced,
        # not failed on (the fix may be part of the epic being architected).
        # #289: a broken measurement is not a clean soundness result either —
        # both booleans follow `applicability` so the JSON can never say
        # "outcome: error" beside "soundness_ok: true".
        return self.applicability != "error" and not self.malformed

    @property
    def coverage_ok(self) -> bool:
        # Close-time: no unsanctioned writer may exist. `unscanned` deliberately
        # does NOT participate (#282 binding decision): it is reported, never
        # blocking on its own — a repo full of binary-ish files must not
        # become unusable under --gate. A zero-file SCAN is a different animal
        # (#289): that is not "some files were skipped", it is "no inventory
        # was taken", and an empty violation list from it means nothing.
        return (self.applicability != "error"
                and not self.violations and not self.malformed)

    def to_dict(self) -> dict:
        return {
            "counts": self.counts,
            "applicability": self.applicability,
            "outcome": self.outcome,
            "measured": self.measured,
            "soundness_ok": self.soundness_ok,
            "coverage_ok": self.coverage_ok,
            "invariants": self.invariants,
            "writers": self.writers,
            "violations": self.violations,
            "boundary": self.boundary,
            "grandfathered": self.grandfathered,
            "malformed": self.malformed,
            "unscanned": self.unscanned,
            "unparsed_artifacts": self.unparsed_artifacts,
            "warnings": self.warnings,
        }


# --- parsing invariants -----------------------------------------------------


def _parse_attrs(attr_str: str) -> tuple[list[str], list[str], bool]:
    controls: list[str] = []
    writers: list[str] = []
    persistence_only = False
    for key, val in ATTR_RE.findall(attr_str):
        k = key.lower()
        items = [v for v in val.split(",") if v]
        if k == "controls_field":
            controls.extend(items)
        elif k == "sanctioned_writers":
            writers.extend(items)
        elif k == "sink":
            persistence_only = persistence_only or val.lower() in {"db", "database", "persistence"}
        elif k == "write_kind":
            persistence_only = persistence_only or val.lower() == "persistence"
    return controls, writers, persistence_only


def parse_prose_invariants(text: str, rel: str) -> tuple[list[SingleWriterInvariant], list[dict]]:
    """Extract single-write-path invariants from a prose ``invariants.md``.

    Returns (invariants, malformed). A ``@cw-writes`` tag with a controls_field but
    no sanctioned_writers (or vice-versa) is malformed — the metadata is incomplete.
    Descriptions are pulled from the nearest ``**INV-...**`` bold label if present.
    """
    invariants: list[SingleWriterInvariant] = []
    malformed: list[dict] = []
    lines = text.splitlines()
    # Map canonical INV id -> description from bold labels.
    descriptions: dict[str, str] = {}
    for line in lines:
        m = INV_DEFINE_RE.search(line)
        if m:
            descriptions[canonical_id(m.group(1))] = m.group(2).strip()
    for i, line in enumerate(lines, start=1):
        for tag in WRITES_TAG_RE.finditer(line):
            inv_id = canonical_id(tag.group("id"))
            controls, writers, persistence_only = _parse_attrs(tag.group("attrs"))
            loc = f"{rel}:{i}"
            if not controls or not writers:
                malformed.append({
                    "id": inv_id,
                    "source": loc,
                    "reason": "@cw-writes tag must set both controls_field and sanctioned_writers",
                })
                continue
            invariants.append(SingleWriterInvariant(
                id=inv_id,
                description=descriptions.get(inv_id, ""),
                controls_field=controls,
                sanctioned_writers=writers,
                source=loc,
                persistence_only=persistence_only,
            ))
    return invariants, malformed


def parse_structured_invariants(data: dict, rel: str) -> tuple[list[SingleWriterInvariant], list[dict]]:
    """Extract single-write-path invariants from a state-machines.json model."""
    invariants: list[SingleWriterInvariant] = []
    malformed: list[dict] = []
    for inv in data.get("invariants", []) or []:
        if not isinstance(inv, dict):
            continue
        controls = inv.get("controls_field")
        writers = inv.get("sanctioned_writers")
        if controls is None and writers is None:
            continue  # not a single-write-path invariant — skip (backward compatible)
        inv_id = canonical_id(str(inv.get("id", "INV-unknown-000")))
        if not controls or not writers:
            malformed.append({
                "id": inv_id,
                "source": rel,
                "reason": "invariant sets one of controls_field/sanctioned_writers but not both",
            })
            continue
        if not isinstance(controls, list) or not isinstance(writers, list):
            malformed.append({
                "id": inv_id,
                "source": rel,
                "reason": "controls_field and sanctioned_writers must be arrays of strings",
            })
            continue
        sink = str(inv.get("sink", "")).lower()
        write_kind = str(inv.get("write_kind", "")).lower()
        persistence_only = (
            bool(inv.get("persistence_only"))
            or sink in {"db", "database", "persistence"}
            or write_kind == "persistence"
        )
        invariants.append(SingleWriterInvariant(
            id=inv_id,
            description=str(inv.get("description", "")),
            controls_field=[str(c) for c in controls],
            sanctioned_writers=[str(w) for w in writers],
            source=rel,
            persistence_only=persistence_only,
        ))
    return invariants, malformed


def collect_invariants(epic_dir: str | Path) -> tuple[list[SingleWriterInvariant], list[dict]]:
    """Public, backward-compatible entry point (code_query.py and many existing
    tests call this expecting a 2-tuple) — a thin wrapper over
    ``collect_invariants_full``, which additionally reports DECLARED metadata
    sources that could not be parsed at all (#289)."""
    invariants, malformed, _unparsed = collect_invariants_full(epic_dir)
    return invariants, malformed


def collect_invariants_full(
    epic_dir: str | Path,
) -> tuple[list[SingleWriterInvariant], list[dict], list[dict]]:
    """``(invariants, malformed, unparsed_artifacts)``.

    A parse failure in a DECLARED metadata source (``state-machines.json`` /
    ``invariants.md``) is a broken instrument, not an absent precondition
    (#289): swallowing it yields zero invariants, which the caller would
    otherwise report as "nothing to check — inapplicable". Parse failures in
    any OTHER epic file stay swallowed: this gate has no authority over a
    malformed ui-spec.json, and blocking on one would be noise.
    """
    root = Path(epic_dir)
    invariants: list[SingleWriterInvariant] = []
    malformed: list[dict] = []
    unparsed: list[dict] = []
    if not root.exists():
        return invariants, malformed, unparsed
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        try:
            if path.suffix == ".json":
                data = json.loads(path.read_text())
                if isinstance(data, dict):
                    invs, bad = parse_structured_invariants(data, rel)
                    invariants += invs
                    malformed += bad
            elif path.suffix == ".md":
                invs, bad = parse_prose_invariants(path.read_text(), rel)
                invariants += invs
                malformed += bad
        except (OSError, json.JSONDecodeError) as exc:
            if path.name in DECLARED_METADATA_SOURCES:
                unparsed.append({"file": rel, "reason": f"{type(exc).__name__}: {exc}"})
            continue
    return invariants, malformed, unparsed


# --- scanning the repo for writers ------------------------------------------


def _distinct_field_forms(inv: SingleWriterInvariant) -> list[tuple[str, str]]:
    """(original controlled-field path, leaf-token) pairs, both snake and compact."""
    forms: list[tuple[str, str]] = []
    seen: set[str] = set()
    for fpath in inv.controls_field:
        leaf = fpath.split(".")[-1].strip()
        for tok in (leaf.lower(), leaf.replace("_", "").lower()):
            if tok and tok not in seen:
                seen.add(tok)
                forms.append((fpath, tok))
    return forms


def _index_sites_by_line(sites: list[WriteSite]) -> dict[tuple[str, int], list[WriteSite]]:
    """Group ``sites`` by ``(file, line)`` — the per-file line index
    ``match_writers`` used to rebuild on EVERY call (#326: once per invariant
    per file, when a scan claims the same file's sites against N invariants).
    Depends only on ``sites`` (a single file's emitted sites in
    ``_scan_writers_and_unscanned``), so callers claiming one file's sites
    against multiple invariants should build this ONCE and reuse it via
    :func:`_match_writers_indexed` — see that loop's use of this function."""
    by_line: dict[tuple[str, int], list[WriteSite]] = defaultdict(list)
    for s in sites:
        by_line[(s.file, s.line)].append(s)
    return by_line


def _match_writers_indexed(
    by_line: dict[tuple[str, int], list[WriteSite]],
    invariant: SingleWriterInvariant,
    forms: list[tuple[str, str]],
) -> list[Writer]:
    """The actual claim logic, taking an already-built line index and an
    already-computed ``_distinct_field_forms(invariant)`` — both invariant-scan
    hot-path callers (``_scan_writers_and_unscanned``) hoist once per file /
    once per invariant respectively (#326), instead of ``match_writers``'
    O(files × invariants) rebuild of both."""
    writers: list[Writer] = []
    for (file, line), line_sites in by_line.items():
        for fpath, tok in forms:
            matched: WriteSite | None = None
            for s in line_sites:
                # persistence_only (`sink=db`): only DB sinks count — the bare
                # quoted-literal-in-mutation-context and SQL UPDATE kinds. Skip
                # in-memory assignment and struct/map literals — those don't
                # write the row.
                if invariant.persistence_only and s.kind in (KIND_ASSIGN, KIND_STRUCT):
                    continue
                if s.token.lower() != tok:
                    continue
                matched = s
                break  # which kind hit doesn't affect the output; take the first
            if matched is None:
                continue
            sanctioned = matched.is_test or _is_sanctioned(invariant, file, matched.symbol)
            writers.append(Writer(
                invariant_id=invariant.id,
                field=fpath,
                file=file,
                line=line,
                text=matched.text,
                symbol=matched.symbol,
                sanctioned=sanctioned,
                is_test=matched.is_test,
            ))
            break  # one write record per (line, invariant)
    return writers


def match_writers(sites: list[WriteSite], invariant: SingleWriterInvariant) -> list[Writer]:
    """Claim: query-time filter of field-agnostic ``sites`` against a single
    invariant's controlled fields + sanctioned writers. Mirrors the original
    interleaved scan exactly — including "one write record per (line,
    invariant), first controlled-field-form wins" — but now as a pure function
    of pre-emitted sites, with no filesystem access.

    Public, backward-compatible entry point: builds its own line index and
    field-forms list on every call. A caller claiming the SAME file's sites
    against MANY invariants (``_scan_writers_and_unscanned``'s hot path)
    should build both once via :func:`_index_sites_by_line`/
    :func:`_distinct_field_forms` and call :func:`_match_writers_indexed`
    directly instead — see that function's docstring (#326).
    """
    return _match_writers_indexed(_index_sites_by_line(sites), invariant, _distinct_field_forms(invariant))


def scan_writers(
    source_root: str | Path,
    invariants: list[SingleWriterInvariant],
    exclude: list[str] | None = None,
    only_files: set[str] | None = None,
) -> list[Writer]:
    """Find every writer of every controlled field across the repo. Public,
    backward-compatible entry point (code_query.py and many existing tests
    call this expecting a plain ``list[Writer]``) — a thin wrapper over
    ``_scan_writers_and_unscanned``, which additionally tracks files that
    could not be READ at all (chief-wiggum#282); ``check()`` calls that
    directly so it can surface them, never silently."""
    writers, _unscanned, _scanned = _scan_writers_and_unscanned(
        source_root, invariants, exclude=exclude, only_files=only_files
    )
    return writers


def _scan_writers_and_unscanned(
    source_root: str | Path,
    invariants: list[SingleWriterInvariant],
    exclude: list[str] | None = None,
    only_files: set[str] | None = None,
) -> tuple[list[Writer], list[dict], int]:
    """Emit field-agnostic write sites per file, then claim them against each
    invariant. ``only_files`` (repo-relative paths), when given, restricts the
    walk to that set instead of the whole tree — used by ``--changed-since``.

    Returns ``(writers, unscanned, scanned)``: ``unscanned`` lists
    ``{"file", "reason"}`` for every candidate that could not be READ at all
    (chief-wiggum#282) — decode failures alone can no longer land here
    (``read_text_safe`` BOM-sniffs and falls back to a lossy decode rather than
    skipping), so this is genuinely "could not open this file", never a silent
    drop. ``scanned`` is the count of files actually read: the measured
    denominator (#289), so a zero-file scan cannot pass for a clean inventory
    — incremented identically whether a file's write sites were served from
    the #327 cache or freshly emitted, so the denominator a cached run reports
    is indistinguishable from an uncached one.

    Per-file EMISSION (the field-agnostic write sites) is cached (#327), keyed
    by ``(rel, blob_sha, scanner_hash)`` — never the CLAIM (``match_writers``
    against each invariant, always computed fresh here over every site
    returned, cached or not). ``candidates`` is ALWAYS the full walk (or the
    ``--changed-since`` set); caching only decides whether a file's sites are
    RECOMPUTED or SERVED, never whether the file is visited. The manifest that
    supplies ``blob_sha`` requires git; a non-git ``--source`` (or a path the
    manifest legitimately excludes) degrades that file to a live scan, exactly
    as before this cache existed.
    """
    root = Path(source_root)
    exclude = exclude or []
    writers: list[Writer] = []
    unscanned: list[dict] = []
    scanned = 0
    if not root.exists() or not invariants:
        return writers, unscanned, scanned

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

    # (#326) _distinct_field_forms(invariant) depends ONLY on the invariant,
    # not on any file's sites — computed once here (O(invariants)) instead of
    # once per (file, line-group, invariant) inside match_writers, which was
    # the actual hot loop this scan used to re-derive it in.
    forms_by_invariant = [_distinct_field_forms(inv) for inv in invariants]

    for rel in candidates:
        if Path(rel).suffix not in SOURCE_EXTS:
            continue
        if any(part in SKIP_PARTS for part in Path(rel).parts):
            continue
        if _excluded(rel, exclude):
            continue
        path = root / rel
        blob_sha = manifest.get(rel) if manifest is not None else None
        sites: list | None = None
        if blob_sha is not None and scanner_hash is not None:
            cached = findings_cache.load(str(root), "check_single_writer", rel, blob_sha, scanner_hash)
            if cached is not None:
                sites = [WriteSite(**d) for d in cached]
        if sites is None:
            text, skip_reason = read_text_safe(path)
            if skip_reason is not None:
                unscanned.append({"file": rel, "reason": skip_reason})
                continue
            # Route through the per-language emitter registry (#162) — the gate
            # consumes the SAME dispatch path scripts/emitters exposes, so a
            # per-language emitter can never drift from what the gate actually
            # scans. Every SOURCE_EXTS extension has an emitter (a tier-1 language
            # module or the generic regex tier), so tier is never "unsupported"
            # here; genuinely unsupported extensions are counted separately by
            # unsupported_extension_counts.
            facts, _tier = emitters.emit(rel, text)
            sites = emitters.facts_of_kind(facts, "write_site")
            if blob_sha is not None and scanner_hash is not None:
                findings_cache.store(
                    str(root), "check_single_writer", rel, blob_sha, scanner_hash,
                    [asdict(s) for s in sites],
                )
        scanned += 1
        if not sites:
            continue
        # Claim per invariant, then merge preserving the ORIGINAL ordering: line
        # ascending first, invariant list-order second (the original scan looped
        # "for line: for invariant", not "for invariant: for line" — a file with
        # hits for multiple invariants at interleaved lines must come out in line
        # order, not grouped by invariant).
        # (#326) by_line is built ONCE per file here, not once per (file,
        # invariant) as a bare match_writers(sites, inv) call per invariant
        # would rebuild it — O(files × lines) instead of O(files × invariants
        # × lines).
        by_line = _index_sites_by_line(sites)
        tagged: list[tuple[int, Writer]] = []
        for idx, inv in enumerate(invariants):
            for w in _match_writers_indexed(by_line, inv, forms_by_invariant[idx]):
                tagged.append((idx, w))
        tagged.sort(key=lambda t: (t[1].line, t[0]))
        writers.extend(w for _, w in tagged)
    return writers, unscanned, scanned


def _is_sanctioned(inv: SingleWriterInvariant, rel: str, symbol: str | None) -> bool:
    """A writer is sanctioned if its enclosing symbol OR its file matches an entry
    in ``sanctioned_writers``. File entries match as a path suffix (so a repo-root
    relative ``internal/billing/reconcile.go`` matches regardless of scan cwd)."""
    rel_norm = rel.replace("\\", "/")
    for entry in inv.sanctioned_writers:
        e = entry.strip()
        if not e:
            continue
        if "/" in e or e.endswith((".go", ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".rb", ".rs")):
            # Treat as a file path (or glob-ish suffix).
            if rel_norm == e or rel_norm.endswith("/" + e) or rel_norm.endswith(e):
                return True
        else:
            # Treat as a symbol name (function/method), case-insensitive.
            if symbol and symbol.lower() == e.lower():
                return True
    return False


# --- manifest-scoped scanning (--changed-since) -----------------------------


def _file_predicate(rel: str) -> bool:
    """The scanner's EXACT file-selection rule (extension allow-list + skipped
    directories) — the same predicate `scan_writers` applies during its own
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
    coverage gap silent. ``scan_writers`` itself still filters candidates to
    ``SOURCE_EXTS``, so the extra paths never affect the writer scan."""
    p = Path(rel)
    if any(part in SKIP_PARTS for part in p.parts):
        return False
    return p.suffix in SOURCE_EXTS or p.suffix in emitters.unsupported_extensions()


def _scanner_version() -> str:
    """Hash-derived ``--scanner-version``: the source of this module plus its
    ``chief_wiggum`` dependencies (annotations.py carries the @cw-writes
    grammar — #170 moved it there). artifacts.py (#213 Phase D) is
    finding-affecting: its scope-matching rule decides the in-domain vs
    boundary classification under --scope. No hand-bumped constant to forget."""
    here = Path(__file__).resolve()
    cw_dir = here.parent / "chief_wiggum"
    return scanner_version(
        here,
        here.parent / "artifacts.py",
        cw_dir / "annotations.py",
        cw_dir / "grandfather.py",
        cw_dir / "manifest.py",
        cw_dir / "hashing.py",
        cw_dir / "write_emission.py",
        cw_dir / "languages.py",
        # The per-file findings cache (#327) — finding-affecting by
        # construction: a bug in HOW a cache hit is validated or served would
        # change what a cached run reports vs a fresh scan, so any edit here
        # must invalidate every previously-cached entry, not just the ones
        # whose file content changed.
        cw_dir / "findings_cache.py",
        # Decode-defensive bulk-source read (#282) — finding-affecting: a
        # decode-policy change here changes what a file's write sites look
        # like once read, or whether it lands in `unscanned` at all.
        cw_dir / "textio.py",
        # The DATA the loader reads, not just the loader (#259): moving an
        # extension between generic_tier / unsupported_extensions changes
        # exactly which files this scanner walks, with no code change at
        # all — the CTR-fh-041 silent-staleness class, one layer out.
        here.parent.parent / "config" / "languages.json",
    )


def unsupported_extension_counts(
    source_root: str | Path,
    exclude: list[str] | None = None,
    only_files: set[str] | None = None,
) -> dict[str, int]:
    """Count files with a RECOGNIZED-but-unsupported extension (#162) — one
    ``scripts/emitters`` has no emitter for at all (language-specific or
    generic tier) — among the same candidate set ``scan_writers`` would walk.
    Never silent: this feeds a ``coverage`` warning in ``check()`` instead of
    the file simply disappearing from the scan with no trace. Extensions
    outside the curated ``config/languages.json`` unsupported list (arbitrary
    non-source files: markdown, images, lockfiles, ...) are not counted —
    only extensions this repo explicitly recognizes as "a real language we
    don't scan yet" are worth flagging."""
    root = Path(source_root)
    exclude = exclude or []
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
        if _excluded(rel, exclude):
            continue
        counts[suffix] = counts.get(suffix, 0) + 1
    return counts


# --- adoption grandfathers (#215 F5) ----------------------------------------


def grandfather_key(violation: dict) -> str:
    """The grandfather-file key for one violation — EXACTLY what
    ``adopt.py grandfather`` writes: ``check_single_writer:`` + the
    invariant id, controlled-field path, and repo-relative file, colon-joined
    (see chief_wiggum.grandfather's key grammar)."""
    return "check_single_writer:" + ":".join(
        str(violation.get(k, "?")) for k in ("invariant_id", "field", "file"))


def _apply_grandfathers(report: SingleWriterReport, entries: dict[str, dict],
                        *, today: date | None = None) -> None:
    """Apply adoption-grandfather waivers to a built report, in place —
    mirroring the JUSTIFIED-waiver mechanics (chief_wiggum.trace_links): a
    NON-EXPIRED entry moves the violation to ``grandfathered`` (reported,
    never blocking); an EXPIRED entry does NOT waive — the violation stays in
    ``violations`` (blocks again) with ``grandfather_expired``/``expiry``
    labels for the renderer."""
    today = today or date.today()
    remaining: list[dict] = []
    for v in report.violations:
        entry = entries.get(grandfather_key(v))
        if entry is None:
            remaining.append(v)
            continue
        if cw_grandfather.is_expired(entry, today):
            remaining.append({**v, "grandfather_expired": True,
                              "grandfather_expiry": entry.get("expiry")})
        else:
            report.grandfathered.append({
                **v, "grandfathered": True,
                "grandfather_expiry": entry.get("expiry"),
                "grandfather_owner": entry.get("owner"),
                "grandfather_reason": entry.get("reason"),
            })
    report.violations = remaining


# --- top-level check --------------------------------------------------------


def check(
    epic_dir: str | Path,
    source_root: str | Path | None = None,
    exclude: list[str] | None = None,
    changed_since: str | None = None,
    scope: dict | None = None,
    grandfather_path: str | Path | None = None,
    today: date | None = None,
) -> SingleWriterReport:
    """``scope`` (chief-wiggum#213 Phase D, optional) is a parsed scope.json
    document ({"include": [...], "exclude": [...]}). When given, detection
    still scans REPO-WIDE, but findings are classified: writers whose file is
    in scope are in-domain (blocking-eligible, exactly as without a scope);
    writers outside scope are BOUNDARY findings — surfaced in
    ``report.boundary`` with ``boundary: true``, never violations, never
    affecting the exit code. ``None`` (no --scope) is byte-identical to the
    pre-scope behavior. ``grandfather_path`` (#215 F5, optional) is the
    adoption grandfather file: an in-domain violation whose key
    (``check_single_writer:<INV-id>:<field>:<file>``) matches a NON-EXPIRED
    entry moves to ``report.grandfathered`` (non-blocking); an EXPIRED entry
    leaves the violation blocking, labeled. ``today`` overrides the
    expiry-check clock (defaults to the real today)."""
    report = SingleWriterReport()
    invariants, malformed, unparsed = collect_invariants_full(epic_dir)
    report.invariants = [inv.to_dict() for inv in invariants]
    report.malformed = malformed
    report.unparsed_artifacts = unparsed

    if unparsed:
        # #289: the declared metadata source exists and could not be parsed.
        # Whatever invariant list came out of this run is short by an unknown
        # amount — a broken instrument, never "nothing to check".
        report.applicability = "error"
        for entry in unparsed:
            report.warnings.append(
                f"{entry['file']}: declared metadata source could not be parsed "
                f"({entry['reason']}) — the invariant set is incomplete by breakage, "
                "not by absence (ERROR, not inapplicable)"
            )
        return report

    if not invariants:
        report.applicability = "inapplicable"
        report.warnings.append(
            "no single-write-path invariants found (no controls_field/sanctioned_writers "
            "metadata); nothing to check — inapplicable, not passing"
        )
        return report

    if source_root:
        # The epic's OWN artifacts (invariants.md, rendered models/*.py, contract
        # assertions) DESCRIBE the controlled field; they never write the production
        # row. When the epic dir lives under the scanned source_root (the common case:
        # source is the repo root, epic is docs/epics/<slug>), exclude that subtree so a
        # field token appearing in a rendered `@deal.post` message or a guard template
        # (e.g. `{active_owner_count:-1}` inside a spec string) is not mis-read as a
        # second writer. Writers must be found in the implementation, not the spec.
        scan_exclude = list(exclude or [])
        try:
            epic_rel = Path(epic_dir).resolve().relative_to(Path(source_root).resolve())
            rel_str = str(epic_rel)
            if rel_str and rel_str != ".":
                scan_exclude.append(rel_str)
        except ValueError:
            pass  # epic_dir is outside source_root (e.g. CW_TMP at architect time)
        only_files = None
        if changed_since:
            # Ticket-scoped speed-up ONLY — never used by /close-epic's coverage
            # gate, which must see the whole repo to be authoritative. The
            # predicate is widened with unsupported extensions so a changed
            # .php/.cpp still triggers the coverage warning (scan_writers
            # filters back down to SOURCE_EXTS itself).
            only_files = changed_paths(source_root, changed_since, predicate=_changed_since_predicate)
        writers, unscanned, scanned = _scan_writers_and_unscanned(
            source_root, invariants, exclude=scan_exclude, only_files=only_files
        )
        report.unscanned = unscanned
        report.source_files_scanned = scanned
        if scanned == 0 and only_files is None:
            # #289 — THE fail-open this gate shipped with: invariants declared,
            # a source root given, and the scan read nothing (root absent, no
            # file of a scannable language, or an --exclude that swallowed the
            # tree). The empty violation list below is the absence of a
            # measurement, not a clean inventory. Under --changed-since
            # (only_files is not None) a zero is EXPECTED — a diff that touched
            # no source file genuinely has nothing to measure — so that path
            # stays applicable.
            report.applicability = "error"
            report.warnings.append(
                f"scanned 0 source files under {source_root} while "
                f"{len(invariants)} single-write-path invariant(s) are declared — "
                "no writer inventory was taken, so an empty violation list proves "
                "nothing (ERROR, not a pass)"
            )
        if unscanned:
            # Never silent (#282): a file the scanner could not even read is
            # visible with its path, same treatment as unsupported_extension_counts
            # below — report-only, does not affect soundness_ok/coverage_ok.
            report.warnings.append(
                f"{len(unscanned)} file(s) could not be read during the scan (unscanned) — "
                "see the Unscanned files section"
            )
        if scope is not None:
            # Authority split (#213 Phase D): repo-wide detection, boundary-
            # stopped authority. In-domain writers keep today's exact gate
            # semantics; out-of-scope writers become boundary findings —
            # visible, `boundary: true`, never blocking, never auto-anything.
            in_domain = [w for w in writers if artifacts.path_in_scope(scope, w.file)]
            report.boundary = [
                {**w.to_dict(), "boundary": True}
                for w in writers if not artifacts.path_in_scope(scope, w.file)
            ]
        else:
            in_domain = writers
        report.writers = [w.to_dict() for w in in_domain]
        report.violations = [w.to_dict() for w in in_domain if not w.sanctioned]
        if grandfather_path is not None:
            gf_entries, gf_warning = cw_grandfather.load_entries(grandfather_path)
            if gf_warning:
                report.warnings.append(gf_warning)
            if gf_entries:
                _apply_grandfathers(report, gf_entries, today=today)
        # Surface any invariant whose controlled field has NO writer at all — the
        # sanctioned path may be missing/misnamed (a soft warning, not a violation).
        # Skipped under --changed-since: a ticket-scoped scan is EXPECTED to miss
        # unrelated invariants' writers, so this warning would just be noise.
        # Boundary writers still count as "found" here: a writer exists, it is
        # just out of domain.
        if not changed_since:
            written_ids = {w.invariant_id for w in writers}
            for inv in invariants:
                if inv.id not in written_ids:
                    report.warnings.append(
                        f"{inv.id}: no writer found for {inv.controls_field} — "
                        f"sanctioned writer(s) {inv.sanctioned_writers} may be missing or misnamed"
                    )
        # Coverage metadata (#162): a recognized-but-unsupported-language file is
        # NEVER silently dropped — surfaced as an explicit warning, same as any
        # other coverage gap this checker reports.
        unsupported = unsupported_extension_counts(source_root, exclude=scan_exclude, only_files=only_files)
        if unsupported:
            total = sum(unsupported.values())
            detail = ", ".join(f"{ext} ({n})" for ext, n in sorted(unsupported.items()))
            report.warnings.append(
                f"{total} file(s) skipped: no emitter coverage for recognized-but-unsupported "
                f"extension(s) {detail} — see config/languages.json"
            )
    else:
        report.warnings.append("no --source given; parsed invariant metadata only (no repo scan)")

    return report


# --- rendering / CLI --------------------------------------------------------


def render_text(report: SingleWriterReport) -> str:
    c = report.counts
    lines = [
        "# Single-Writer Audit",
        "",
        f"Single-write-path invariants: {c['invariants']}",
        # #289 item 5: the denominator is printed on every run, green or not,
        # so a zero is visible without reading the JSON.
        f"Measured: {report.source_files_scanned} source file(s) scanned",
        f"Writers found: {c['writers']}  |  Violations: {c['violations']}  |  "
        f"Grandfathered: {c['grandfathered']}  |  Malformed metadata: {c['malformed']}  |  "
        f"Unscanned: {c['unscanned']}",
        "",
        f"- Outcome: {report.outcome.upper()}",
        f"- Soundness (metadata well-formed): {'OK' if report.soundness_ok else 'FINDINGS'}",
        f"- Coverage (no unsanctioned writer): {'OK' if report.coverage_ok else 'FINDINGS'}",
    ]
    if report.applicability == "inapplicable":
        lines.append(
            "- Applicability: INAPPLICABLE — no single-write-path invariants defined; "
            "nothing was checked (not a real pass)"
        )
    if report.applicability == "error":
        lines.append(
            "- Applicability: ERROR — the instrument measured nothing; this result "
            "is the absence of a finding, not the absence of a problem"
        )
    if report.unparsed_artifacts:
        lines += ["", "## Unparsed metadata sources", ""]
        lines += [f"- {u['file']}: {u['reason']}" for u in report.unparsed_artifacts]
    if report.malformed:
        lines += ["", "## Malformed metadata", ""]
        lines += [f"- {m['id']} ({m['source']}): {m['reason']}" for m in report.malformed]
    if report.violations:
        lines += ["", "## Unsanctioned writers (single-write-path violations)", ""]
        for v in report.violations:
            sym = f" in {v['symbol']}()" if v.get("symbol") else ""
            expired = ""
            if v.get("grandfather_expired"):
                expired = (f" [EXPIRED grandfather — expiry "
                           f"{v.get('grandfather_expiry') or '?'} passed; blocks again]")
            lines.append(
                f"- {v['invariant_id']} field `{v['field']}` written at "
                f"{v['file']}:{v['line']}{sym}{expired}"
            )
            lines.append(f"    {v['text']}")
    if report.grandfathered:
        lines += ["", "## Grandfathered writers (pre-adoption baseline — waived, non-blocking)", ""]
        for v in report.grandfathered:
            sym = f" in {v['symbol']}()" if v.get("symbol") else ""
            lines.append(
                f"- {v['invariant_id']} field `{v['field']}` written at "
                f"{v['file']}:{v['line']}{sym} "
                f"(expires {v.get('grandfather_expiry') or '?'})"
            )
    if report.writers and not report.violations:
        lines += ["", "## Sanctioned writers", ""]
        for w in report.writers:
            sym = f" in {w['symbol']}()" if w.get("symbol") else ""
            tag = " [test]" if w.get("is_test") else ""
            lines.append(f"- {w['invariant_id']} `{w['field']}` at {w['file']}:{w['line']}{sym}{tag}")
    if report.boundary:
        lines += [
            "",
            "## Boundary findings (outside the domain scope — visible, NEVER blocking)",
            "",
            "Detection scans repo-wide; authority stops at the scope boundary. These",
            "writers of a controlled field live outside this domain's scope: report",
            "them to the owning team — they do not affect this gate's exit code and",
            "are never auto-filed or auto-fixed.",
            "",
        ]
        for w in report.boundary:
            sym = f" in {w['symbol']}()" if w.get("symbol") else ""
            sanc = "" if w.get("sanctioned") else " [unsanctioned]"
            lines.append(
                f"- {w['invariant_id']} field `{w['field']}` written at "
                f"{w['file']}:{w['line']}{sym}{sanc}"
            )
            lines.append(f"    {w['text']}")
    if report.unscanned:
        lines += [
            "",
            "## Unscanned files (could not be read — never blocking on their own)",
            "",
        ]
        lines += [f"- {u['file']}: {u['reason']}" for u in report.unscanned]
    if report.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in report.warnings]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Single-writer / mutator-inventory checker for single-write-path invariants"
    )
    parser.add_argument(
        "epic_dir", nargs="?", default=None,
        help="docs/epics/<slug> directory (or CW_TMP at architect time); not required with --scanner-version",
    )
    parser.add_argument("--source", help="Repo root to scan for writers of controlled fields")
    parser.add_argument(
        "--scope",
        metavar="PATH|auto",
        help="Domain-scope authority split (chief-wiggum#213): path to a scope.json "
        "({'include': [globs], 'exclude': [globs]}), or the literal 'auto' to read it "
        "from the --source target's resolved meta root (scripts/artifacts.py). Detection "
        "still scans repo-wide; writers INSIDE scope stay blocking-eligible exactly as "
        "today, writers OUTSIDE scope become boundary findings — reported, boundary: true "
        "in JSON, never affecting the exit code. Omitted: behavior identical to today.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="Repo-relative path/dir/glob to skip; repeatable (e.g. --exclude ui --exclude '**/*.gen.ts')",
    )
    parser.add_argument(
        "--gate",
        choices=["soundness", "coverage"],
        help="Fail (exit 1) on this gate's findings (soundness=malformed metadata; "
        "coverage=unsanctioned writers)",
    )
    parser.add_argument(
        "--grandfather",
        metavar="PATH",
        help="Path to the adoption grandfather file (#215; adopt.py writes "
        f"<meta root>/{cw_grandfather.GRANDFATHER_RELPATH}). Defaults to that resolver "
        "location for the --source target. Keys for this gate: "
        "check_single_writer:<INV-id>:<field>:<file>. Non-expired entries waive "
        "matching violations (reported under Grandfathered); EXPIRED entries "
        "block again.",
    )
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

    if not Path(args.epic_dir).exists():
        print(f"Error: epic dir not found: {args.epic_dir}", file=sys.stderr)
        return 2

    # #289: a --source that does not exist is a typo, not a repo with no
    # unsanctioned writers. Caught here with a clear message rather than left
    # to become an `error` outcome further down (check() still classifies it
    # as one for library callers).
    if args.source and not Path(args.source).exists():
        print(f"Error: --source repo not found: {args.source}", file=sys.stderr)
        return 2

    scope = None
    if args.scope:
        # load_scope_file raises ValueError on a malformed or unknown-key scope
        # document (#213 F6: {"includes": ...} must never silently mean
        # whole-repo scope) — surfaced as a usage error, exit 2.
        try:
            if args.scope == "auto":
                # The --source target's elected meta root (embedded: <repo>/docs;
                # sidecar: the external meta dir). No scope.json there = whole-repo
                # scope — the documented default, not an error.
                scope_path = artifacts.Resolver.resolve(Path(args.source or ".")).scope_path()
                scope = artifacts.load_scope_file(scope_path)
                if scope is None:
                    scope = {}  # whole repo: everything in-domain, nothing boundary
            else:
                # An explicit path that doesn't exist is a usage error — a typo'd
                # --scope silently meaning "everything in-domain" would be a silent
                # authority widening.
                scope = artifacts.load_scope_file(args.scope)
                if scope is None:
                    print(f"Error: cannot read scope file: {args.scope}", file=sys.stderr)
                    return 2
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    if args.grandfather:
        grandfather_path = Path(args.grandfather)
    else:
        # Default: the --source target's resolved adoption grandfather file
        # (#215). Missing file degrades to graceful absence inside check().
        grandfather_path = (
            artifacts.Resolver.resolve(Path(args.source or ".")).meta_root
            / cw_grandfather.GRANDFATHER_RELPATH
        )

    try:
        report = check(args.epic_dir, args.source, exclude=args.exclude,
                       changed_since=args.changed_since, scope=scope,
                       grandfather_path=grandfather_path)
    except ManifestError as exc:
        # Bad --changed-since ref, non-git --source, missing HEAD, no git binary:
        # a usage error, reported concisely — never a traceback.
        print(f"Error: --changed-since manifest failed: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_text(report))

    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from factory_log import emit_gate
        caught = len(report.violations) + len(report.malformed)
        parts = os.path.abspath(args.epic_dir).split(os.sep)
        repo = parts[parts.index("docs") - 1] if "docs" in parts and parts.index("docs") > 0 \
            else os.path.basename(os.path.abspath(args.epic_dir))
        emit_gate("check_single_writer", "fail" if caught else "pass", caught=caught, repo=repo)
    except Exception:
        pass

    # Vacuous-pass fix (#213 Phase E): an inapplicable run under --gate still
    # exits 0 (existing green pipelines must not break) but the verdict is
    # NEVER a silent identical green — the banner is printed and the JSON
    # carries applicability explicitly, so a wrapper can distinguish.
    if args.gate and report.applicability == "inapplicable":
        print(
            "check_single_writer: INAPPLICABLE — no single-write-path invariants "
            f"defined; --gate {args.gate} passes vacuously (nothing was checked, "
            "not a real green)",
            file=sys.stderr,
        )

    # #289: a BROKEN measurement fails EITHER gate. "The scanner read nothing"
    # is not a clean soundness result and not a clean coverage result — it is
    # the absence of a result. Exit stays in the uniform 0/1/2 contract every
    # gate shares (docs/gate-rollout.md); the state is carried by the banner
    # and by "applicability": "error" / "outcome": "error" in the JSON.
    if args.gate and report.applicability == "error":
        print(
            "check_single_writer: ERROR — "
            f"{report.source_files_scanned} source file(s) scanned for "
            f"{len(report.invariants)} single-write-path invariant(s); "
            f"--gate {args.gate} measured nothing (FAILURE, not a pass). "
            "See the warnings for which input the instrument could not see "
            "(chief-wiggum#289)",
            file=sys.stderr,
        )
        return 1

    if args.gate == "soundness" and not report.soundness_ok:
        return 1
    if args.gate == "coverage" and not report.coverage_ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
