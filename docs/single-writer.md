# Single-writer: mechanically enforcing "single write path" invariants

Some invariants declare that a field or state has exactly one sanctioned mutator —
a **single write path** / **single source of truth**. Prose alone can't enforce
this, and neither can the other CW gates:

- **Traceability** (`check_traceability.py`) proves a contract↔code↔test *link*
  exists — not *who else* writes the field.
- **The ratchet** (`ratchet.py`) protects the pass-set and contract definitions —
  it never inventories field writers.

So a second writer slips through. This actually happened: a "Billing" epic
declared **INV-BIL-001** ("single atomic Stripe→plan write"). The reconcile and
overlay features honoured it — but a pre-existing admin control (`ChangePlan`, a
plan dropdown from an earlier epic) was a SECOND writer of the same
`provider.stripe_plan` field, and nothing flagged it. `scripts/check_single_writer.py`
is the mechanical check that would have caught it.

## The convention

A single-write-path invariant names two things, machine-readably:

- **`controls_field`** — the fully-qualified field path(s) with a single write
  path, e.g. `provider.plan`, `provider.stripe_plan`.
- **`sanctioned_writers`** — the ONLY authorized writers: a **symbol** (a
  function/method name like `ReconcileStripe`) and/or a **file path** (like
  `internal/billing/reconcile.go`). Any other writer of the field is a violation.

It attaches to an invariant in either of two carriers (mirroring how traceability
supports both structured models and prose):

**Structured** — on a `state-machines.json` `invariant` object (schema:
`templates/formal-models/state-machine-schema.json`):

```json
{
  "id": "INV-bil-001",
  "description": "single atomic Stripe→plan write",
  "category": "consistency",
  "controls_field": ["provider.plan", "provider.stripe_plan"],
  "sanctioned_writers": ["ReconcileStripe", "internal/billing/reconcile.go"]
}
```

**Prose** — a `@cw-writes` namespaced tag next to the `**INV-...**` label in
`invariants.md` (same LOBSTER-style shape as `@cw-trace`; attributes are
comma-separated and order-free):

```markdown
**INV-bil-001**: single atomic Stripe→plan write / single write path
<!-- @cw-writes INV-bil-001 controls_field=provider.plan,provider.stripe_plan
     sanctioned_writers=ReconcileStripe,internal/billing/reconcile.go -->
```

Backward-compatible: invariants without this metadata are plain prose and are
silently skipped — exactly like `check_traceability.py` when IDs are absent.

## How matching works

- **Field tokens.** `provider.stripe_plan` matches writes to its leaf token
  (`stripe_plan`), case-insensitively and tolerant of the camelCase form
  (`StripePlan`) and the compacted form (`stripeplan`). So Go structs, snake bson
  keys, and JSON tags all match the same declaration.
- **Writer detection** (Go + Mongo-aware, written to be reasonably general):
  - assignment — `x.Plan =`, `p.StripePlan := v`
  - struct-literal / map set — `Plan: v`, `"plan": v`, `Key: "plan"`
  - bson/Mongo update — a quoted field literal inside a `$set` / `UpdateOne` /
    `FindOneAndUpdate` / `bson.M` context (a bare `"plan":` in a plain DTO is *not*
    counted)
  - SQL — `UPDATE ... SET plan = ...`
- **Sanctioned?** A writer is sanctioned if its nearest enclosing function/method
  equals a symbol entry (case-insensitive), OR its file matches a file entry (as a
  path suffix). **Test files** (`*_test`, `*spec*`, `e2e/…`) are treated as
  fixtures, never violations.

The checker proves *no unsanctioned write site exists*; it does not prove the
sanctioned path is semantically correct (that's the DbC verification frontier, and
LSP symbol resolution is the cheaper precision upgrade over regex here).

## The checker

```bash
# design-time (/architect Step 5a): metadata must be well-formed; surfaces writers
python3 scripts/check_single_writer.py "$CW_TMP" --source "$TARGET_REPO" --gate soundness

# close-time (/close-epic Step 2e): hard-fail on any unsanctioned writer
python3 scripts/check_single_writer.py docs/epics/<slug> --source . --gate coverage

# report only (no gate), JSON or text
python3 scripts/check_single_writer.py docs/epics/<slug> --source . --format json
```

Gates (mirroring `check_traceability.py`):

- `--gate soundness` — fails only on **malformed metadata** (a `controls_field`
  without `sanctioned_writers`, or vice-versa). Existing writers are *surfaced,
  not failed on*, since the fix may be part of the epic being architected.
- `--gate coverage` — hard-fails on any **unsanctioned writer** (and on malformed
  metadata).

Exit codes: `0` ok, `1` gate violation, `2` usage error.

## Worked example (the incident)

Given the `INV-bil-001` invariant above, a repo with the sanctioned reconcile
writer plus a legacy admin `ChangePlan`:

```
internal/billing/reconcile.go:  func ReconcileStripe(...) { p.StripePlan = ... }   # sanctioned
internal/admin/handlers.go:     func ChangePlan(...)      { p.StripePlan = ... }   # SECOND writer
```

`--gate coverage` reports:

```
## Unsanctioned writers (single-write-path violations)

- INV-bil-001 field `provider.stripe_plan` written at internal/admin/handlers.go:3 in ChangePlan()
    p.StripePlan = newPlan
```

and exits `1` — the exact regression that shipped silently before this check.
