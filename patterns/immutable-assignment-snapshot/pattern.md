# Pattern: Immutable Assignment Snapshot

- **Category:** data-structure
- **Trust class:** the published-version store is a protected path (edits must never mutate an outstanding assignment)
- **Status:** specified (spec complete; `scaffold/` not yet built — see [chief-wiggum#135](https://github.com/plwp/chief-wiggum/issues/135))

## What it is

The shape that lets you **edit a template without retroactively changing work
already assigned from it**. When a composite item (a course, a checklist, a form, a
plan) is assigned to someone, the assignment pins the exact *version* that was
published — not a live reference to the template. Later edits create a **new
immutable version**; outstanding assignments keep pointing at the old one. Nobody's
in-flight work silently changes underneath them, and longitudinal completion
analysis stays drift-free (the denominator an assignment was measured against never
moves).

The subtle, load-bearing detail (mined from real code): immutability lives in a
**write-once version snapshot created at publish time** — *not* a copy of the whole
composite stuffed into the assignment. The assignment stores only a **version id by
reference**; the version is the liability boundary that is never patched.

## When to apply

Any product where a template is *assigned* or *issued* and then edited over its
lifetime, and where an outstanding assignment must reflect the template **as it was
when assigned**: course/curriculum platforms, checklists/SOPs, forms, care/workout
plans, policy acknowledgements, quizzes. Skip it when assignments should *always*
track the latest template (then a live reference is correct — but say so explicitly,
because "edit silently changed everyone's assignment" is a classic latent bug).

## Mechanism — generic components

- **Write-once published version.** Publishing a template snapshots it into a
  `Version` record whose store exposes **only insert + find** — no update, no delete.
  The version is immutable by construction; there is no code path that patches it.
- **Editing forks a new version.** An edit to the template produces a *new* version
  (new id / version number). The template's live/draft state moves forward; every
  prior published version is frozen.
- **Assignments reference a version id, not the template.** An assignment stores the
  `version_id` (and item ids) **by reference** — it does not embed a copy of the
  composite. Hydration reads the composite from the pinned version.
- **Composite-item fields are value-copied at publish (the snapshot).** Fields that
  must not drift (titles, durations, ordering, per-item metadata) are **value-copied
  into the version at publish** and read from that pin at hydrate time — so editing
  the underlying item later doesn't leak into an outstanding assignment.
- **Deliberate mutable exceptions are documented, not accidental.** Some fields may
  intentionally read live (e.g. a directly-assigned single item's display title);
  where that's the design, it's an explicit, documented asymmetry — not an
  accidental live reference. Make the choice per field, on purpose.

## Invariant cluster

| Generic ID | Invariant | Realized as (provenance) |
|--|--|--|
| `INV-IAS-001` | Published version is write-once: no in-place patch (the version store exposes insert + find only). | dogeared-coach `db/store.go:95-118`; `models/course.go:20-40` |
| `INV-IAS-002` | Editing creates a NEW version; outstanding assignments keep referencing the old immutable version id. | `services/course.go:609-629`; `models/assignment.go:8-23` |
| `INV-IAS-003` | Composite-item fields are value-copied into the version at publish (snapshot), read from the pin at hydrate. | `services/assignment_view.go:364-389` |

## Parameters

| Parameter | Required | Description |
|--|--|--|
| `composite` | yes | The assignable template (course/checklist/form/plan). |
| `version_store` | yes | Write-once store for published versions (insert + find only). |
| `pinned_fields` | yes | Item fields value-copied into the version at publish (must-not-drift). |
| `live_fields` | no | Fields deliberately read live at hydrate (documented mutable exceptions). |

## Success metrics

| ID | Goal | Description |
|--|--|--|
| `retroactive_mutation_incidents` | down | outstanding assignments whose content changed from a template edit (target 0). |
| `denominator_drift` | down | longitudinal completion measured against a shifted denominator (target 0). |
| `version_store_mutations` | down | in-place updates/deletes on a published version (target 0; the store forbids them). |

## Relationship to other patterns

- **`engagement-instrumentation`** — the pinned version *is* the trusted
  denominator its completion tracking measures against; without the snapshot, the
  denominator drifts and drop-off becomes uncomputable.
- **`multi-tenant-isolation`** — the version store is tenant-scoped like every other
  repository; the snapshot is orthogonal to (and composes with) the tenant floor.
