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
| `contract` | `<CTR-ID \| "METHOD /path">` | REQUIRES/ENSURES, error cases, `invariants_touched`, `state_transition` for a `contracts.json` operation or condition |
| `state` | `<machine name \| state \| INV-ID>` | Adjacency (in/out transitions), entry/exit actions, invalid transitions, applicable invariants, `required_in_states` context fields |
| `show` | `<file:line \| ID>` | Dereference a handle or stable ID to its raw declared text — the token-frugal "give me the actual content" escape hatch |

`--scanner-version` prints a hash of this module's source plus its
`chief_wiggum` dependencies (same convention as `check_single_writer.py` /
`check_traceability.py`).

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
  word-tokenized, length-aware matching against the file's own path
  (`inferred`, never `exact`): every literal (non-param) word of the
  route/path is matched against the file's directory+filename words, with a
  minimum length before two words may substring-match each other (so `order`
  ~ `orders` still matches, but a short word like `ui` can never hide inside
  an unrelated longer word like `builder` — this exact false positive was
  found and fixed during validation against a real repo; see below).
- **Known limitation, disclosed not hidden**: this is a lexical heuristic, not
  symbol resolution (tree-sitter/refs-lite is explicitly out of phase 1). In an
  admin-heavy domain where one entity name (e.g. "provider") is genuinely
  common across dozens of operations, a file whose own name contains that
  word (e.g. `auth-provider.tsx`) will match most of them. These facts are
  always labeled `"relation": "inferred"` and ranked below any `direct`
  (annotation or `code_locations`-exact) match — real annotations remain the
  precise path when a file's governing contract must be unambiguous. Tracked
  for a future precision pass: [chief-wiggum#185](https://github.com/plwp/chief-wiggum/issues/185).

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
