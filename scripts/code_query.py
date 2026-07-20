#!/usr/bin/env python3
"""Agent-facing architecture knowledge CLI (#159) — "gnosis" phase 1, live-scan.

Agents (and workflows) re-derive the architecture every session — N greps plus a
full context-load of `contracts.md`/`state-machines.md`/`invariants.md`/`adr.md` —
instead of asking one structural question. This is that one question, answered
from the epic artifacts plus code annotations.

**The two-plane invariant (load-bearing — do not cache Plane A).**

- **Plane A — epic knowledge**: `contracts.json`, `state-machines.json`,
  `transition-map.json`, `ui-spec.json`, prose invariants/ADR. Read LIVE on every
  query, never cached or re-serialized here. This module is a **locator**, not a
  content store: it returns stable IDs and `file:line` handles, never paraphrased
  contract bodies — callers that want the actual text call `show`.
- **Plane B — per-file code emissions**: `@cw-trace` sites (`check_traceability`),
  candidate writer sites (`check_single_writer`). The only cacheable plane
  (caching is a later issue) — phase 1 here is live-scan, exactly like the two
  checkers this module builds on.

Every claim (orphans, coverage, writer verdicts, artifact bindings) is computed
FRESH by joining Plane B onto Plane A at query time — never persisted.

**Artifact ingestion is first-class.** `contracts.json` operations
(method+path+conditions+error_cases+invariants_touched), `transition-map.json`
(transition -> code_locations, status, undocumented/drift), `ui-spec.json` +
`docs/design/design.json` (component/page -> route, tokens, auth), invariant
substructure (scope, applies_to_states, category, controls_field), and
`derived_from` provenance on any of the above are all parsed structurally — not
just scraped for stable IDs. `orient` binds by ARTIFACT as well as annotation
(operation path, ui-spec route, transition-map `code_locations`) so an
un-annotated handler still gets a real answer.

**Never serve unknown as empty.** A query against a path that cannot be read
(doesn't exist under the repo root) is reported as *unscanned* — a warning plus
an explicit "unscanned" summary — never silently rendered as the same empty
`facts: []` a genuinely-scanned-and-clean file gets. Absence of knowledge and
proof of absence are different answers.

**Response envelope**: every verb returns
``{summary, facts, omitted, cursor, warnings, provenance}``. `facts` is capped
(default ~40, see `--limit`) with `cursor` to page further; `omitted` is the
count beyond the returned window. Ranking (see `_rank_key`): exact ID/path hits
first, then violations/unsanctioned findings, then proximity (same file >
package/dir > epic > other), then prod-before-tests (inverted for `verifies`,
where the test/probe/policy/telemetry side IS the point). Each fact carries its
own `provenance` (`blob_sha`, `dirty`, `from_cache: false` — phase 1 never
caches). The envelope's top-level `provenance` is query-level: repo root, epics
scanned, scanner version.

Verbs (phase 1): `orient`, `governs`, `writers`, `guards`, `verifies`,
`annotations`, `trace`, `contract`, `state`, `show`. See `docs/code-query.md`.

Explicitly out of phase 1: a persisted cache, tree-sitter/symbol outlines,
sqlite, any new annotation convention, a `map` verb beyond module level.

Exit codes: 0 = ok (including a genuinely-empty or unscanned answer),
2 = usage error (bad repo root, unknown verb).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_single_writer  # noqa: E402
import check_traceability  # noqa: E402
from chief_wiggum.hashing import scanner_version  # noqa: E402
from chief_wiggum.trace_ids import DEFINE_RE, ID_KINDS  # noqa: E402

DEFAULT_LIMIT = 40

MODEL_FILES = ("contracts.json", "state-machines.json", "transition-map.json", "ui-spec.json")

_ID_KIND_RE = re.compile(rf"^(?:{'|'.join(ID_KINDS)})-", re.IGNORECASE)
# Extensions that make an argument "path-like" rather than a dotted field name
# (e.g. `order.status` must NOT be mistaken for a path just because it has a
# dot in it — `Path(...).suffix` membership in this set is the actual test).
_PATH_LIKE_EXTS = check_single_writer.SOURCE_EXTS | check_traceability.SOURCE_EXTS | {".md", ".json"}
_METHOD_PATH_RE = re.compile(
    r"^(GET|POST|PUT|PATCH|DELETE)\s+(\S+)$", re.IGNORECASE
)
_PARAM_RE = re.compile(r"(:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\}|\[[A-Za-z_.]+\])")


# --- Plane A: epic artifact discovery (read live, never cached across calls) --


@dataclass
class Epic:
    """One epic's Plane-A artifacts, loaded live for this query."""

    slug: str
    dir: Path
    models: dict[str, dict] = field(default_factory=dict)  # filename -> parsed JSON
    defined: dict[str, tuple[str, int]] = field(default_factory=dict)  # id -> (rel, line)
    sw_invariants: list = field(default_factory=list)  # check_single_writer.SingleWriterInvariant
    sw_malformed: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def statement_for(self, node_id: str) -> str:
        """Best-effort one-line prose statement for a declared ID (locator, not a
        content dump): the nearest bold-label line's trailing text, if any."""
        rel, line = self.defined.get(node_id, (None, None))
        if rel is None:
            return ""
        try:
            text = (self.dir / rel).read_text()
        except OSError:
            return ""
        lines = text.splitlines()
        if not (1 <= line <= len(lines)):
            return ""
        raw = lines[line - 1].strip()
        # Strip markdown heading/bold markers; keep the trailing prose.
        raw = re.sub(r"^#{1,6}\s*", "", raw)
        raw = re.sub(r"^\*\*\s*", "", raw)
        raw = raw.split("**", 1)[-1] if "**" in raw else raw
        return raw.strip(" :*")


def _locate_definitions(epic_dir: Path) -> dict[str, tuple[str, int]]:
    """Every declared stable ID in this epic's docs, with its `(rel_file, line)` —
    unlike `check_traceability.extract_defined_ids`, this keeps line numbers
    (needed by `show`'s dereference)."""
    out: dict[str, tuple[str, int]] = {}
    if not epic_dir.exists():
        return out
    for path in sorted(epic_dir.rglob("*")):
        if path.suffix not in (".md", ".json") or not path.is_file():
            continue
        rel = str(path.relative_to(epic_dir))
        try:
            text = path.read_text()
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            for m in DEFINE_RE.finditer(line):
                nid = check_traceability.canonical_id(m.group(1))
                out.setdefault(nid, (rel, i))
    return out


def discover_epics(repo_root: Path, epic: str | None = None) -> list[Epic]:
    """Discover epics under `docs/epics/*` (or just `epic` if given). Plane A is
    parsed fresh every call — never memoized across queries."""
    epics_root = Path(repo_root) / "docs" / "epics"
    if not epics_root.is_dir():
        return []
    if epic:
        slugs = [epic] if (epics_root / epic).is_dir() else []
    else:
        slugs = sorted(p.name for p in epics_root.iterdir() if p.is_dir())
    out: list[Epic] = []
    for slug in slugs:
        edir = epics_root / slug
        e = Epic(slug=slug, dir=edir)
        models_dir = edir / "models"
        for name in MODEL_FILES:
            p = models_dir / name
            if p.is_file():
                try:
                    e.models[name] = json.loads(p.read_text())
                except (OSError, json.JSONDecodeError) as exc:
                    e.warnings.append(f"malformed {name}: {exc}")
        e.defined = _locate_definitions(edir)
        try:
            invs, malformed = check_single_writer.collect_invariants(edir)
            e.sw_invariants, e.sw_malformed = invs, malformed
        except Exception as exc:  # noqa: BLE001 — never let one epic's bad data abort discovery
            e.warnings.append(f"single-writer metadata parse failed: {exc}")
        out.append(e)
    return out


def load_design(repo_root: Path) -> dict | None:
    p = Path(repo_root) / "docs" / "design" / "design.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# --- Facts, ranking, and the response envelope --------------------------------


@dataclass
class Fact:
    kind: str
    id: str | None
    statement: str
    handle: str
    epic: str | None
    extra: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    # Ranking hints — never serialized.
    exact: bool = False
    violation: bool = False
    proximity: int = 3  # 0 same file, 1 same dir/package, 2 same epic, 3 other
    prod: bool = True  # True = code/prod-like source; False = test/probe/policy/telemetry

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "id": self.id, "statement": self.statement,
             "handle": self.handle, "epic": self.epic}
        d.update(self.extra)
        d["provenance"] = self.provenance
        return d


# Leading rank-key element (CTR-fh-052/053, INV-fh-007/012): direct facts
# always sort before inferred, which always sort before measured. `measured`
# is forward-compat for the #187 hotspot tier — no producer of `measured`
# facts exists in this ticket, only the tier, so #187 doesn't have to touch
# ranking again.
_RELATION_TIER = {"direct": 0, "inferred": 1, "measured": 2}


def _relation_tier(f: Fact) -> int:
    """The leading `_rank_key` element. Facts that predate the `relation` tag
    (e.g. `writer` facts, or `field_contract` facts from `governs <field>`
    mode, neither of which set `extra["relation"]`) fall back to their
    `exact` flag — exactly how they already sorted before this key was added,
    so this is additive for verbs that never emit `inferred`/`measured`
    facts."""
    relation = f.extra.get("relation")
    if relation in _RELATION_TIER:
        return _RELATION_TIER[relation]
    return 0 if f.exact else 1


def _rank_key(f: Fact, verb: str) -> tuple:
    """
    @cw-trace guards CTR-fh-052 CTR-fh-053 INV-fh-007
    """
    prod_rank = 0 if f.prod else 1
    if verb == "verifies":
        prod_rank = 1 - prod_rank  # tests-first: that IS the point of `verifies`
    return (_relation_tier(f), 0 if f.exact else 1, 0 if f.violation else 1, f.proximity, prod_rank)


def rank_facts(facts: list[Fact], verb: str) -> list[Fact]:
    # Stable sort: ties keep their original (discovery) order.
    return sorted(facts, key=lambda f: _rank_key(f, verb))


def build_envelope(
    facts: list[Fact],
    *,
    verb: str,
    summary: str,
    warnings: list[str],
    query_provenance: dict,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict:
    ranked = rank_facts(facts, verb)
    try:
        start = int(cursor) if cursor else 0
    except ValueError:
        start = 0
    start = max(0, min(start, len(ranked)))
    window = ranked[start:start + limit]
    next_start = start + len(window)
    next_cursor = str(next_start) if next_start < len(ranked) else None
    omitted = len(ranked) - next_start
    return {
        "summary": summary,
        "facts": [f.to_dict() for f in window],
        "omitted": omitted,
        "cursor": next_cursor,
        "warnings": warnings,
        "provenance": query_provenance,
    }


def _query_provenance(repo_root: Path, epics: list[Epic]) -> dict:
    return {
        "repo": str(repo_root),
        "epics": [e.slug for e in epics],
        "scanner_version": _scanner_version(),
    }


def _file_provenance(repo_root: Path, rel: str) -> dict:
    """Per-fact provenance: current blob hash + dirty flag. Degrades to `None`s
    (never raises) when the path isn't a file or git isn't available — the
    fact itself is still returned, just without a hash lineage."""
    root = Path(repo_root)
    full = root / rel
    blob_sha: str | None = None
    dirty: bool | None = None
    try:
        if full.is_file():
            r = subprocess.run(
                ["git", "hash-object", rel], cwd=str(root), capture_output=True, text=True, check=True
            )
            blob_sha = r.stdout.strip()
        st = subprocess.run(
            ["git", "status", "--porcelain", "--", rel], cwd=str(root), capture_output=True, text=True, check=True
        )
        dirty = bool(st.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        pass
    return {"blob_sha": blob_sha, "dirty": dirty, "from_cache": False}


def _scanner_version() -> str:
    here = Path(__file__).resolve()
    cw_dir = here.parent / "chief_wiggum"
    # check_single_writer.py / check_traceability.py are hash inputs because
    # their EMISSIONS define this tool's facts (writer sites, annotation
    # sites): a change to either scanner's detection logic changes what a
    # query returns, so it must change this version too. Since #162 the actual
    # emission logic lives in chief_wiggum.write_emission / trace_emission
    # (with the extension universe in chief_wiggum.languages) — those are
    # inputs for the same reason.
    return scanner_version(
        here,
        here.parent / "check_single_writer.py",
        here.parent / "check_traceability.py",
        cw_dir / "trace_ids.py", cw_dir / "annotations.py",
        cw_dir / "trace_emission.py", cw_dir / "write_emission.py",
        cw_dir / "languages.py",
        cw_dir / "manifest.py", cw_dir / "hashing.py",
    )


def _unscanned_envelope(reason: str, *, query_provenance: dict) -> dict:
    return {
        "summary": f"unscanned: {reason}",
        "facts": [],
        "omitted": 0,
        "cursor": None,
        "warnings": [f"unscanned — {reason}"],
        "provenance": query_provenance,
    }


# --- path / route matching helpers (artifact-derived binding) -----------------


def _norm(p: str) -> str:
    return p.replace("\\", "/").lstrip("./")


def _same_file(loc_file: str, rel: str) -> bool:
    a, b = _norm(loc_file), _norm(rel)
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


_WORD_RE = re.compile(r"[a-z0-9]+")
# Below this length, a word must match EXACTLY — otherwise a short token like
# "ui" would substring-match inside an unrelated longer word (e.g. hides
# inside "builder"). Validated against a real repo (dogeared-coach): the naive
# substring version bound `ui/src/App.tsx` to a `course-builder` ui-spec page
# purely because "ui" is a substring of "builder" — a false artifact binding
# that undermines the whole point of `orient`. At/above this length, a plain
# substring check is still allowed so `order`/`orders` (singular vs plural)
# keep matching.
_MIN_FUZZY_WORD_LEN = 4
# Ubiquitous path boilerplate that carries no entity-identifying signal on its
# own — excluded from both sides so two unrelated files sharing an "/api/v1/"
# prefix (or a stray "id"/"ids" param-ish token) don't look bound.
_GENERIC_PATH_WORDS = {"api", "v1", "v2", "v3", "id", "ids"}


def _literal_words(pattern: str) -> set[str]:
    """Word-tokenized, non-parameter, non-generic words in a route/operation-path
    template (`/api/v1/orders/:id/confirm` -> {"orders", "confirm"})."""
    segs = [s for s in pattern.strip("/").split("/") if s]
    words: set[str] = set()
    for s in segs:
        if s.startswith(":") or (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")):
            continue
        words |= set(_WORD_RE.findall(s.lower()))
    return words - _GENERIC_PATH_WORDS


def _path_words(rel: str) -> set[str]:
    """Word-tokenized directory + filename-stem words for a repo-relative path."""
    p = Path(_norm(rel))
    text = "/".join((*p.parent.parts, p.stem))
    return set(_WORD_RE.findall(text.lower())) - _GENERIC_PATH_WORDS


def _fuzzy_word_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) < _MIN_FUZZY_WORD_LEN or len(b) < _MIN_FUZZY_WORD_LEN:
        return False  # short words must match exactly (see _MIN_FUZZY_WORD_LEN)
    return a in b or b in a  # tolerate simple stemming (order/orders, confirm/confirmed)


# --- corpus-derived word specificity (CTR-fh-050/051, INV-fh-012) -------------
#
# #185: the all-words guard above (kept — see ERROR CASES) still over-matches
# when a pattern's literal words are ALL common: an entity name that recurs
# across dozens of an epic's own operations (dogeared-coach's "provider" was
# the real-world trigger — a file named `auth-provider.tsx` word-matched ~30
# unrelated provider operations, because many of them reduce to the bare word
# "providers" once params are stripped). The fix stays lexical and stdlib-only
# (tree-sitter/symbol resolution is explicitly out of phase 1, per #159):
# weight each literal word by how common it is across the EPIC'S OWN corpus of
# operation-paths + ui routes — no external corpus, no network, so the same
# epic artifacts always yield the same weights — and require at least one
# word that ISN'T ubiquitous before accepting an all-words match.

# A word present in MORE than this fraction of the epic's own operation-paths/
# routes carries zero binding weight — it's domain boilerplate for this epic,
# not a signal that a specific file is about THIS specific operation. This is
# a deliberately generous majority-ish line, not a tight percentile: it must
# still treat a moderately-shared word (a handful of an epic's operations
# mentioning "orders") as specific enough to bind, while catching a word a
# true plurality of operations share (the "provider" case). Tuned and
# revalidated against a real, previously-shipped repo (see docs/code-query.md).
_MAX_COMMON_WORD_DF = 0.4
# Below this many corpus documents, document frequency is statistical noise
# and the DF bar is bypassed entirely (every word counts as specific — the
# pre-#185 all-words behavior). Without this floor, a one-operation epic
# rejects ITSELF: its own path's words are the whole corpus (df=1.0 > 0.4),
# so `orient` would return nothing for the file that operation genuinely
# governs; a two-document epic (an operation plus its UI route sharing the
# entity word) self-blocks the same way at df=1.0. Threshold interaction:
# at total_docs >= 5, a word must appear in >40% of documents to be common —
# i.e. at least 3 of 5 — so a genuinely-shared entity word still needs real
# recurrence before it loses binding weight, while the dogeared-coach
# "provider" case (5 of 8 documents) stays blocked.
_MIN_CORPUS_DOCS = 5


def _epic_path_documents(epic: Epic) -> list[frozenset[str]]:
    """One 'document' (word set) per `contracts.json` operation path and per
    `ui-spec.json` page route declared by THIS epic — the closed corpus that
    word document-frequency is computed over. Deliberately scoped to a single
    epic's own artifacts: an operation is only "common" relative to what this
    epic itself declares, so the metric can't be diluted or gamed by an
    unrelated epic's vocabulary."""
    docs: list[frozenset[str]] = []
    contracts = epic.models.get("contracts.json")
    if contracts:
        for entity in contracts.get("entities", []):
            for op in entity.get("operations", []):
                words = _literal_words(op.get("path", ""))
                if words:
                    docs.append(frozenset(words))
    ui_spec = epic.models.get("ui-spec.json")
    if ui_spec:
        for route in (ui_spec.get("pages") or {}):
            words = _literal_words(route)
            if words:
                docs.append(frozenset(words))
    return docs


def _word_document_frequency(docs: list[frozenset[str]]) -> dict[str, float]:
    """word -> fraction of `docs` that contain it, or `{}` when the corpus has
    fewer than `_MIN_CORPUS_DOCS` documents — DF over a tiny corpus is noise,
    and returning an empty map makes every `_has_specific_word` lookup miss
    (df=0.0, maximally specific), i.e. small epics keep the pre-#185
    all-words-only behavior instead of self-blocking (a one-operation epic's
    own words are the entire corpus at df=1.0). Deterministic across runs
    (CTR-fh-051): built by iterating `docs` in the epic artifacts' own JSON
    list order (stable across runs/platforms), and dict iteration order in
    CPython is insertion order, not hash order — so this never depends on
    Python's per-process randomized string hashing, even though the `frozenset`
    documents themselves are hash-ordered internally. Nothing here reads a
    set's iteration order; only membership/count, which hashing doesn't
    affect."""
    total = len(docs)
    if total < _MIN_CORPUS_DOCS:
        return {}
    counts: dict[str, int] = {}
    for doc in docs:
        for w in doc:
            counts[w] = counts.get(w, 0) + 1
    return {w: c / total for w, c in counts.items()}


def _has_specific_word(literals: set[str], doc_freq: dict[str, float]) -> bool:
    """At least one of `literals` must clear the specificity bar (CTR-fh-050).
    A word absent from the map is a lookup miss treated as maximally specific
    (df=0.0) — which is also how the small-corpus bypass arrives here:
    `_word_document_frequency` returns an empty map below `_MIN_CORPUS_DOCS`,
    so every word of a small epic misses and the all-words guard alone
    decides, exactly as before #185."""
    return any(doc_freq.get(w, 0.0) <= _MAX_COMMON_WORD_DF for w in literals)


def _path_matches_literal_segments(pattern: str, rel: str, doc_freq: dict[str, float]) -> bool:
    """Best-effort, inferred binding: EVERY non-generic literal word of
    `pattern` (a ui-spec route or contracts.json operation path) must
    word-match the target file's own path, AND at least one of those literal
    words must be specific (not corpus-common per `_has_specific_word`).
    All-words-but-not-all-common keeps precision on two fronts:
    `/api/v1/orders/:id/confirm` requires both "orders" AND "confirm" among
    the file's path words, so `ui/orders/page.tsx` does not inherit the
    confirm operation just for living in an `orders/` directory (all-words);
    and a file that shares ONLY a ubiquitous entity word with a bare
    single-word operation (e.g. "providers") does not bind either, even
    though that one word technically satisfies all-words on its own
    (specificity). Heuristic by design (no file-level binding field exists in
    the schemas) — labeled `inferred`, never `exact`, in the facts this
    produces.

    @cw-trace guards CTR-fh-050 CTR-fh-051 INV-fh-012
    """
    literals = _literal_words(pattern)
    if not literals:
        return False
    if not _has_specific_word(literals, doc_freq):
        return False
    file_words = _path_words(rel)
    return all(any(_fuzzy_word_match(lw, fw) for fw in file_words) for lw in literals)


def _path_to_regex(template: str) -> re.Pattern:
    parts, last = [], 0
    for m in _PARAM_RE.finditer(template):
        parts.append(re.escape(template[last:m.start()]))
        parts.append(r"[^/]+")
        last = m.end()
    parts.append(re.escape(template[last:]))
    return re.compile("^" + "".join(parts) + "$")


def _operation_path_matches(op_path: str, concrete: str) -> bool:
    if op_path == concrete:
        return True
    try:
        return bool(_path_to_regex(op_path).fullmatch(concrete))
    except re.error:
        return False


# --- verb: orient ---------------------------------------------------------------


def _invariant_statement(inv: check_single_writer.SingleWriterInvariant) -> str:
    return inv.description or f"single write path for {', '.join(inv.controls_field)}"


def _sm_invariants_for_states(sm: dict, states: set[str]) -> list[dict]:
    out = []
    for inv in sm.get("invariants", []) or []:
        scope = inv.get("scope", "global")
        if scope == "global" or (set(inv.get("applies_to_states", [])) & states):
            out.append(inv)
    return out


def governing_facts_for_file(repo_root: Path, rel: str, epics: list[Epic]) -> list[Fact]:
    """Shared computation behind `orient` and `governs <path>`: every fact that
    governs `rel`, tagged `exact` (direct annotation / precise code_location
    match) or not (artifact-derived proximity match)."""
    root = Path(repo_root)
    full = root / rel
    text = full.read_text()
    suffix = full.suffix
    direct_anns = check_traceability.emit_source_annotations(rel, text, suffix)
    prov = _file_provenance(root, rel)

    facts: list[Fact] = []
    for epic in epics:
        # Corpus-derived word specificity (CTR-fh-050/051): computed once per
        # epic, live from THIS epic's own artifacts, and reused by both
        # inferred-binding call sites (c)/(d) below.
        doc_freq = _word_document_frequency(_epic_path_documents(epic))

        # (a) Direct: @cw-trace annotations in THIS file targeting a defined ID.
        for ann in direct_anns:
            if ann.target not in epic.defined:
                continue
            facts.append(Fact(
                kind="invariant" if ann.target.startswith("INV-") else "contract",
                id=ann.target,
                statement=epic.statement_for(ann.target) or f"{ann.verb} target",
                handle=f"{rel}:{ann.line}",
                epic=epic.slug,
                extra={"verb": ann.verb, "relation": "direct"},
                provenance=prov,
                exact=True,
                proximity=0,
                prod=(ann.verb != "verifies"),
            ))

        # (b) Artifact-derived, exact: transition-map code_locations bound to this file.
        tmap = epic.models.get("transition-map.json")
        if tmap:
            for entity in tmap.get("entities", []):
                for t in entity.get("transitions", []):
                    for loc in t.get("code_locations", []):
                        if not _same_file(loc.get("file", ""), rel):
                            continue
                        states = {t.get("from"), t.get("to")}
                        applicable_inv = _sm_invariants_for_states(
                            epic.models.get("state-machines.json", {}), states
                        )
                        facts.append(Fact(
                            kind="transition",
                            id=None,
                            statement=(
                                f"{entity['name']}: {t['from']} -> {t['to']} on "
                                f"{t['event']} ({t['status']})"
                            ),
                            handle=f"{rel}:{loc.get('line', 0)}",
                            epic=epic.slug,
                            extra={
                                "relation": "direct",
                                "entity": entity["name"],
                                "invariants": [i["id"] for i in applicable_inv],
                            },
                            provenance=prov,
                            exact=True,
                            proximity=0,
                        ))
                for u in entity.get("undocumented", []):
                    for loc in u.get("code_locations", []):
                        if not _same_file(loc.get("file", ""), rel):
                            continue
                        facts.append(Fact(
                            kind="transition_undocumented",
                            id=None,
                            statement=(
                                f"{entity['name']}: undocumented transition "
                                f"{u.get('from', '*')} -> {u['to']} — model or code drift"
                            ),
                            handle=f"{rel}:{loc.get('line', 0)}",
                            epic=epic.slug,
                            extra={"relation": "direct"},
                            provenance=prov,
                            exact=True,
                            violation=True,
                            proximity=0,
                        ))

        # (c) Artifact-derived, inferred: contracts.json operation path.
        contracts = epic.models.get("contracts.json")
        if contracts:
            for entity in contracts.get("entities", []):
                for op in entity.get("operations", []):
                    if not _path_matches_literal_segments(op.get("path", ""), rel, doc_freq):
                        continue
                    # Locator discipline (two-plane): counts + IDs only — the
                    # REQUIRES/ENSURES/error bodies stay in Plane A; deref the
                    # handle via `show` (or ask `contract`) for the one-liners.
                    facts.append(Fact(
                        kind="contract_operation",
                        id=None,
                        statement=f"{op['method']} {op['path']}: {op.get('name', '')}",
                        handle=f"docs/epics/{epic.slug}/models/contracts.json#{entity['name']}/{op.get('name')}",
                        epic=epic.slug,
                        extra={
                            "relation": "inferred",
                            "n_preconditions": len(op.get("preconditions", [])),
                            "n_postconditions": len(op.get("postconditions", [])),
                            "n_error_cases": len(op.get("error_cases", [])),
                            "state_transition": op.get("state_transition"),
                            "invariants_touched": op.get("invariants_touched", []),
                        },
                        provenance=prov,
                        exact=False,
                        proximity=1,
                    ))

        # (d) Artifact-derived, inferred: ui-spec page route / auth.
        ui_spec = epic.models.get("ui-spec.json")
        if ui_spec:
            for route, page in (ui_spec.get("pages") or {}).items():
                if not route or not _path_matches_literal_segments(route, rel, doc_freq):
                    continue
                facts.append(Fact(
                    kind="ui_component",
                    id=None,
                    statement=f"page {route} ({page.get('title', '')}) auth={page.get('auth', 'required')}",
                    handle=f"docs/epics/{epic.slug}/models/ui-spec.json#pages[{route}]",
                    epic=epic.slug,
                    extra={
                        "relation": "inferred",
                        "auth": page.get("auth", "required"),
                        "layout": page.get("layout"),
                        "design_refs": page.get("design_refs", []),
                    },
                    provenance=prov,
                    exact=False,
                    proximity=1,
                ))

        # (e) Single-write-path invariants: is this file a (sanctioned?) writer?
        for inv in epic.sw_invariants:
            sites = check_single_writer.emit_write_sites(rel, text)
            for w in check_single_writer.match_writers(sites, inv):
                facts.append(Fact(
                    kind="writer",
                    id=inv.id,
                    statement=(
                        f"{'UNSANCTIONED' if not w.sanctioned else 'sanctioned'} writer of "
                        f"{w.field} ({_invariant_statement(inv)})"
                    ),
                    handle=f"{rel}:{w.line}",
                    epic=epic.slug,
                    extra={"relation": "direct", **w.to_dict()},
                    provenance=prov,
                    exact=True,
                    violation=not w.sanctioned,
                    proximity=0,
                ))
    return facts


_HOTSPOTS_PATH = Path("docs") / "quality" / "hotspots.json"


def _load_hotspots(repo_root: Path) -> dict | None:
    """Read `docs/quality/hotspots.json` fresh (never cached — same live-scan
    discipline as everything else this module reads). Missing/unparsable is a
    silent `None`: the hotspot fact is advisory, never a hard dependency."""
    p = Path(repo_root) / _HOTSPOTS_PATH
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("hotspots"), list):
        return None
    return doc


def _hotspot_facts_for_file(repo_root: Path, rel: str) -> list[Fact]:
    """The #187 `measured` fact tier: EXACT file-path membership in
    `docs/quality/hotspots.json` ONLY — never `_path_matches_literal_segments`
    or any other lexical channel (INV-fh-007/012). A file gets a hotspot fact
    if it IS a top-decile entry itself, or is a `coupled_with` partner of one
    (still exact membership: the partner path is read verbatim off that
    entry, never lexically re-derived). Provenance carries the generating
    `git_sha` so a stale artifact is visibly attributable.

    @cw-trace guards CTR-fh-033 CTR-fh-034 INV-fh-007 INV-fh-012
    """
    doc = _load_hotspots(repo_root)
    if not doc:
        return []
    sha = doc.get("git_sha")
    authority = doc.get("authority", "")
    by_file = {h.get("file"): h for h in doc["hotspots"] if isinstance(h, dict) and h.get("file")}

    own = by_file.get(rel)
    facts: list[Fact] = []
    if own is not None and own.get("decile") == 10:
        facts.append(Fact(
            kind="hotspot",
            id=None,
            statement=(
                f"top-decile hotspot (score={own.get('score')}, "
                f"churn={own.get('churn')}, complexity={own.get('complexity')}): {authority}"
            ),
            handle=f"{_HOTSPOTS_PATH}#hotspots[{rel}]",
            epic=None,
            extra={
                "relation": "measured",
                "decile": own.get("decile"),
                "score": own.get("score"),
                "churn_score": own.get("norm_churn"),
                "complexity_score": own.get("norm_complexity"),
                "coupled_with": own.get("coupled_with", []),
                "trend": own.get("trend"),
            },
            provenance={
                **_file_provenance(repo_root, rel),
                "derived": True,
                "generating_sha": sha,
            },
            exact=True,
            proximity=0,
        ))
    else:
        # Coupled-partner path: rel is not itself a top-decile hotspot, but a
        # top-decile hotspot's coupled_with names it verbatim — still exact
        # membership, just on the OTHER record's field, never a lexical guess.
        for h in doc["hotspots"]:
            if not isinstance(h, dict) or h.get("decile") != 10:
                continue
            for partner in h.get("coupled_with") or []:
                if not isinstance(partner, dict) or partner.get("file") != rel:
                    continue
                facts.append(Fact(
                    kind="hotspot",
                    id=None,
                    statement=(
                        f"coupled with top-decile hotspot {h.get('file')} "
                        f"(confidence={partner.get('confidence')}, "
                        f"co_changes={partner.get('co_changes')}): {authority}"
                    ),
                    handle=f"{_HOTSPOTS_PATH}#hotspots[{h.get('file')}]",
                    epic=None,
                    # coupling.confidence is single-write-path (INV-fh-001,
                    # sanctioned_writers: scripts/quality/process.py) — relay
                    # the already-computed sub-fields off `partner` rather than
                    # re-declaring the field with a fresh dict-literal key.
                    extra={
                        "relation": "measured",
                        "coupled_hotspot": h.get("file"),
                        **{k: partner.get(k) for k in ("confidence", "co_changes")},
                    },
                    provenance={
                        **_file_provenance(repo_root, rel),
                        "derived": True,
                        "generating_sha": sha,
                    },
                    exact=True,
                    proximity=0,
                ))
                break  # one fact per coupled owning hotspot is plenty
    return facts


def cmd_orient(repo_root: Path, path: str, epic: str | None, limit: int = DEFAULT_LIMIT, cursor: str | None = None) -> dict:
    epics = discover_epics(repo_root, epic)
    rel = _norm(path)
    full = Path(repo_root) / rel
    if not full.is_file():
        return _unscanned_envelope(
            f"{rel} not found under {repo_root}",
            query_provenance=_query_provenance(repo_root, epics),
        )
    facts = governing_facts_for_file(repo_root, rel, epics)
    facts += _hotspot_facts_for_file(repo_root, rel)
    warnings = [w for e in epics for w in e.warnings]
    if not epics:
        warnings.append("no docs/epics/* found — orienting on annotations/design only would need epic context")
    summary = (
        f"orient: {len(facts)} governing fact(s) for {rel} across {len(epics)} epic(s)"
        if facts else f"orient: scanned, nothing governs {rel} (no annotation or artifact binding found)"
    )
    return build_envelope(
        facts, verb="orient", summary=summary, warnings=warnings,
        query_provenance=_query_provenance(repo_root, epics),
        limit=limit, cursor=cursor,
    )


# --- verb: governs --------------------------------------------------------------


def cmd_governs(repo_root: Path, target: str, epic: str | None, limit: int = DEFAULT_LIMIT, cursor: str | None = None) -> dict:
    epics = discover_epics(repo_root, epic)
    rel = _norm(target)
    full = Path(repo_root) / rel
    looks_like_path = "/" in target or Path(target).suffix in _PATH_LIKE_EXTS
    if full.is_file():
        facts = governing_facts_for_file(repo_root, rel, epics)
        for f in facts:
            f.extra["relation"] = "direct" if f.exact else "inferred"
        warnings = [w for e in epics for w in e.warnings]
        summary = (
            f"governs: {len(facts)} fact(s) govern {rel}"
            if facts else f"governs: scanned, nothing governs {rel}"
        )
        return build_envelope(
            facts, verb="governs", summary=summary, warnings=warnings,
            query_provenance=_query_provenance(repo_root, epics),
            limit=limit, cursor=cursor,
        )
    if looks_like_path:
        return _unscanned_envelope(
            f"{rel} not found under {repo_root}",
            query_provenance=_query_provenance(repo_root, epics),
        )

    # Field-name mode.
    field_tok = target.split(".")[-1].strip().lower()
    facts: list[Fact] = []
    warnings: list[str] = []
    for epic_ctx in epics:
        matching = [i for i in epic_ctx.sw_invariants if field_tok in i.field_tokens()]
        if matching:
            writers = check_single_writer.scan_writers(repo_root, matching, exclude=[
                str(Path("docs") / "epics" / epic_ctx.slug)
            ])
            for w in writers:
                facts.append(Fact(
                    kind="writer",
                    id=w.invariant_id,
                    statement=f"{'UNSANCTIONED' if not w.sanctioned else 'sanctioned'} writer of {w.field}",
                    handle=f"{w.file}:{w.line}",
                    epic=epic_ctx.slug,
                    extra=w.to_dict(),
                    provenance=_file_provenance(repo_root, w.file),
                    exact=True,
                    violation=not w.sanctioned,
                    proximity=2,
                ))
        contracts = epic_ctx.models.get("contracts.json")
        if contracts:
            for entity in contracts.get("entities", []):
                for fld in entity.get("fields", []):
                    if fld.get("name", "").split(".")[-1].lower() != field_tok:
                        continue
                    facts.append(Fact(
                        kind="field_contract",
                        id=None,
                        statement=f"{entity['name']}.{fld['name']}: {fld.get('type', '')}",
                        handle=f"docs/epics/{epic_ctx.slug}/models/contracts.json#{entity['name']}/{fld['name']}",
                        epic=epic_ctx.slug,
                        extra={
                            "required": fld.get("required", "optional"),
                            "required_when": fld.get("required_when"),
                            "source_of_truth": fld.get("source_of_truth"),
                            "immutable": fld.get("immutable", False),
                        },
                        provenance={"blob_sha": None, "dirty": None, "from_cache": False},
                        exact=True,
                        proximity=2,
                    ))
        warnings.extend(epic_ctx.warnings)
    summary = (
        f"governs: {len(facts)} fact(s) for field '{field_tok}'"
        if facts else f"governs: scanned all epics, no single-write-path invariant or contract field named '{field_tok}'"
    )
    return build_envelope(
        facts, verb="governs", summary=summary, warnings=warnings,
        query_provenance=_query_provenance(repo_root, epics),
        limit=limit, cursor=cursor,
    )


# --- verb: writers ---------------------------------------------------------------


def cmd_writers(repo_root: Path, target: str, epic: str | None, limit: int = DEFAULT_LIMIT, cursor: str | None = None) -> dict:
    epics = discover_epics(repo_root, epic)
    canonical = check_traceability.canonical_id(target) if _ID_KIND_RE.match(target) else None
    field_tok = target.split(".")[-1].strip().lower()

    facts: list[Fact] = []
    warnings: list[str] = []
    matched_any_invariant = False
    for epic_ctx in epics:
        if canonical:
            matching = [i for i in epic_ctx.sw_invariants if i.id == canonical]
        else:
            matching = [i for i in epic_ctx.sw_invariants if field_tok in i.field_tokens()]
        if not matching:
            continue
        matched_any_invariant = True
        writers = check_single_writer.scan_writers(
            repo_root, matching, exclude=[str(Path("docs") / "epics" / epic_ctx.slug)]
        )
        found_ids = {w.invariant_id for w in writers}
        for w in writers:
            facts.append(Fact(
                kind="writer",
                id=w.invariant_id,
                statement=f"{'UNSANCTIONED' if not w.sanctioned else 'sanctioned'} writer of {w.field}",
                handle=f"{w.file}:{w.line}",
                epic=epic_ctx.slug,
                extra=w.to_dict(),
                provenance=_file_provenance(repo_root, w.file),
                exact=True,
                violation=not w.sanctioned,
                proximity=0,
            ))
        for inv in matching:
            if inv.id not in found_ids:
                warnings.append(
                    f"{inv.id}: no writer found for {inv.controls_field} in epic {epic_ctx.slug} — "
                    f"sanctioned writer(s) {inv.sanctioned_writers} may be missing or misnamed"
                )
        warnings.extend(epic_ctx.warnings)

    if not matched_any_invariant:
        # Distinguish "ID declared but not a single-write-path invariant" from
        # "ID/field not declared anywhere" — both are genuine empties (no path
        # to fail to scan; this is a pure data lookup), but the reason differs.
        if canonical:
            declared = any(canonical in e.defined for e in epics)
            reason = (
                f"{canonical} is declared but carries no controls_field/sanctioned_writers metadata"
                if declared else f"{canonical} is not declared in any epic artifact"
            )
        else:
            reason = f"'{field_tok}' is not declared as controls_field in any epic's single-write-path invariants"
        warnings.append(reason)

    summary = (
        f"writers: {len(facts)} writer(s) found for '{target}'"
        if facts else f"writers: scanned, no single-write-path writers found for '{target}'"
    )
    return build_envelope(
        facts, verb="writers", summary=summary, warnings=warnings,
        query_provenance=_query_provenance(repo_root, epics),
        limit=limit, cursor=cursor,
    )


# --- verbs: guards / verifies / annotations --------------------------------------


def _all_source_annotations(repo_root: Path) -> list:
    return check_traceability.scan_source(repo_root)


def _all_epic_annotations(epics: list[Epic]) -> list:
    out = []
    for e in epics:
        out.extend(check_traceability.scan_epic_annotations(e.dir))
    return out


def _owner_epic(epics: list[Epic], node_id: str) -> Epic | None:
    for e in epics:
        if node_id in e.defined:
            return e
    return None


def _annotation_fact(ann, epics: list[Epic], repo_root: Path, *, exact: bool = True) -> Fact:
    owner = _owner_epic(epics, ann.target)
    # emit_epic_annotations tags source_kind with the declaring ID's KIND (e.g.
    # "CTR") and ann.file is EPIC-relative; emit_source_annotations tags it with
    # code/test/probe/policy/telemetry and ann.file is REPO-relative. Normalize
    # both to a repo-relative handle so `show` and provenance always resolve.
    is_epic_doc = ann.source_kind in ID_KINDS
    handle_file = str(Path("docs") / "epics" / owner.slug / ann.file) if (is_epic_doc and owner) else ann.file
    prod = ann.source_kind not in ("test", "probe", "policy", "telemetry")
    return Fact(
        kind="annotation",
        id=ann.target,
        statement=f"{ann.verb} @ {handle_file}:{ann.line} ({ann.source_kind})",
        handle=f"{handle_file}:{ann.line}",
        epic=owner.slug if owner else None,
        extra={"verb": ann.verb, "source_kind": ann.source_kind},
        provenance=_file_provenance(repo_root, handle_file),
        exact=exact,
        proximity=0,
        prod=prod,
    )


def _annotations_for(
    repo_root: Path, target_id: str, epics: list[Epic], *, verbs: tuple[str, ...] | None = None,
) -> tuple[list, list[str]]:
    canonical = check_traceability.canonical_id(target_id)
    source_anns = _all_source_annotations(repo_root)
    epic_anns = _all_epic_annotations(epics)
    combined = source_anns + epic_anns
    matched = [a for a in combined if a.target == canonical and (verbs is None or a.verb in verbs)]
    warnings: list[str] = []
    if not any(canonical in e.defined for e in epics):
        warnings.append(f"{canonical} is not declared in any epic artifact (dangling reference, if any found)")
    return matched, warnings


def cmd_guards(repo_root: Path, ctr_id: str, epic: str | None, limit: int = DEFAULT_LIMIT, cursor: str | None = None) -> dict:
    epics = discover_epics(repo_root, epic)
    anns, warnings = _annotations_for(repo_root, ctr_id, epics, verbs=("guards", "ensures"))
    facts = [_annotation_fact(a, epics, repo_root) for a in anns]
    canonical = check_traceability.canonical_id(ctr_id)
    summary = (
        f"guards: {len(facts)} guard/ensures site(s) for {canonical}"
        if facts else f"guards: scanned, no guard/ensures annotation found for {canonical}"
    )
    return build_envelope(
        facts, verb="guards", summary=summary, warnings=warnings,
        query_provenance=_query_provenance(repo_root, epics),
        limit=limit, cursor=cursor,
    )


def cmd_verifies(repo_root: Path, ctr_id: str, epic: str | None, limit: int = DEFAULT_LIMIT, cursor: str | None = None) -> dict:
    epics = discover_epics(repo_root, epic)
    anns, warnings = _annotations_for(repo_root, ctr_id, epics, verbs=("verifies",))
    facts = [_annotation_fact(a, epics, repo_root) for a in anns]
    canonical = check_traceability.canonical_id(ctr_id)
    summary = (
        f"verifies: {len(facts)} test/verification site(s) for {canonical}"
        if facts else f"verifies: scanned, no verifies annotation found for {canonical}"
    )
    return build_envelope(
        facts, verb="verifies", summary=summary, warnings=warnings,
        query_provenance=_query_provenance(repo_root, epics),
        limit=limit, cursor=cursor,
    )


def cmd_annotations(repo_root: Path, node_id: str, epic: str | None, verb_filter: str | None, limit: int = DEFAULT_LIMIT, cursor: str | None = None) -> dict:
    epics = discover_epics(repo_root, epic)
    verbs = (verb_filter.lower(),) if verb_filter else None
    anns, warnings = _annotations_for(repo_root, node_id, epics, verbs=verbs)
    facts = [_annotation_fact(a, epics, repo_root) for a in anns]
    canonical = check_traceability.canonical_id(node_id)
    scope = f" (verb={verb_filter})" if verb_filter else ""
    summary = (
        f"annotations: {len(facts)} annotation(s) for {canonical}{scope}"
        if facts else f"annotations: scanned, no annotation found for {canonical}{scope}"
    )
    return build_envelope(
        facts, verb="annotations", summary=summary, warnings=warnings,
        query_provenance=_query_provenance(repo_root, epics),
        limit=limit, cursor=cursor,
    )


# --- verb: trace -------------------------------------------------------------------


def _find_derived_from(obj, target_id: str) -> list[dict] | None:
    """Recursively search a Plane-A JSON tree for the node declaring `target_id`
    and return its `derived_from` provenance list, if any."""
    if isinstance(obj, dict):
        node_id = obj.get("id")
        if isinstance(node_id, str) and check_traceability.canonical_id(node_id) == target_id:
            df = obj.get("derived_from")
            if df:
                return df
        for v in obj.values():
            found = _find_derived_from(v, target_id)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_derived_from(item, target_id)
            if found is not None:
                return found
    return None


def _find_derived_from_in_models(epic_ctx: Epic, target_id: str) -> tuple[str, list[dict]] | None:
    """(model filename, derived_from list) for the node declaring `target_id`,
    searched per model file so the resulting handle names the FILE that carries
    the provenance (a dereferenceable `file#ID` handle, not a directory)."""
    for name, model in epic_ctx.models.items():
        df = _find_derived_from(model, target_id)
        if df is not None:
            return name, df
    return None


def cmd_trace(repo_root: Path, node_id: str, epic: str | None, limit: int = DEFAULT_LIMIT, cursor: str | None = None) -> dict:
    epics = discover_epics(repo_root, epic)
    canonical = check_traceability.canonical_id(node_id)
    owner = _owner_epic(epics, canonical)
    kind = check_traceability.kind_of(canonical)

    epic_anns = _all_epic_annotations(epics)
    source_anns = _all_source_annotations(repo_root)

    facts: list[Fact] = []
    warnings: list[str] = []
    if owner is None:
        warnings.append(f"{canonical} is not declared in any epic artifact")

    contract_ids = {canonical}
    if kind == "BR":
        realizers = [a for a in epic_anns if a.verb == "realizes" and a.target == canonical and a.source_id]
        for a in realizers:
            contract_ids.add(a.source_id)
            facts.append(_annotation_fact(a, epics, repo_root))
    else:
        realizes = [a for a in epic_anns if a.verb == "realizes" and a.source_id == canonical]
        for a in realizes:
            facts.append(_annotation_fact(a, epics, repo_root))

    for a in source_anns:
        if a.verb in ("guards", "ensures", "verifies") and a.target in contract_ids:
            facts.append(_annotation_fact(a, epics, repo_root))

    if owner is not None:
        found = _find_derived_from_in_models(owner, canonical)
        if found:
            model_file, df = found
            for p in df:
                facts.append(Fact(
                    kind="derived_from",
                    id=canonical,
                    statement=f"{p.get('type')}: {p.get('ref')} — {p.get('description', '')}".strip(" —"),
                    handle=f"docs/epics/{owner.slug}/models/{model_file}#{canonical}",
                    epic=owner.slug,
                    extra=dict(p),
                    provenance={"blob_sha": None, "dirty": None, "from_cache": False},
                    exact=True,
                    proximity=0,
                ))

    summary = (
        f"trace: {len(facts)} link(s) in the BR->contract->code->test slice for {canonical}"
        if facts else f"trace: scanned, no links found for {canonical}"
    )
    return build_envelope(
        facts, verb="trace", summary=summary, warnings=warnings,
        query_provenance=_query_provenance(repo_root, epics),
        limit=limit, cursor=cursor,
    )


# --- verb: contract ----------------------------------------------------------------


def _condition_line(cond: dict) -> str:
    """One summary line for a contracts.json condition — `id: description` when
    the condition carries an id, bare description otherwise. Never the
    machine `expression` (that's Plane-A body; deref via `show`)."""
    desc = cond.get("description", "")
    cid = cond.get("id")
    return f"{cid}: {desc}" if cid else desc


def cmd_contract(repo_root: Path, query: str, epic: str | None, limit: int = DEFAULT_LIMIT, cursor: str | None = None) -> dict:
    epics = discover_epics(repo_root, epic)
    facts: list[Fact] = []
    warnings: list[str] = []

    m = _METHOD_PATH_RE.match(query.strip())
    if m:
        method, path = m.group(1).upper(), m.group(2)
        for epic_ctx in epics:
            contracts = epic_ctx.models.get("contracts.json")
            for entity in (contracts or {}).get("entities", []):
                for op in entity.get("operations", []):
                    if op.get("method") != method or not _operation_path_matches(op.get("path", ""), path):
                        continue
                    # Locator discipline (two-plane): each condition/error case
                    # is AT MOST one summary line ("id: description" / "status:
                    # condition") — never the structured body (expressions stay
                    # in Plane A; deref the handle via `show` for the block).
                    facts.append(Fact(
                        kind="contract_operation",
                        id=None,
                        statement=f"{method} {op['path']}: {op.get('name', '')}",
                        handle=f"docs/epics/{epic_ctx.slug}/models/contracts.json#{entity['name']}/{op.get('name')}",
                        epic=epic_ctx.slug,
                        extra={
                            "preconditions": [_condition_line(c) for c in op.get("preconditions", [])],
                            "postconditions": [_condition_line(c) for c in op.get("postconditions", [])],
                            "error_cases": [
                                f"{e.get('status')}: {e.get('condition', '')}" for e in op.get("error_cases", [])
                            ],
                            "state_transition": op.get("state_transition"),
                            "invariants_touched": op.get("invariants_touched", []),
                        },
                        provenance={"blob_sha": None, "dirty": None, "from_cache": False},
                        exact=True,
                        proximity=0,
                    ))
            warnings.extend(epic_ctx.warnings)
        summary = (
            f"contract: {len(facts)} operation(s) match {method} {path}"
            if facts else f"contract: scanned, no operation matches {method} {path}"
        )
        return build_envelope(
            facts, verb="contract", summary=summary, warnings=warnings,
            query_provenance=_query_provenance(repo_root, epics),
            limit=limit, cursor=cursor,
        )

    # ID lookup: search structured conditions first, fall back to prose contracts.md.
    canonical = check_traceability.canonical_id(query) if _ID_KIND_RE.match(query) else query
    for epic_ctx in epics:
        contracts = epic_ctx.models.get("contracts.json")
        if contracts:
            for entity in contracts.get("entities", []):
                for op in entity.get("operations", []):
                    for kind, bucket in (("precondition", "preconditions"), ("postcondition", "postconditions")):
                        for cond in op.get(bucket, []):
                            if cond.get("id") == canonical:
                                # Locator: description is the one-line summary;
                                # the machine expression stays in Plane A (deref
                                # the handle via `show`).
                                facts.append(Fact(
                                    kind=kind,
                                    id=canonical,
                                    statement=cond.get("description", ""),
                                    handle=f"docs/epics/{epic_ctx.slug}/models/contracts.json#{entity['name']}/{op.get('name')}",
                                    epic=epic_ctx.slug,
                                    extra={"operation": op.get("name")},
                                    provenance={"blob_sha": None, "dirty": None, "from_cache": False},
                                    exact=True,
                                    proximity=0,
                                ))
        if canonical in epic_ctx.defined:
            facts.append(Fact(
                kind="contract",
                id=canonical,
                statement=epic_ctx.statement_for(canonical),
                handle=f"docs/epics/{epic_ctx.slug}/{epic_ctx.defined[canonical][0]}:{epic_ctx.defined[canonical][1]}",
                epic=epic_ctx.slug,
                extra={"relation": "declaration"},
                provenance={"blob_sha": None, "dirty": None, "from_cache": False},
                exact=True,
                proximity=0,
            ))
        warnings.extend(epic_ctx.warnings)
    summary = (
        f"contract: {len(facts)} fact(s) for {canonical}"
        if facts else f"contract: scanned, {canonical} not found in any epic's contracts"
    )
    return build_envelope(
        facts, verb="contract", summary=summary, warnings=warnings,
        query_provenance=_query_provenance(repo_root, epics),
        limit=limit, cursor=cursor,
    )


# --- verb: state -------------------------------------------------------------------


def cmd_state(repo_root: Path, query: str, epic: str | None, limit: int = DEFAULT_LIMIT, cursor: str | None = None) -> dict:
    epics = discover_epics(repo_root, epic)
    facts: list[Fact] = []
    warnings: list[str] = []
    canonical = check_traceability.canonical_id(query) if _ID_KIND_RE.match(query) else None

    any_sm = False
    for epic_ctx in epics:
        sm = epic_ctx.models.get("state-machines.json")
        if not sm:
            continue
        any_sm = True
        warnings.extend(epic_ctx.warnings)
        handle_base = f"docs/epics/{epic_ctx.slug}/models/state-machines.json"

        if canonical:
            invs = [i for i in sm.get("invariants", []) if i.get("id") == canonical]
            for inv in invs:
                states = set(inv.get("applies_to_states", [])) if inv.get("scope") == "state-specific" else set(sm.get("states", {}))
                facts.append(Fact(
                    kind="invariant",
                    id=canonical,
                    statement=inv.get("description", ""),
                    handle=f"{handle_base}#invariants[{inv.get('id')}]",
                    epic=epic_ctx.slug,
                    extra={
                        "scope": inv.get("scope", "global"),
                        "category": inv.get("category"),
                        "applies_to_states": sorted(states),
                        "controls_field": inv.get("controls_field", []),
                        "sanctioned_writers": inv.get("sanctioned_writers", []),
                    },
                    provenance={"blob_sha": None, "dirty": None, "from_cache": False},
                    exact=True,
                    proximity=0,
                ))
            continue

        if query.lower() in (sm.get("name", "") or "").lower():
            for t in sm.get("transitions", []):
                facts.append(Fact(
                    kind="transition", id=None,
                    statement=f"{t['from']} -> {t['to']} on {t['event']}",
                    handle=f"{handle_base}#transitions[{t['from']}->{t['to']}]", epic=epic_ctx.slug,
                    extra={"machine": sm.get("name")},
                    provenance={"blob_sha": None, "dirty": None, "from_cache": False},
                    exact=True, proximity=0,
                ))
            for inv in sm.get("invariants", []):
                facts.append(Fact(
                    kind="invariant", id=inv.get("id"), statement=inv.get("description", ""),
                    handle=f"{handle_base}#invariants[{inv.get('id')}]", epic=epic_ctx.slug,
                    extra={"scope": inv.get("scope", "global")},
                    provenance={"blob_sha": None, "dirty": None, "from_cache": False},
                    exact=True, proximity=0,
                ))
            continue

        states = sm.get("states", {})
        if query in states:
            state_def = states[query] or {}
            for t in sm.get("transitions", []):
                if t.get("from") == query or t.get("to") == query:
                    # Locator discipline: guard summaries are one line each
                    # (description only) — the machine expression stays in
                    # Plane A; deref the handle via `show` for the block.
                    facts.append(Fact(
                        kind="transition", id=None,
                        statement=f"{t['from']} -> {t['to']} on {t['event']}",
                        handle=f"{handle_base}#transitions[{t['from']}->{t['to']}]", epic=epic_ctx.slug,
                        extra={
                            "guards": [_condition_line(g) for g in t.get("guards", [])],
                            "actions": t.get("actions", []),
                        },
                        provenance={"blob_sha": None, "dirty": None, "from_cache": False},
                        exact=True, proximity=0,
                    ))
            for it in sm.get("invalid_transitions", []):
                if it.get("from") == query or it.get("to") == query:
                    facts.append(Fact(
                        kind="invalid_transition", id=None,
                        statement=f"{it['from']} -> {it['to']} REJECTED: {it.get('reason', '')}",
                        handle=f"{handle_base}#invalid_transitions[{it['from']}->{it['to']}]", epic=epic_ctx.slug,
                        extra={}, provenance={"blob_sha": None, "dirty": None, "from_cache": False},
                        exact=True, violation=True, proximity=0,
                    ))
            applicable_inv = _sm_invariants_for_states(sm, {query})
            for inv in applicable_inv:
                facts.append(Fact(
                    kind="invariant", id=inv.get("id"), statement=inv.get("description", ""),
                    handle=f"{handle_base}#invariants[{inv.get('id')}]", epic=epic_ctx.slug,
                    extra={"scope": inv.get("scope", "global")},
                    provenance={"blob_sha": None, "dirty": None, "from_cache": False},
                    exact=True, proximity=0,
                ))
            if state_def.get("entry_actions") or state_def.get("exit_actions"):
                facts.append(Fact(
                    kind="state", id=None, statement=state_def.get("description", query),
                    handle=f"{handle_base}#states/{query}", epic=epic_ctx.slug,
                    extra={
                        "entry_actions": state_def.get("entry_actions", []),
                        "exit_actions": state_def.get("exit_actions", []),
                    },
                    provenance={"blob_sha": None, "dirty": None, "from_cache": False},
                    exact=True, proximity=0,
                ))
            for fname, cf in (sm.get("context") or {}).items():
                if query in (cf.get("required_in_states") or []):
                    facts.append(Fact(
                        kind="context_field", id=None, statement=f"{fname}: {cf.get('type')}",
                        handle=f"{handle_base}#context/{fname}", epic=epic_ctx.slug,
                        extra={"required_in_states": cf.get("required_in_states", [])},
                        provenance={"blob_sha": None, "dirty": None, "from_cache": False},
                        exact=True, proximity=0,
                    ))

    if not any_sm:
        warnings.append("no state-machines.json found in any epic")
    summary = (
        f"state: {len(facts)} fact(s) for '{query}'"
        if facts else f"state: scanned, nothing found for '{query}'"
    )
    return build_envelope(
        facts, verb="state", summary=summary, warnings=warnings,
        query_provenance=_query_provenance(repo_root, epics),
        limit=limit, cursor=cursor,
    )


# --- verb: show --------------------------------------------------------------------


_HANDLE_RE = re.compile(r"^(.+):(\d+)$")


def _find_node_by_id(obj, target_id: str):
    """The JSON node declaring stable ID `target_id` (case-insensitive), or None."""
    if isinstance(obj, dict):
        nid = obj.get("id")
        if isinstance(nid, str) and check_traceability.canonical_id(nid) == target_id:
            return obj
        for v in obj.values():
            found = _find_node_by_id(v, target_id)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_node_by_id(item, target_id)
            if found is not None:
                return found
    return None


def _resolve_json_fragment(data: dict, fragment: str):
    """Resolve a pseudo-handle fragment against a parsed Plane-A model.

    Supports exactly the fragment grammar the other verbs EMIT — every handle
    in any envelope must round-trip through `show`:

    - ``pages[<route>]``                      (ui-spec.json)
    - ``hotspots[<file>]``                    (docs/quality/hotspots.json, #187)
    - ``invariants[<ID>]``                    (state-machines.json)
    - ``transitions[<from>-><to>]``           (state-machines.json)
    - ``invalid_transitions[<from>-><to>]``   (state-machines.json)
    - ``states/<name>`` / ``context/<name>``  (state-machines.json)
    - ``<Entity>/<operation-or-field name>``  (contracts.json)
    - a bare stable ID                        (any model; derived_from handles)

    Returns the declared node (dict), a list of matching nodes (a from->to
    pair may carry multiple events), or None.
    """
    m = re.match(r"^pages\[(.+)\]$", fragment)
    if m:
        return (data.get("pages") or {}).get(m.group(1))
    m = re.match(r"^hotspots\[(.+)\]$", fragment)
    if m:
        want = m.group(1)
        for h in data.get("hotspots", []) or []:
            if isinstance(h, dict) and h.get("file") == want:
                return h
        return None
    m = re.match(r"^invariants\[(.+)\]$", fragment)
    if m:
        want = m.group(1).lower()
        for inv in data.get("invariants", []) or []:
            if str(inv.get("id", "")).lower() == want:
                return inv
        return None
    m = re.match(r"^(transitions|invalid_transitions)\[(.+?)->(.+)\]$", fragment)
    if m:
        bucket, frm, to = m.groups()
        matches = [t for t in data.get(bucket, []) or [] if t.get("from") == frm and t.get("to") == to]
        if not matches:
            return None
        return matches[0] if len(matches) == 1 else matches
    m = re.match(r"^states/(.+)$", fragment)
    if m:
        return (data.get("states") or {}).get(m.group(1))
    m = re.match(r"^context/(.+)$", fragment)
    if m:
        return (data.get("context") or {}).get(m.group(1))
    if _ID_KIND_RE.match(fragment):
        node = _find_node_by_id(data, check_traceability.canonical_id(fragment))
        if node is not None:
            return node
    m = re.match(r"^([^/\[\]]+)/(.+)$", fragment)
    if m:
        entity_name, member = m.groups()
        for entity in data.get("entities", []) or []:
            if entity.get("name") != entity_name:
                continue
            for op in entity.get("operations", []) or []:
                if op.get("name") == member:
                    return op
            for fld in entity.get("fields", []) or []:
                if fld.get("name") == member:
                    return fld
    return None


def _show_pseudo_handle(repo_root: Path, handle: str, epics: list[Epic],
                        limit: int, cursor: str | None) -> dict:
    """Dereference a `file#fragment` pseudo-handle to its declared JSON block.
    This is `show`'s job in the two-plane model: the other verbs only LOCATE
    (IDs + handles + one-line summaries); the actual declared content is only
    ever served here, read live from Plane A."""
    rel, _, fragment = handle.partition("#")
    full = Path(repo_root) / _norm(rel)
    if not full.is_file():
        return _unscanned_envelope(
            f"{rel} not found under {repo_root}", query_provenance=_query_provenance(repo_root, epics)
        )
    try:
        data = json.loads(full.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return _unscanned_envelope(
            f"{rel} could not be parsed as JSON: {exc}",
            query_provenance=_query_provenance(repo_root, epics),
        )
    node = _resolve_json_fragment(data, fragment)
    if node is None:
        return build_envelope(
            [], verb="show",
            summary=f"show: scanned, fragment '{fragment}' not found in {rel}",
            warnings=[], query_provenance=_query_provenance(repo_root, epics),
            limit=limit, cursor=cursor,
        )
    if isinstance(node, dict):
        statement = str(
            node.get("description") or node.get("name") or node.get("title") or fragment
        )
    else:
        statement = f"{len(node)} matching declaration(s)"
    fact = Fact(
        kind="declaration", id=node.get("id") if isinstance(node, dict) else None,
        statement=statement, handle=handle, epic=None,
        extra={"block": json.dumps(node, indent=2).splitlines()},
        provenance=_file_provenance(repo_root, _norm(rel)),
        exact=True, proximity=0,
    )
    return build_envelope(
        [fact], verb="show", summary=f"show: {handle}", warnings=[],
        query_provenance=_query_provenance(repo_root, epics),
        limit=limit, cursor=cursor,
    )


def cmd_show(repo_root: Path, handle: str, epic: str | None, limit: int = DEFAULT_LIMIT, cursor: str | None = None) -> dict:
    epics = discover_epics(repo_root, epic)
    if "#" in handle:
        return _show_pseudo_handle(repo_root, handle, epics, limit, cursor)
    m = _HANDLE_RE.match(handle)
    if m:
        rel, line_s = m.group(1), m.group(2)
        line = int(line_s)
        full = Path(repo_root) / rel
        if not full.is_file():
            return _unscanned_envelope(
                f"{rel} not found under {repo_root}", query_provenance=_query_provenance(repo_root, epics)
            )
        lines = full.read_text().splitlines()
        lo, hi = max(0, line - 3), min(len(lines), line + 2)
        window = lines[lo:hi]
        symbol = check_single_writer._enclosing_symbol(lines, min(line - 1, len(lines) - 1)) if lines else None
        fact = Fact(
            kind="source", id=None, statement=lines[line - 1].strip() if 1 <= line <= len(lines) else "",
            handle=handle, epic=None,
            extra={"context": window, "start_line": lo + 1, "symbol": symbol},
            provenance=_file_provenance(repo_root, rel),
            exact=True, proximity=0,
        )
        return build_envelope(
            [fact], verb="show", summary=f"show: {handle}", warnings=[],
            query_provenance=_query_provenance(repo_root, epics),
            limit=limit, cursor=cursor,
        )

    canonical = check_traceability.canonical_id(handle) if _ID_KIND_RE.match(handle) else handle
    for e in epics:
        if canonical in e.defined:
            rel, line = e.defined[canonical]
            full = e.dir / rel
            lines = full.read_text().splitlines()
            lo, hi = max(0, line - 1), min(len(lines), line + 4)
            fact = Fact(
                kind="declaration", id=canonical, statement=e.statement_for(canonical),
                handle=f"docs/epics/{e.slug}/{rel}:{line}", epic=e.slug,
                extra={"context": lines[lo:hi]},
                provenance=_file_provenance(repo_root, str(Path("docs") / "epics" / e.slug / rel)),
                exact=True, proximity=0,
            )
            return build_envelope(
                [fact], verb="show", summary=f"show: {canonical}", warnings=[],
                query_provenance=_query_provenance(repo_root, epics),
                limit=limit, cursor=cursor,
            )
    return build_envelope(
        [], verb="show", summary=f"show: scanned, {handle} not found as a handle or declared ID",
        warnings=[], query_provenance=_query_provenance(repo_root, epics),
        limit=limit, cursor=cursor,
    )


# --- rendering / CLI ----------------------------------------------------------------


def render_text(envelope: dict) -> str:
    lines = [envelope["summary"], ""]
    for f in envelope["facts"]:
        epic_tag = f" [{f['epic']}]" if f.get("epic") else ""
        lines.append(f"- ({f['kind']}) {f.get('id') or ''} {f['handle']}{epic_tag}")
        if f.get("statement"):
            lines.append(f"    {f['statement']}")
    if envelope["omitted"]:
        lines.append(f"\n... {envelope['omitted']} more (cursor={envelope['cursor']})")
    if envelope["warnings"]:
        lines += ["", "Warnings:"] + [f"- {w}" for w in envelope["warnings"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Agent-facing architecture knowledge CLI (contracts/invariants/state machines/ui-spec + code annotations)"
    )
    parser.add_argument("--repo", help="Target repo root")
    parser.add_argument("--epic", help="Scope to one epic slug (default: all epics under docs/epics/)")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--cursor")
    parser.add_argument("--scanner-version", action="store_true", help="Print the hash-derived scanner version and exit")
    sub = parser.add_subparsers(dest="verb")

    p = sub.add_parser("orient")
    p.add_argument("path")
    p = sub.add_parser("governs")
    p.add_argument("target")
    p = sub.add_parser("writers")
    p.add_argument("target")
    p = sub.add_parser("guards")
    p.add_argument("ctr_id")
    p = sub.add_parser("verifies")
    p.add_argument("ctr_id")
    p = sub.add_parser("annotations")
    p.add_argument("id")
    p.add_argument("--verb")
    p = sub.add_parser("trace")
    p.add_argument("id")
    p = sub.add_parser("contract")
    p.add_argument("query")
    p = sub.add_parser("state")
    p.add_argument("query")
    p = sub.add_parser("show")
    p.add_argument("handle")

    args = parser.parse_args(argv)

    if args.scanner_version:
        print(_scanner_version())
        return 0

    if not args.verb:
        print("Error: a verb is required unless --scanner-version is given", file=sys.stderr)
        return 2
    if not args.repo:
        print("Error: --repo is required", file=sys.stderr)
        return 2
    repo_root = Path(args.repo)
    if not repo_root.is_dir():
        print(f"Error: repo not found: {args.repo}", file=sys.stderr)
        return 2
    if args.epic and not (repo_root / "docs" / "epics" / args.epic).is_dir():
        # A nonexistent epic slug is a usage error (a typo), exactly like the
        # other checkers' missing epic_dir — NOT a "scanned, nothing governs"
        # empty answer, which would serve absence of knowledge as knowledge.
        print(f"Error: epic dir not found: {repo_root / 'docs' / 'epics' / args.epic}", file=sys.stderr)
        return 2

    kw = {"limit": args.limit, "cursor": args.cursor}
    dispatch = {
        "orient": lambda: cmd_orient(repo_root, args.path, args.epic, **kw),
        "governs": lambda: cmd_governs(repo_root, args.target, args.epic, **kw),
        "writers": lambda: cmd_writers(repo_root, args.target, args.epic, **kw),
        "guards": lambda: cmd_guards(repo_root, args.ctr_id, args.epic, **kw),
        "verifies": lambda: cmd_verifies(repo_root, args.ctr_id, args.epic, **kw),
        "annotations": lambda: cmd_annotations(repo_root, args.id, args.epic, args.verb, **kw),
        "trace": lambda: cmd_trace(repo_root, args.id, args.epic, **kw),
        "contract": lambda: cmd_contract(repo_root, args.query, args.epic, **kw),
        "state": lambda: cmd_state(repo_root, args.query, args.epic, **kw),
        "show": lambda: cmd_show(repo_root, args.handle, args.epic, **kw),
    }
    envelope = dispatch[args.verb]()

    target_arg = getattr(args, "path", None) or getattr(args, "target", None) or getattr(args, "ctr_id", None) \
        or getattr(args, "id", None) or getattr(args, "query", None) or getattr(args, "handle", None)
    try:  # factory telemetry; no-op unless enabled, never breaks the query
        import os
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from factory_log import emit_query
        hit_count = len(envelope["facts"]) + envelope["omitted"]
        emit_query(args.verb, repo=args.repo, path=target_arg, hit_count=hit_count)
    except Exception:
        pass

    if args.format == "json":
        print(json.dumps(envelope, indent=2))
    else:
        print(render_text(envelope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
