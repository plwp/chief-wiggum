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

# ticket-scoped speed-up (/implement, /implement-wave): scan only what changed
python3 scripts/check_single_writer.py docs/epics/<slug> --source . --changed-since main

# hash-derived version (source of the scanner + its chief_wiggum deps)
python3 scripts/check_single_writer.py --scanner-version
```

Gates (mirroring `check_traceability.py`):

- `--gate soundness` — fails only on **malformed metadata** (a `controls_field`
  without `sanctioned_writers`, or vice-versa). Existing writers are *surfaced,
  not failed on*, since the fix may be part of the epic being architected.
- `--gate coverage` — hard-fails on any **unsanctioned writer** (and on malformed
  metadata).

`--changed-since <ref>` scopes the `--source` scan to files that differ from
`<ref>` (committed diff + dirty tracked + untracked, via
`chief_wiggum.manifest`) instead of walking the whole tree — a fast per-ticket
signal for `/implement`/`/implement-wave` (report-only there; see those
skills). **Whole-repo scanning remains the default, and `/close-epic --gate
coverage` NEVER passes `--changed-since`** — the coverage gate must see every
writer in the repo to be authoritative; a scoped scan can only ever report a
false "no writer found"/"uncovered" for code outside its window, never prove
absence of a violation.

`--scanner-version` prints a hash of the scanner's own source plus its
`chief_wiggum` dependencies (`chief_wiggum/manifest.py`, `chief_wiggum/hashing.py`)
— the version IS the content hash, so there's no hand-bumped constant to forget
to update when the detection logic changes.

Exit codes: `0` ok, `1` gate violation, `2` usage error.

### Emission/claim seam (internal)

`scan_writers` is implemented as two pure phases (#160): `emit_write_sites(path,
text) -> list[WriteSite]` finds every FIELD-AGNOSTIC candidate write site in one
file's content — an assignment/struct-literal/quoted-literal/SQL-SET token, its
line, and its enclosing symbol — with no knowledge of any invariant.
`match_writers(sites, invariant) -> list[Writer]` is the query-time join: does
any site's token belong to THIS invariant's controlled field, honoring
`persistence_only`? This is the seam a future content-addressed cache would key
off (`chief_wiggum.manifest.build_manifest`) — a file's emitted sites are valid
cache entries as long as its content hash is unchanged.

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

## Infra extension: terraform drift as sanctioned-writer enforcement (#165)

The single-writer idiom above inventories *code* writers of a field. Some
invariants declare a single writer of *infrastructure* instead: "terraform owns
env/secrets; CI only pushes images." That rule lived only in a memory file until
the Dogeared deploy's `enable_cicd` footgun made the gap concrete — a CI run
silently applied an infra change out-of-band, and nothing flagged it, because no
check inventories *live* infra writers the way `check_single_writer.py`
inventories code writers.

`scripts/check_infra_writer.py` closes that gap by treating `terraform plan` as
the writer-inventory tool: if the live state ever diverges from the declared
(terraform) state, exactly one thing could have written it out-of-band, and
`terraform plan -detailed-exitcode` already knows how to detect that.

### Declaration

An infra invariant lives in a JSON config (default
`docs/system/infra-invariants.json`):

```json
[
  {
    "id": "INV-infra-001",
    "controls_field": "infra.env-secrets",
    "sanctioned_writers": ["terraform"],
    "terraform_root": "infra/",
    "schedule_note": "run nightly via cron"
  }
]
```

- **`id`** — an `INV-` stable ID (same grammar as `check_traceability.py` /
  `check_single_writer.py`; validated against the shared ID grammar
  (`scripts/chief_wiggum/trace_ids.py`) when it exists on the branch, else a
  local regex — the import is optional/guarded so this checker works standalone).
- **`controls_field`** — the scope name (e.g. `infra.env-secrets`), matched
  against exemption `scope` for break-glass downgrade.
- **`sanctioned_writers`** — the only authorized writer(s) (normally `["terraform"]`).
- **`terraform_root`** — the directory `terraform plan` runs in for this
  invariant, **repo-relative**. It resolves against the target repo's root — an
  explicit `--repo`, else the nearest ancestor of the config file containing
  `.git`, else the config file's directory — never the caller's CWD. Roots that
  escape the repo boundary (absolute paths, `..` traversal) are rejected as
  errors: the config is committed data and must not be able to point the
  scanner (or the journal) outside the repo it lives in.
- **`schedule_note`** — optional free text (e.g. "nightly cron", "on every close-epic run").

### The check

For each declared invariant, `check_infra_writer.py` runs (via `subprocess`, never a
shell):

```
terraform plan -detailed-exitcode -input=false -lock=false -no-color
```

in `terraform_root`, and maps terraform's own exit-code contract:

| Exit code | Meaning | Status |
| --- | --- | --- |
| `0` | declared state matches live state | `clean` |
| `2` | live state diverges from declared state — an unsanctioned write happened out-of-band | `drift` (or `exempted`, see below) |
| `1` | terraform itself errored (auth/network/config) | `error` — **never conflated with drift** |
| other | unexpected | `error` |

`terraform` missing entirely degrades gracefully: `{"available": false, ...}`,
exit `0` — mirroring `lsp_query.py`'s missing-language-server path. This is the
**one** graceful-degradation exception (intended rollout behavior). Every other
failure to evaluate — terraform exit `1`, a missing or repo-escaping
`terraform_root`, an unparseable/malformed declaration, a failed journal write —
is an `error`/`malformed` finding that **fails `--gate`**: a blocking gate must
fail when it could not actually evaluate the invariant, otherwise "terraform is
broken" silently reads as "no drift". None of these are ever conflated with
`drift` in the report.

### Drift is an event, not just a state

Every detected drift (`drift` or `exempted`) appends an **append-only** JSONL
record to `docs/quality/infra-drift.jsonl` **in the target repo** (resolved
against the same repo root as `terraform_root`, never the caller's CWD):

```json
{"ts": 1752921600.0, "invariant": "INV-infra-001", "root": "infra/", "plan_summary_first_40_lines": ["~ update in place", "..."]}
```

A later clean plan (someone reconciled the drift, or terraform re-applied) does
**not** erase this record — convergence is not innocence. The journal is the
durable evidence that an out-of-band write occurred, independent of whether it
was later fixed.

The drift **finding is recorded before** the journal write, so a failed write
can never swallow it: a journal-write failure is reported as an explicit
(gate-failing) `error` finding alongside the drift, and report-only mode still
renders the full report instead of crashing.

### Break-glass: committed exemption records

An incident sometimes requires a deliberate, temporary out-of-band change (the
break-glass case GitOps assumes exists). Rather than silently accepting drift,
`check_infra_writer.py` looks for committed exemption records in
`docs/system/exemptions/*.json`:

```json
{
  "scope": "infra.env-secrets",
  "reason": "emergency secret rotation during INC-42",
  "expiry": "2026-08-01",
  "approver": "pat",
  "incident_ref": "INC-42"
}
```

- An **active** exemption (`scope` matches the invariant's `controls_field`, and
  `expiry` has not passed) downgrades a `drift` finding to `exempted` — it is
  still journaled (see above), just not gate-failing.
- An **expired** exemption is itself a finding: the break-glass window closed
  and nobody re-declared or cleaned it up. Expired exemptions gate-fail
  independently of whether any current drift matches their scope.

Creating an exemption is a single JSON file commit — frictionless by design, so
it happens *during* an incident, not as after-the-fact paperwork.

### Authority boundary

Every report — text or JSON — states the same authority line:

> proves declared state matches live state at scan time for scanned roots; does
> not prove no out-of-band write occurred between scans

This is the same class of caveat the code-level checker states for its regex
lens: the tool proves what it can observe, not everything that could have
happened. Audit-log integration (proving *no* out-of-band write happened
*between* scans, not just checking whether one has left a live trace) is a
deferred trigger item.

### Running it

```bash
# report-only (default): prints findings, exit 0
python3 scripts/check_infra_writer.py --config docs/system/infra-invariants.json

# JSON output
python3 scripts/check_infra_writer.py --format json

# explicit repo root (otherwise derived from the config file's location)
python3 scripts/check_infra_writer.py --repo /path/to/target-repo

# blocking: exit 1 on unexempted drift, an expired exemption, or any
# error/malformed finding that prevented evaluating an invariant
python3 scripts/check_infra_writer.py --gate
```

Per `docs/gate-rollout.md`: this gate ships **report-only**. Validate it against
a real repo's terraform (a clean-plan run, and a seeded drift caught in a
sandbox workspace) before wiring `--gate` into `/close-epic` for repos that
declare infra invariants.

Exit codes: `0` ok / report-only, `1` gate violation, `2` usage error.
