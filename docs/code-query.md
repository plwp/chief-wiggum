# code_query.py — agent-facing architecture knowledge CLI (#159)

Agents (and workflows) were re-deriving the architecture every session — N
greps plus a full context-load of `contracts.md`/`state-machines.md`/
`invariants.md`/`adr.md` — instead of asking one structural question.
`scripts/code_query.py` is that one question: "what must I know before
touching this file/field/contract", answered from the epic artifacts plus code
annotations, as a small JSON envelope with stable-ID handles instead of a
context dump.

## The two-plane invariant (load-bearing)

- **Plane A — epic knowledge**: `contracts.json`, `state-machines.json`,
  `transition-map.json`, `ui-spec.json`, prose invariants/ADR/`docs/design/design.json`.
  Read **live** on every query, **never cached or re-serialized** by this tool.
  `code_query.py` is a **locator**, not a content store — it returns stable IDs
  and `file:line` handles, never paraphrased contract bodies. Want the actual
  text? Call `show`.
- **Plane B — per-file code emissions**: `@cw-trace` sites
  (`check_traceability.py`), candidate writer sites (`check_single_writer.py`).
  The only cacheable plane (a persisted cache is a later issue) — phase 1 here
  is live-scan, exactly like the two checkers this tool builds on
  (`emit_source_annotations`/`emit_write_sites`, #160).

Every claim (governing artifacts, writer verdicts, coverage) is computed
**fresh** by joining Plane B onto Plane A at query time.

## Verbs (phase 1)

```
python3 scripts/code_query.py --repo <path> [--epic <slug>] [--format json|text] \
  [--limit N] [--cursor C] <verb> <args...>
```

| Verb | Args | Answers |
| --- | --- | --- |
| `orient` | `<path>` | Flagship: contracts/invariants governing this file, controlled fields it writes (sanctioned?), state-machine transitions bound to it, ui-spec component/route/auth (frontend files) — each with a one-line statement + handle |
| `governs` | `<path\|field>` | Reverse index: for a path, the same governing facts as `orient` split `direct` vs `inferred`; for a field, its single-write-path writers and/or contract field metadata (`immutable`/`source_of_truth`/`required_when`) |
| `writers` | `<field\|INV-ID>` | Wraps `check_single_writer.scan_writers` — every write site, sanctioned/unsanctioned, enclosing symbol |
| `guards` / `verifies` | `<CTR-ID\|INV-ID>` | `@cw-trace guards`/`ensures` (code) or `verifies` (test/probe/policy/telemetry) sites targeting this ID |
| `annotations` | `<ID> [--verb V]` | Every `@cw-trace` annotation (any verb, or filtered) targeting this ID, across epic docs (`realizes`) and source |
| `trace` | `<ID>` | Full `BR -> CTR/INV -> code -> test` slice, plus `derived_from` provenance (ticket/AC) when the declaring JSON node carries it |
| `contract` | `<CTR-ID \| "METHOD /path">` | REQUIRES/ENSURES/error-case **one-line summaries** ("id: description" / "status: condition"), `invariants_touched` IDs, `state_transition` for a `contracts.json` operation or condition |
| `state` | `<machine name \| state \| INV-ID>` | Adjacency (in/out transitions), entry/exit actions, invalid transitions, applicable invariants, `required_in_states` context fields — guard summaries are descriptions only |
| `show` | `<file:line \| ID \| file#fragment>` | Dereference ANY emitted handle to its raw declared content — the only verb that serves bodies (source context, or a model's declared JSON block) |

**Locator discipline (two-plane, enforced by test):** no verb except `show`
ever returns a structured Plane-A body or a machine `expression` — facts carry
IDs, dereferenceable handles, and at most one summary line per item (`orient`'s
operation facts carry only counts: `n_preconditions`/`n_postconditions`/
`n_error_cases`). Want the block? `show` the handle.

**Every emitted handle round-trips through `show`** (property-tested). Three
handle forms exist:

- `file:line` — source/prose location; `show` prints the line + context window.
- a bare stable ID — `show` prints its declaration site from the epic docs.
- `file#fragment` — a Plane-A model node; `show` loads the JSON live and prints
  the declared block. Fragment grammar (exactly what the verbs emit):
  `Entity/Operation-or-Field` (contracts.json), `pages[route]` (ui-spec.json),
  `invariants[ID]`, `transitions[from->to]`, `invalid_transitions[from->to]`,
  `states/name`, `context/name` (state-machines.json), or a bare stable ID
  (derived_from handles).

`--scanner-version` prints a hash of this module's source plus
`check_single_writer.py`, `check_traceability.py` (their emissions define this
tool's facts), and the shared `chief_wiggum` dependencies (same convention as
those checkers). A nonexistent `--epic` slug is a usage error (exit 2), never
an empty answer.

## Response envelope

Every verb returns exactly:

```json
{
  "summary": "one-line human-readable answer",
  "facts": [ { "kind": "...", "id": "...", "statement": "...", "handle": "file:line", "epic": "slug", "...verb-specific fields...": "...", "provenance": {"blob_sha": "...", "dirty": false, "from_cache": false} } ],
  "omitted": 0,
  "cursor": null,
  "warnings": [],
  "provenance": { "repo": "...", "epics": ["..."], "scanner_version": "..." }
}
```

- **`facts`** is capped at `--limit` (default ~40); `cursor`/`omitted` page
  through the rest. Ranking (`_rank_key`): exact ID/path hits first, then
  violations/unsanctioned findings, then proximity (same file > package/dir >
  epic > other), then prod-before-tests — **inverted for `verifies`**, where
  the test/probe/policy/telemetry side IS the point.
- **Per-fact `provenance`** (`blob_sha`, `dirty`, `from_cache: false` — phase 1
  never caches) is the fact's own lineage; the envelope's top-level
  `provenance` is query-level (repo root, epics scanned, scanner version).

## Never serve unknown as empty

A query against a path that genuinely can't be read (doesn't exist under the
repo root) reports **`unscanned`** — an explicit `summary`/`warnings` marker —
never the same `facts: []` a file that WAS scanned and genuinely has nothing
governing it gets. Absence of knowledge and proof of absence are different
answers. ID-shaped lookups (an invariant, a state, a contract) that aren't
declared anywhere are always a genuine empty (there's no file-read to fail —
it's a pure data lookup), with a `warnings` entry explaining why.

## Artifact-derived binding (`orient` binds by artifact, not only annotation)

An un-annotated handler still gets a real answer:

- **`transition-map.json` `code_locations`** — exact file+line match, so a
  covered-but-un-annotated transition's handler is bound precisely (`exact`).
- **`contracts.json` operation `path`** / **`ui-spec.json` page route** —
  word-tokenized, length-aware, ALL-words matching against the file's own path
  (`inferred`, never `exact`): **every** literal (non-param) word of the
  route/path must match one of the file's directory+filename words, with a
  minimum length before two words may substring-match each other. So `order`
  ~ `orders` still matches, but a short word like `ui` can never hide inside
  an unrelated longer word like `builder` (a real false positive found and
  fixed during validation), and `ui/orders/page.tsx` does not inherit
  `/api/v1/orders/:id/confirm` just for matching "orders" — "confirm" must
  match too (regression-tested).
- **Fixed by #185 — corpus-derived word specificity**: an all-words match is
  necessary but no longer sufficient. Each literal word is weighted by its
  **document frequency across the epic's own operation-paths + ui routes**
  (no external corpus, no network — recomputed live from the same `contracts.json`/
  `ui-spec.json` already loaded for this query, so it stays fully reproducible
  and stdlib-only): a word present in more than 40% of the epic's own
  path/route "documents" carries **zero binding weight**. At least one of the
  pattern's literal words must clear that bar, or the match is rejected
  outright — even though the all-words guard alone would have accepted it.
  **Small-corpus bypass**: with fewer than 5 documents the DF bar is skipped
  entirely (every word counts as specific) — document frequency over a tiny
  corpus is noise, and without the floor a one-operation epic would reject
  *itself* (its own path's words ARE the whole corpus at df=1.0), as would
  the two-document shape of an operation plus its UI route sharing the
  entity word. Threshold interaction: at ≥5 documents, "common" requires
  presence in >40% of them — at least 3 of 5 — so a shared entity word needs
  real recurrence before it loses binding weight.
  This is the mechanical fix for the real over-match found validating against
  dogeared-coach: `ui/src/providers/auth-provider.tsx` word-matched dozens of
  operations that reduce to the bare word `providers` once params are
  stripped, because "provider" is that epic's own primary tenant entity name
  and recurs across most of its operations. An entity+verb combination (e.g.
  a file whose path also contains "verify" for a `.../providers/:id/verify`
  operation) still binds — only the entity word alone, when it's corpus-common,
  no longer does (regression-tested: `tests/test_code_query.py`'s
  `test_common_entity_word_alone_does_not_bind` /
  `test_entity_verb_combination_still_binds_despite_common_entity_word`).
- **Known limitation, still disclosed not hidden**: this remains a lexical
  heuristic, not symbol resolution (tree-sitter/refs-lite is explicitly out of
  phase 1). A file whose name word-covers all of an unrelated operation's
  literal words, where at least one of those words is ALSO specific by the
  document-frequency measure (not just corpus-common), can still over-match —
  the fix narrows the false-positive surface to genuinely rare word
  collisions, it does not eliminate lexical matching's inherent ceiling. These
  facts are always labeled `"relation": "inferred"` and ranked below any
  `direct` fact via `_rank_key`'s **leading relation-tier element**
  (`direct=0 < inferred=1 < measured=2`) — real annotations remain the precise
  path when a file's governing contract must be unambiguous.

## The `measured` tier: hotspot facts (#187)

`orient` also surfaces a `kind: "hotspot"`, `relation: "measured"` fact when
the queried file — or a `coupled_with` partner of it — appears as a
top-decile entry in `docs/quality/hotspots.json` (produced by
`scripts/hotspot_discovery.py`, composed from `scripts/quality/{churn,
complexity,process}.py`). This is a **third, separate channel** from the
`inferred` lexical matcher above: membership is checked by exact file-path
equality against `hotspots.json` ONLY — `_hotspot_facts_for_file` never calls
`_path_matches_literal_segments` (INV-fh-007/012). A file that merely *looks*
like a hotspot path (shares a word) but isn't listed gets no hotspot fact; a
file that IS listed gets exactly one, with `provenance.generating_sha` set to
the `git_sha` the record was generated at. The leading relation-tier rank key
still dominates: a `direct` `@cw-trace` annotation on the same file always
sorts before its `measured` hotspot fact, regardless of either fact's `exact`
flag. `docs/quality/hotspots.json` itself carries no stable IDs and is
referenced by no `@cw-trace` link — it's a rebuildable, observational
artifact (INV-fh-007), and this fact is advisory: it never gates anything,
and its absence for a file is not evidence the file is safe.

## Validation

- **Golden parity** (`tests/test_code_query_golden.py`): `writers` facts are
  asserted set-equal to `check_single_writer.check()`'s own `writers`/
  `violations`; `guards`/`verifies`/`annotations` facts are asserted set-equal
  to `check_traceability.scan_source()`/`scan_epic_annotations()` on the same
  fixture repo (`tests/fixtures/code_query_repo`) — code_query never invents or
  drops a site the checkers themselves would report.
- **Real-repo validation**: `orient` was run against 10 files in a
  previously-shipped repo (dogeared-coach) — annotated middleware, several
  genuinely un-annotated handlers, and frontend files with no `@cw-trace` at
  all — confirming annotation-bound, artifact-bound, and genuinely-empty
  answers all render correctly (see the module docstring / PR for the specific
  false positive this found and fixed).

## Query telemetry

Every call emits a `factory_log.py` `query` event (`verb`, `path`, `hit_count`)
— no-op unless telemetry is enabled (`CW_TELEMETRY=1`/`CW_FACTORY_LOG=<path>`),
never blocks. `factory_log.py aggregate` reports calls/hits/misses per verb, so
which structural questions agents actually ask is measurable, not assumed.

## Explicitly out of phase 1

A persisted cache for Plane B (a future issue), tree-sitter/symbol
outlines/refs-lite, sqlite, any new annotation convention (`transition-map.json`
already binds transition sites), and a `map` verb beyond module level.
