# Traceability: business rule → contract → code → test

Chief Wiggum can prove an epic's contracts are implemented, tested, and
internally consistent — mechanically, from machine-readable annotations, instead
of trusting prose and self-reported coverage. This is the Traceability
Information Model (TIM) + Design-by-Contract pattern.

The chain, navigable in both directions:

```
business rule ──realizes──▶ contract/invariant ──guards/ensures──▶ code
                                   │
                                   └──verifies──▶ test
```

## Stable IDs

`/architect` assigns every contract and invariant a stable ID, immutable once
issued. Business rules (from `/seed`/`/architect`) get IDs too:

- `BR-<slug>-NNN` — business rule
- `CTR-<slug>-NNN` — contract (a REQUIRES/ENSURES condition)
- `INV-<slug>-NNN` — invariant

IDs are *declared* in the epic docs by a markdown heading (`### CTR-order-001 …`),
a bold label (`**INV-order-003**: …`), or a JSON `"id"` field in
`models/contracts.json` / `models/state-machines.json`.

## Annotation grammar (uniform, language-agnostic)

A single LOBSTER-style namespaced tag — works in any language's comments, and
won't collide with JSDoc/decorators/test markers:

```
@cw-trace <verb> <ID> [<ID> ...]      verbs: realizes | guards | ensures | verifies
```

Examples:

```python
# @cw-trace guards CTR-order-001
def create_order(req): ...

@pytest.mark.contract("CTR-order-001")  # @cw-trace verifies CTR-order-001
def test_create_order(): ...
```
```go
// @cw-trace ensures CTR-order-001 INV-order-003
```
```markdown
<!-- in contracts.md, near the contract: -->
### CTR-order-001 — valid date range
<!-- @cw-trace realizes BR-order-001 -->
```

- `realizes` links a contract/invariant to a business rule (authored in the epic docs).
- `guards`/`ensures` links **code** to the contract it enforces.
- `verifies` links a **test** to the contract it checks.

## The checker

`scripts/check_traceability.py` builds the graph from the defined IDs + the
`@cw-trace` annotations and reports **orphan business rules** (no realizing
contract), **uncovered contracts** (no code guard), **untested contracts** (no
verifying test), **dangling annotations** (reference to an undefined ID), and
**invalid links** (a verb whose node types violate `templates/formal-models/tim-schema.json`).

```bash
python3 scripts/check_traceability.py docs/epics/<slug> --source . --format json
python3 scripts/check_traceability.py docs/epics/<slug> --source . --gate soundness  # /architect
python3 scripts/check_traceability.py docs/epics/<slug> --source . --gate coverage   # /close-epic

# ticket-scoped speed-up (/implement, /implement-wave): scan only what changed
python3 scripts/check_traceability.py docs/epics/<slug> --source . --changed-since main

# hash-derived version (source of the scanner + its chief_wiggum deps)
python3 scripts/check_traceability.py --scanner-version
```

It is a **separate pass**, not compile-time enforcement, and degrades gracefully:
an epic with no annotations reports absence rather than failing. It proves a
trace *link exists* — not that a guard is semantically correct (that is the
Design-by-Contract verification frontier, out of scope; LSP symbol resolution
is the cheaper next step). Mirrors `check_unresolved.py`.

## Three measurement states (chief-wiggum#281, #289 vocabulary)

A measurement can fail in two structurally different ways, and the report's
`applicability` field (`applicable | inapplicable | error`) distinguishes them:

- **`inapplicable`** — there is nothing to measure: no ID-bearing artifact
  (`contracts.md`/`.json`, `invariants.md`, `state-machines.md`/`.json`,
  `architecture.json`) exists with content anywhere under the epic dir. A
  genuinely empty epic, or one holding only non-ID-bearing docs
  (`adr.md`, `retrospective.md`, …) or zero-byte/whitespace-only files, is
  `inapplicable`, not a pass. `--gate` exits 0 with a banner (existing
  pipelines keep working); the JSON always carries the explicit state.
- **`applicable`** — the graph was measured: at least one stable ID was
  parsed. `soundness_ok`/`coverage_ok` then report real findings (or a real
  pass), not a vacuous one.
- **`error`** — a BROKEN INSTRUMENT: an ID-bearing artifact exists *with
  content* but the scanner parsed **ZERO** stable IDs out of it. This is what
  happens when an epic is authored from a worked example whose IDs don't
  match `DEFINE_RE` (the two-segment `INV-001` shape the `/architect` skill
  used to model, chief-wiggum#281) — the graph looks "clean" only because
  nothing was measured, not because nothing is wrong. `error` folds into
  `soundness_ok` (so it always fails `--gate soundness`) and additionally
  fails `--gate coverage` explicitly (`main()`'s dedicated rule) — a broken
  measurement is not a clean result for EITHER gate. It is never waivable
  (`chief_wiggum.grandfather` waives coverage gaps, not a broken instrument)
  and a scan in this state never writes the `--write-links` sidecar.

Two finding classes feed `error` (and are folded into `soundness_ok` even in
the `applicable` case — see "partial drift" below):

- **`unparsed_artifacts`** — an ID-bearing artifact present with content that
  yielded zero parseable IDs.
- **`malformed_ids`** — a declaration-position near-miss: a token that looks
  like a stable ID (`chief_wiggum.trace_ids.near_miss_ids`) but fails
  `ID_RE.fullmatch` — in practice, the two-segment `KIND-NNN` shape missing
  its slug. This is the *complement* of `DEFINE_RE` in the same declaration
  positions (markdown heading, bold, JSON `"id"`), not a heuristic — it
  catches **partial drift** too: an epic where `contracts.json` is
  model-generated and fine but `invariants.md` was hand-written from a
  now-fixed bad example stays `applicable` (something WAS measured), with the
  near-miss reported as a soundness finding instead of silently
  under-measuring.

A derived `outcome` property (never stored, so it can't drift from
`applicability`) carries the standard four-state vocabulary
(`pass | findings | inapplicable | error`) and the report's `measured`
object (`id_bearing_artifacts`, `defined_ids`) is the denominator — visible
even when the report is otherwise green, so a `defined_ids: 0` can never hide
inside a passing scan.

## Emission/claim seam, `--changed-since`, `--scanner-version` (#160)

Per-file **emission** is a pair of pure functions of file content, with no
knowledge of the defined-ID set: `emit_epic_annotations(rel, text)` (epic docs
— attributes a `realizes`/`derive` annotation to the nearest stable ID declared
above it) and `emit_source_annotations(rel, text, suffix)` (source/test/
verification files — classifies by `source_kind`). `scan_epic_annotations` /
`scan_source` walk the tree and call these per file; the orphan/uncovered/
untested/dangling/invalid-link **verdicts** are computed once, at report time,
in `build_report` — a join against the full defined-ID set. This is the same
shape as `check_single_writer.py`'s `emit_write_sites`/`match_writers` split,
and the basis for a future content-addressed cache
(`chief_wiggum.manifest.build_manifest`): a file's emitted annotations are a
valid cache entry as long as its content hash is unchanged.

`--changed-since <ref>` scopes the `--source` scan (`scan_source`) to files
that differ from `<ref>` (committed diff + dirty tracked + untracked, via
`chief_wiggum.manifest`) instead of walking the whole tree. It does NOT scope
the epic-doc scan — the epic's own docs are always read in full. This is a fast
per-ticket signal for `/implement`/`/implement-wave` (report-only there).
**Whole-repo scanning remains the default, and `/close-epic --gate coverage`
NEVER passes `--changed-since`**: a scoped scan can only under-report coverage
(annotations outside its window are invisible to it), never prove a contract IS
covered — using it for the authoritative gate would produce false "uncovered"/
"untested" findings for code the scan never looked at.

`--scanner-version` prints a hash of the scanner's own source plus its
`chief_wiggum` dependencies (`trace_ids.py`, `manifest.py`, `hashing.py`) — the
version IS the content hash, so there's no hand-bumped constant to forget to
update when the annotation grammar or ID kinds change.

**Submodules / nested git checkouts are excluded from BOTH scan modes.** A
directory under `--source` that contains a `.git` entry (a submodule's gitlink
file, or a vendored/nested repo) is pruned from the full-tree walk, and the
manifest behind `--changed-since` never surfaces a submodule's files either
(git records a submodule as a single gitlink entry, not blobs). Submodule
contents belong to the submodule's own repo and its own gates — this keeps the
two scan modes agreeing on the file universe. A bad `--changed-since` ref or a
non-git `--source` with `--changed-since` is a usage error (exit 2), reported
concisely on stderr.

## Gate scope: blocking a worker only on what it owns (#379)

`--gate soundness` was not part of the per-ticket floor, so `@cw-trace`
direction errors — `guards` on a test, `verifies` on production code — were
introduced freely by implementation workers and surfaced three merges later at
wave-merge. One epic carried two separate `fix(trace): correct @cw-trace
direction` commits for the same class.

The obvious fix is wrong. `--changed-since` scopes only the **source** scan;
the epic docs are always read in full. So `malformed_ids`, `unparsed_artifacts`
and `orphan_business_rules` fire regardless of what the worker touched — and
the ratchet protects goalposts, so a worker may not edit `contracts.md` or
`invariants.md` to clear them.

Measured before the fix was designed: a worker whose entire diff was one
unrelated helper function was blocked, exit 1, by a malformed ID in
`invariants.md` that predated its branch. A gate that blocks on an unfixable
defect is precisely the noisy gate [gate-rollout.md](gate-rollout.md) warns
about — the operator learns to `--force`, and every gate loses authority.

So findings carry an **origin**:

| origin | meaning | diff-local? |
|---|---|---|
| `source` | from the source scan, which `--changed-since` filters to the diff | yes |
| `epic` | from the epic docs — goalposts, always scanned in full | no |
| `external` | from an [external-links](external-links.md) sidecar, re-anchored against the whole tree | no |

`--gate-scope changed` blocks only on `source`-origin findings:

```bash
python3 scripts/check_traceability.py docs/epics/<slug> --source . \
  --changed-since main --gate soundness --gate-scope changed
```

Everything else still prints, and still blocks under the default
`--gate-scope all` in `/architect` and `/close-epic`, where the actor can
actually fix it. Same in-domain vs boundary split [sidecar.md](sidecar.md)
already applies to scope: **report everything, block only on what the actor
owns.** This is a narrowing of an already-validated gate, never a way to make a
finding disappear.

Two usage errors (exit 2), both closing a way to weaken a gate by accident:

- `--gate-scope changed` **without** `--changed-since` — the source scan would
  be the whole repo, so "originates in source" would stop meaning "in this
  diff" and the flag would silently drop epic findings from an authoritative
  run.
- `--gate-scope changed` with `--gate coverage` — coverage over a partial scan
  is meaningless in either direction, since every untouched contract looks
  uncovered. Refused outright rather than implying a scoped coverage gate
  exists.

Under `changed`, the text report prints a `Gate scope: **changed**` line saying
how many of the total soundness findings could block. Without it, `Soundness:
FINDINGS` beside exit 0 would read exactly like the fail-open this repo keeps
hunting.

## Suspect-link propagation (#169)

A trace link only proves what it claimed at the moment it was last checked. If
`CTR-order-001`'s wording changes and no one re-reviews the `@cw-trace guards
CTR-order-001` annotation that cites it, the link still *looks* healthy —
uncovered/untested/dangling all miss this, because the annotation still
resolves to a real, defined ID. This is the doorstop pattern's fix: every link
also records the **definition hash** of the ID it was verified against
(the same stable-ID block hash `ratchet.py` uses to detect weakened contracts —
`chief_wiggum.hashing.hash_epic_definitions`, shared, not duplicated).

The hash-per-link record lives in a generated sidecar,
`docs/quality/trace-links.json` (in the target repo, alongside the ratchet's
own state) — never hand-maintained:

```bash
# Write/refresh the sidecar from the CURRENT scan (only actually writes if the
# requested --gate passes; a failing gate leaves the file untouched):
python3 "$CW_HOME/scripts/check_traceability.py" docs/epics/<slug> --source . \
    --gate coverage --write-links --format text

# Override the sidecar location (default: <--source or cwd>/docs/quality/trace-links.json):
python3 "$CW_HOME/scripts/check_traceability.py" docs/epics/<slug> --links path/to/trace-links.json
```

`--write-links` is always a **full** source scan: the sidecar is the global
record of validated links, and rewriting it from a `--changed-since` partial
scan would silently drop every validated link in unchanged files (they could
then never go suspect). The two flags together are a usage error (exit 2).

Sidecar link targets and definition-hash keys share the **canonical ID form**
(uppercase kind, lowercase slug — `chief_wiggum.trace_ids.canonical_id`), so an
epic doc declaring `CTR-BIL-001` and an annotation citing it in any casing
join on `CTR-bil-001`. The same form keys the ratchet's `contract_hashes`;
journals written before this canonicalization are read compatibly (keys are
canonicalized on load — hash *values* cover block content only and are
unaffected).

**The two written grammars differ on purpose (chief-wiggum#347).** The stable-ID
`pattern`s in `templates/formal-models/*.json` allow a mixed-case slug
(`^ARC-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3}$`), while `tim-registry`'s `id_pattern`
is lowercase-only (`^(BR|CTR|INV|...)-[a-z0-9][a-z0-9-]*-[0-9]{3}$`). That reads
like a disagreement and is not one: they describe **different stages**.

- The schema patterns describe what an author may WRITE. Accepting
  `CTR-BIL-001` means a human who capitalises an abbreviation is not rejected
  by a validator over letter case.
- `id_pattern` describes the CANONICAL form everything joins on after
  `canonical_id()` has run — uppercase kind, lowercase slug.

So `CTR-BIL-001`, `ctr-bil-001` and `CTR-Bil-001` are all valid to author and
all join as `CTR-bil-001`.

Reconciling them by editing either side would be a regression, not a tidy-up:
tightening the schemas to lowercase rejects IDs that are valid today, and
loosening `id_pattern` lets non-canonical IDs into the registry that the join
would then have to guess about.

On every run, if a sidecar exists at the resolved location, each recorded link
is compared against the ID's CURRENT definition hash. A mismatch is **SUSPECT**
— reported in `suspect_links`/`suspect_contracts`, distinct from both dangling
(the ID doesn't exist at all) and uncovered/untested (no link exists): here a
link exists, its claim about the contract is just stale. Rewording the
contract flips its links to SUSPECT; re-running `--write-links` against the
reworded contract clears them (the reviewer re-validated the claim).

Suspect propagation is **report-only initially** (see
[gate-rollout.md](gate-rollout.md)): it does not change `soundness_ok`/
`coverage_ok`, and `--gate coverage` does not yet hard-fail on it. `ratchet.py
check`/`regressed` also cross-reference the same sidecar against the CURRENT
scorecard's contract hashes and print suspect links explicitly — a
definition-hash change is never silently absorbed into "the ratchet held".

**Known limitation**: the sidecar comparison is scoped to the single epic
directory `check_traceability.py` is invoked against — a link whose target ID
is declared in a *different* epic is invisible to that run (it will simply
have no `current_hash` to compare and is skipped, not falsely flagged).
Multi-epic sidecar aggregation is a follow-up, not yet needed for the
single-epic-at-a-time workflow `/architect`/`/close-epic` already use.

## JUSTIFIED waivers (#169)

An uncovered/untested contract isn't always a bug — sometimes coverage is
deliberately deferred (e.g. manual QA only for this release) and pretending
otherwise with a fake `@cw-trace guards`/`verifies` annotation would be a lie.
The LOBSTER pattern's fix: a first-class waiver record, distinct from both
"OK" and "gap".

Waivers live under `docs/epics/<slug>/justifications/*.json`, one file per
waiver, diffable and committed like any other epic artifact:

```json
{
  "id": "CTR-order-002",
  "reason": "manual QA only for this release; automated coverage tracked separately",
  "approver": "jane@example.com",
  "expiry": "2026-12-31",
  "ticket": "#170"
}
```

All five fields are required. **A justification without a real `ticket` ref is
invalid** — per the ticket-every-deferral doctrine, a waiver is not a way to
skip opening a tracking ticket. The ref must match one of the accepted forms:
`#123`, `owner/repo#123`, an `http(s)` issue URL, or a JIRA-style `KEY-123` —
placeholders (`"none"`, `"N/A"`, whitespace) are rejected, and an invalid
record does NOT satisfy coverage (it's reported under
`invalid_justifications`, and the contract stays uncovered/untested). An
**expired** justification (`expiry` has passed) also does not satisfy coverage
— it's reported under `expired_justifications` so a stale waiver is visible,
not a silent pass forever.

A valid, non-expired justification for a currently uncovered/untested contract
moves it out of `uncovered_contracts`/`untested_contracts` into
`justified_contracts` — `coverage_ok` becomes true honestly, because the gap
is now a documented, ticket-tracked decision instead of an unexplained miss. A
justification for an already-covered contract, or for an ID that isn't even
defined, has no effect (not reported as JUSTIFIED — there's nothing to waive).

Note the `justifications/` subtree is excluded from ID/hash scanning (a
waiver's own `"id"` field names the CTR/INV it waives, not a new declaration).

**Adoption grandfathers (#215)** mirror the same mechanics for brownfield
repos: the checker resolves `<meta root>/adoption/grandfathered.json`
(written by `adopt.py grandfather`; override with `--grandfather PATH`),
keyed `check_traceability:uncovered:<ID>` / `check_traceability:untested:<ID>`.
A gap matching a **non-expired** entry moves into `grandfathered_contracts`
(reported, non-blocking under `--gate coverage`); an **expired** entry does
not waive — the gap blocks again, labeled **EXPIRED grandfather**. See
[adopt.md](adopt.md) and `chief_wiggum.grandfather`.

## Coverage-requirement alternatives (#169)

By default any `verifies` link — from a test, probe, policy, or telemetry
artifact — satisfies a contract's test coverage. A contract may instead
declare which kinds are acceptable, with OR semantics, via a JSON model entry:

```json
{"id": "CTR-order-005", "coverage_requires": ["unit-test", "integration-spec"]}
```

`CTR-order-005` is then untested unless a `verifies` annotation's `source_kind`
matches ONE of the declared alternatives — a `telemetry`-only signal, for
example, would no longer count if only `["unit-test", "integration-spec"]` are
declared acceptable. Omitting `coverage_requires` for an ID preserves the
original "any verifying kind counts" behavior.

## Per-language emitter seam + coverage metadata (#162)

`emit_source_annotations` moved to `chief_wiggum.trace_emission` (re-exported
here unchanged) so it can sit BEHIND `scripts/emitters/` — a per-language
`emit(path, content) -> [Fact]` interface with one module per Go/Python/
TypeScript, delegating to the same emission function every language shares.
The declared support matrix (which language has which capability, and its
maturity tier) lives in `config/languages.json`, rendered to
`docs/languages.md` by `scripts/render_languages_doc.py`.

`SOURCE_EXTS` is now derived from that matrix (`chief_wiggum.languages.
all_known_extensions()`) plus this checker's own verification-artifact
extensions (`.rego`/`.yaml`/`.yml`) — identical set to the pre-#162 hardcoded
list. A file whose extension the matrix doesn't recognize at all (neither a
tier-1 emitter nor the generic regex tier) is **never silently dropped**: a
full `--source` scan counts every such file (`unsupported_extension_counts`)
and surfaces one aggregated `warnings` entry, e.g. `"3 file(s) skipped: no
emitter coverage for recognized-but-unsupported extension(s) .php (2), .cpp
(1) — see config/languages.json"` — in both `--gate` and plain (query) output.
