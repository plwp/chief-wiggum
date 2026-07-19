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

All five fields are required. **A justification without a `ticket` ref is
invalid** — per the ticket-every-deferral doctrine, a waiver is not a way to
skip opening a tracking ticket, and an invalid record does NOT satisfy
coverage (it's reported under `invalid_justifications`, and the contract stays
uncovered/untested). An **expired** justification (`expiry` has passed) also
does not satisfy coverage — it's reported under `expired_justifications` so a
stale waiver is visible, not a silent pass forever.

A valid, non-expired justification for a currently uncovered/untested contract
moves it out of `uncovered_contracts`/`untested_contracts` into
`justified_contracts` — `coverage_ok` becomes true honestly, because the gap
is now a documented, ticket-tracked decision instead of an unexplained miss. A
justification for an already-covered contract, or for an ID that isn't even
defined, has no effect (not reported as JUSTIFIED — there's nothing to waive).

Note the `justifications/` subtree is excluded from ID/hash scanning (a
waiver's own `"id"` field names the CTR/INV it waives, not a new declaration).

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
