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

Known limitations (regex, not a type checker): even with ``sink=db`` two residual false
positives remain because they need collection/type awareness the scanner doesn't have —
(1) a same-named field written to a DIFFERENT collection in a mutation context (e.g. an
audit-log ``bson.M{"plan": …}``), and (2) a FILTER clause with a literal value
(``bson.M{"plan": ""}`` — a ``$``-operator filter IS skipped, a bare-literal one is not).
Mitigate with precise ``sanctioned_writers`` and ``--exclude``. Because of this, wire a
new single-write-path invariant on a common field as **report-only first** (no ``--gate``)
and confirm the finding set is clean before making it a ``coverage`` blocker.

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
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
from chief_wiggum.manifest import ManifestError, changed_paths, walk_source_files  # noqa: E402

# Same INV- shape as check_traceability.py (case-insensitive slug segment).
INV_ID_RE = re.compile(r"\bINV-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3}(?![A-Za-z0-9-])", re.IGNORECASE)

# Prose invariant declaration (bold label), same as check_traceability's DEFINE_RE
# but scoped to INV- and capturing the description for reporting.
INV_DEFINE_RE = re.compile(r"\*\*\s*(INV-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3})\s*\*\*\s*:?\s*(.*)")

SOURCE_EXTS = {".go", ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".rb", ".rs"}
SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", "vendor", "dist", "build"}

# A file is test infrastructure (not a sanctioned/unsanctioned production writer)
# — same heuristic as check_traceability.py. Test writes of a controlled field
# are fixtures, not a competing production write path, so they don't violate.
def _is_test_path(rel: str) -> bool:
    low = rel.lower()
    return "test" in low or "spec" in low or any(p == "e2e" for p in Path(low).parts)


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
    writers: list[dict] = field(default_factory=list)      # all production writers found
    violations: list[dict] = field(default_factory=list)   # unsanctioned writers
    malformed: list[dict] = field(default_factory=list)     # bad metadata (soundness)
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict:
        return {
            "invariants": len(self.invariants),
            "writers": len(self.writers),
            "violations": len(self.violations),
            "malformed": len(self.malformed),
        }

    @property
    def soundness_ok(self) -> bool:
        # Design-time: metadata must be well-formed. Existing writers are surfaced,
        # not failed on (the fix may be part of the epic being architected).
        return not self.malformed

    @property
    def coverage_ok(self) -> bool:
        # Close-time: no unsanctioned writer may exist.
        return not self.violations and not self.malformed

    def to_dict(self) -> dict:
        return {
            "counts": self.counts,
            "soundness_ok": self.soundness_ok,
            "coverage_ok": self.coverage_ok,
            "invariants": self.invariants,
            "writers": self.writers,
            "violations": self.violations,
            "malformed": self.malformed,
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
    root = Path(epic_dir)
    invariants: list[SingleWriterInvariant] = []
    malformed: list[dict] = []
    if not root.exists():
        return invariants, malformed
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
        except (OSError, json.JSONDecodeError):
            continue
    return invariants, malformed


# --- scanning the repo for writers ------------------------------------------


# Generic (field-agnostic) write-site detectors. Each captures a candidate
# identifier token; whether that token belongs to any particular invariant's
# controlled field is a QUERY-TIME decision (match_writers), not baked into the
# regex. Kind indices (0-3) mirror the four write shapes documented in the
# module docstring and drive `persistence_only` filtering in match_writers.
KIND_ASSIGN, KIND_STRUCT, KIND_QUOTED, KIND_SQL = range(4)

# The token classes are wider than `\w+` on purpose: the pre-split scanner
# built its regexes from `re.escape(leaf_token)`, so a controlled field whose
# leaf contains a hyphen (e.g. a Mongo key `plan-tier`) DID match. Emission
# must cover at least that same token surface or a full scan could silently
# miss an unsanctioned writer of such a field; over-wide captures are harmless
# (a token that matches no invariant's field is dropped at claim time).
# 0. Assignment: `something.Plan =` / `.stripe_plan =` (not ==; `:=` — Go's
#    declare+assign — IS a write, so it's allowed).
ASSIGN_RE = re.compile(r"\.([\w-]+)\s*:?=[^=]")
# 1. Struct-literal / map set: `Plan: value` or `"plan": value` or `Key: "plan"`.
#    `:(?!=)` so Go's short-var-decl `plan := expr` is NOT read as a field set
#    (the captured token would be the local var name, and `:` the `:` of `:=`).
STRUCT_RE = re.compile(r"""(^|[\s,{(])['"]?([\w.-]+)['"]?\s*:(?!=)\s*""")
# 2. bson/Mongo update key referenced literally in a set expression. `.` is in
#    the class so a dotted key (`"provider.plan"`) is captured whole — leaf
#    tokens never contain dots, so it can't claim-match a leaf field, exactly
#    like the old quote-delimited exact-token match behaved.
QUOTED_RE = re.compile(r"""['"]([\w.-]+)['"]""")
# 3. SQL UPDATE ... SET <field> = ...  (multiple fields may be set on one line).
SQL_SET_KEYWORD_RE = re.compile(r"\bSET\b", re.IGNORECASE)
SQL_FIELD_RE = re.compile(r"\b([\w-]+)\s*=")

# A bson $set / Mongo update / SQL UPDATE context marker — a bare `"plan":` in a
# non-mutating context (e.g. a JSON response DTO field) shouldn't count. We only
# treat KIND_QUOTED (quoted-literal) as a write when the surrounding lines look
# like a mutation. KIND_ASSIGN and KIND_STRUCT are writes on their own.
MUTATION_CONTEXT_RE = re.compile(
    r"\$set|UpdateOne|UpdateMany|UpdateByID|FindOneAndUpdate|bson\.[ME]|SET\b|UPDATE\b",
    re.IGNORECASE,
)

# A bson/Mongo QUERY operator on the same line means the `"field":` there is a FILTER
# clause (which document to match), not a `$set` value (what to write). e.g.
# `bson.M{"plan": bson.M{"$exists": false}}` selects rows, it doesn't write plan. Skip it.
FILTER_OPERATOR_RE = re.compile(
    r"\$(?:exists|in|nin|ne|eq|gt|gte|lt|lte|regex|or|and|not|nor|type|all|elemMatch|size)\b"
)

# Line-comment markers per language. Used to strip trailing comments before matching,
# so a field name mentioned in a comment (e.g. `// Free plan: …`) is not read as a write.
_COMMENT_MARKERS = {
    ".go": ("//",), ".ts": ("//",), ".tsx": ("//",), ".js": ("//",), ".jsx": ("//",),
    ".java": ("//",), ".rs": ("//",), ".py": ("#",), ".rb": ("#",),
}


def _strip_line_comment(line: str, suffix: str) -> str:
    """Drop a trailing line comment, respecting string/char literals so an in-string
    marker (a URL's `//`, a TS `#private`, a `#` inside a Python string) is preserved.
    Multi-line strings aren't tracked (per-line scan) — acceptable: at worst a comment
    marker inside a rare multi-line literal truncates a line we only search for writes."""
    markers = _COMMENT_MARKERS.get(suffix)
    if not markers:
        return line
    quote: str | None = None
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        for m in markers:
            if line.startswith(m, i):
                return line[:i]
        i += 1
    return line


GO_FUNC_RE = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)")
PY_FUNC_RE = re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)")
TS_FUNC_RE = re.compile(r"(?:function\s+([A-Za-z_$][\w$]*)|([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?\()")


def _enclosing_symbol(lines: list[str], idx: int) -> str | None:
    """Nearest function/method name declared at or above line index ``idx``."""
    for j in range(idx, -1, -1):
        line = lines[j]
        for pat in (GO_FUNC_RE, PY_FUNC_RE):
            m = pat.match(line)
            if m:
                return m.group(1)
        m = TS_FUNC_RE.search(line)
        if m:
            return m.group(1) or m.group(2)
    return None


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


@dataclass
class WriteSite:
    """A FIELD-AGNOSTIC candidate write site: emission time knows nothing about
    any invariant's controlled field — just "this line assigns/sets/matches an
    identifier token, in this file, in this enclosing symbol". Whether ``token``
    belongs to a controlled field is a query-time decision (``match_writers``).
    """

    file: str
    line: int  # 1-indexed
    text: str  # raw (comment-un-stripped) line, stripped + truncated for display
    symbol: str | None
    is_test: bool
    kind: int  # KIND_ASSIGN | KIND_STRUCT | KIND_QUOTED | KIND_SQL
    token: str  # the identifier exactly as it appears in source (case preserved)


def emit_write_sites(path: str, text: str) -> list[WriteSite]:
    """Emission: every candidate write site in a single file's ``text``, with NO
    knowledge of any specific invariant or controlled field (see module + class
    docstrings). ``path`` is a repo-relative label used only for ``.suffix``
    (comment-marker lookup) and the ``file`` attribute — the file is never
    re-read from disk, so this is safe to call on manifest-sourced content.
    """
    suffix = Path(path).suffix
    raw_lines = text.splitlines()
    code_lines = [_strip_line_comment(rl, suffix) for rl in raw_lines]
    is_test = _is_test_path(path)
    sites: list[WriteSite] = []
    for i, line in enumerate(code_lines):
        candidates: list[tuple[int, str]] = []  # (kind, token)
        for m in ASSIGN_RE.finditer(line):
            candidates.append((KIND_ASSIGN, m.group(1)))
        for m in STRUCT_RE.finditer(line):
            candidates.append((KIND_STRUCT, m.group(2)))
        for m in QUOTED_RE.finditer(line):
            # A bare quoted literal only counts as a write in a mutation context
            # (this line or either of the two lines above) and NOT when the same
            # line carries a query operator (a filter clause, not a $set value).
            # Both checks are invariant-independent, so resolved once here.
            if not (
                MUTATION_CONTEXT_RE.search(line)
                or (i > 0 and MUTATION_CONTEXT_RE.search(code_lines[i - 1]))
                or (i > 1 and MUTATION_CONTEXT_RE.search(code_lines[i - 2]))
            ):
                continue
            if FILTER_OPERATOR_RE.search(line):
                continue
            candidates.append((KIND_QUOTED, m.group(1)))
        for set_m in SQL_SET_KEYWORD_RE.finditer(line):
            tail = line[set_m.end():]
            semi = tail.find(";")
            if semi != -1:
                tail = tail[:semi]
            for fm in SQL_FIELD_RE.finditer(tail):
                candidates.append((KIND_SQL, fm.group(1)))
        if not candidates:
            continue
        symbol = _enclosing_symbol(code_lines, i)
        snippet = raw_lines[i].strip()[:200]
        for kind, token in candidates:
            sites.append(WriteSite(
                file=path, line=i + 1, text=snippet, symbol=symbol,
                is_test=is_test, kind=kind, token=token,
            ))
    return sites


def match_writers(sites: list[WriteSite], invariant: SingleWriterInvariant) -> list[Writer]:
    """Claim: query-time filter of field-agnostic ``sites`` against a single
    invariant's controlled fields + sanctioned writers. Mirrors the original
    interleaved scan exactly — including "one write record per (line,
    invariant), first controlled-field-form wins" — but now as a pure function
    of pre-emitted sites, with no filesystem access.
    """
    by_line: dict[tuple[str, int], list[WriteSite]] = defaultdict(list)
    for s in sites:
        by_line[(s.file, s.line)].append(s)

    writers: list[Writer] = []
    for (file, line), line_sites in by_line.items():
        for fpath, tok in _distinct_field_forms(invariant):
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


def scan_writers(
    source_root: str | Path,
    invariants: list[SingleWriterInvariant],
    exclude: list[str] | None = None,
    only_files: set[str] | None = None,
) -> list[Writer]:
    """Find every writer of every controlled field across the repo: emit
    field-agnostic write sites per file, then claim them against each
    invariant. ``only_files`` (repo-relative paths), when given, restricts the
    walk to that set instead of the whole tree — used by ``--changed-since``.
    """
    root = Path(source_root)
    exclude = exclude or []
    writers: list[Writer] = []
    if not root.exists() or not invariants:
        return writers

    if only_files is not None:
        candidates = sorted(only_files)
    else:
        # walk_source_files prunes submodules/nested git checkouts, keeping the
        # full scan's file universe identical to the manifest's (--changed-since).
        candidates = walk_source_files(root)

    for rel in candidates:
        if Path(rel).suffix not in SOURCE_EXTS:
            continue
        if any(part in SKIP_PARTS for part in Path(rel).parts):
            continue
        if _excluded(rel, exclude):
            continue
        path = root / rel
        try:
            text = path.read_text()
        except OSError:
            continue
        sites = emit_write_sites(rel, text)
        if not sites:
            continue
        # Claim per invariant, then merge preserving the ORIGINAL ordering: line
        # ascending first, invariant list-order second (the original scan looped
        # "for line: for invariant", not "for invariant: for line" — a file with
        # hits for multiple invariants at interleaved lines must come out in line
        # order, not grouped by invariant).
        tagged: list[tuple[int, Writer]] = []
        for idx, inv in enumerate(invariants):
            for w in match_writers(sites, inv):
                tagged.append((idx, w))
        tagged.sort(key=lambda t: (t[1].line, t[0]))
        writers.extend(w for _, w in tagged)
    return writers


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
    walk, reused to build a manifest whose keys are exactly the files that walk
    would visit (see ``chief_wiggum.manifest``)."""
    p = Path(rel)
    if p.suffix not in SOURCE_EXTS:
        return False
    if any(part in SKIP_PARTS for part in p.parts):
        return False
    return True


def _scanner_version() -> str:
    """Hash-derived ``--scanner-version``: the source of this module plus its
    ``chief_wiggum`` dependencies. No hand-bumped constant to forget."""
    here = Path(__file__).resolve()
    cw_dir = here.parent / "chief_wiggum"
    return scanner_version(here, cw_dir / "manifest.py", cw_dir / "hashing.py")


# --- top-level check --------------------------------------------------------


def check(
    epic_dir: str | Path,
    source_root: str | Path | None = None,
    exclude: list[str] | None = None,
    changed_since: str | None = None,
) -> SingleWriterReport:
    report = SingleWriterReport()
    invariants, malformed = collect_invariants(epic_dir)
    report.invariants = [inv.to_dict() for inv in invariants]
    report.malformed = malformed

    if not invariants:
        report.warnings.append(
            "no single-write-path invariants found (no controls_field/sanctioned_writers "
            "metadata); nothing to check"
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
            # gate, which must see the whole repo to be authoritative.
            only_files = changed_paths(source_root, changed_since, predicate=_file_predicate)
        writers = scan_writers(source_root, invariants, exclude=scan_exclude, only_files=only_files)
        report.writers = [w.to_dict() for w in writers]
        report.violations = [w.to_dict() for w in writers if not w.sanctioned]
        # Surface any invariant whose controlled field has NO writer at all — the
        # sanctioned path may be missing/misnamed (a soft warning, not a violation).
        # Skipped under --changed-since: a ticket-scoped scan is EXPECTED to miss
        # unrelated invariants' writers, so this warning would just be noise.
        if not changed_since:
            written_ids = {w.invariant_id for w in writers}
            for inv in invariants:
                if inv.id not in written_ids:
                    report.warnings.append(
                        f"{inv.id}: no writer found for {inv.controls_field} — "
                        f"sanctioned writer(s) {inv.sanctioned_writers} may be missing or misnamed"
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
        f"Writers found: {c['writers']}  |  Violations: {c['violations']}  |  Malformed metadata: {c['malformed']}",
        "",
        f"- Soundness (metadata well-formed): {'OK' if report.soundness_ok else 'FINDINGS'}",
        f"- Coverage (no unsanctioned writer): {'OK' if report.coverage_ok else 'FINDINGS'}",
    ]
    if report.malformed:
        lines += ["", "## Malformed metadata", ""]
        lines += [f"- {m['id']} ({m['source']}): {m['reason']}" for m in report.malformed]
    if report.violations:
        lines += ["", "## Unsanctioned writers (single-write-path violations)", ""]
        for v in report.violations:
            sym = f" in {v['symbol']}()" if v.get("symbol") else ""
            lines.append(
                f"- {v['invariant_id']} field `{v['field']}` written at "
                f"{v['file']}:{v['line']}{sym}"
            )
            lines.append(f"    {v['text']}")
    if report.writers and not report.violations:
        lines += ["", "## Sanctioned writers", ""]
        for w in report.writers:
            sym = f" in {w['symbol']}()" if w.get("symbol") else ""
            tag = " [test]" if w.get("is_test") else ""
            lines.append(f"- {w['invariant_id']} `{w['field']}` at {w['file']}:{w['line']}{sym}{tag}")
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
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    if args.scanner_version:
        print(_scanner_version())
        return 0

    if not args.epic_dir:
        print("Error: epic_dir is required unless --scanner-version is given", file=sys.stderr)
        return 2

    if not Path(args.epic_dir).exists():
        print(f"Error: epic dir not found: {args.epic_dir}", file=sys.stderr)
        return 2

    try:
        report = check(args.epic_dir, args.source, exclude=args.exclude, changed_since=args.changed_since)
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
        import os
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

    if args.gate == "soundness" and not report.soundness_ok:
        return 1
    if args.gate == "coverage" and not report.coverage_ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
