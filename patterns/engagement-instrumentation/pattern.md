# Pattern: Engagement Instrumentation

- **Category:** monitoring-feedback
- **Trust class:** produces trust-tagged signal (each metric annotated with what it proves)
- **Status:** specified (spec complete; `scaffold/` not yet built)

## What it is

The instrumentation tier that captures **how the product is actually doing** —
how far users get through the work/content assigned to them — in a way that is
robust against out-of-order reports, replays, concurrent writers, and client
clock/duration drift. It produces per-item and rolled-up completion/engagement
signals, each annotated with its trust class.

This is the **companion to the [improvement-loop](../improvement-loop/pattern.md)
pattern**: strong monitoring and feedback is that loop's one enabling condition,
and this pattern is how you *build* that signal deliberately rather than hoping it
exists. Its per-signal trust annotation is exactly what the loop's
[trust model](../improvement-loop/pattern.md#trust-model) consumes to decide
whether a signal-derived change may auto-deploy or must be admin-approved.

## When to apply

Apply to any content or task product where "did the user actually engage with /
complete what they were given" is a question worth answering — which is nearly
all of them. It is table-stakes for retention/funnel analysis, and the correctness
subtleties below are subtle enough that most teams get them wrong.

Do **not** bolt it on as an afterthought: the value is in the *server-trusted,
monotonic* discipline, which has to be designed in, not patched later.

## Mechanism — generic components

Neutral throughout; the "work item", completion threshold, and player/event
source are [parameters](#parameters).

### Client-side throttled emitter

- Subscribe to the content/player events (progress tick, pause, end, teardown).
- **Throttle** steady-state progress ticks (e.g. one report per ~10s); **flush
  immediately** on meaningful boundaries (pause / end / unmount).
- The unmount flush uses a **keepalive** request so it survives in-app teardown.
- **Skip reporting while no trusted denominator exists yet** (don't emit progress
  you can't ground).
- **No retry** beyond the next natural event — safe *because* the server latch is
  monotonic, so a lost report only delays, never loses, eventual state.
- A **"preview never writes"** guard: reporting is disabled entirely unless a real
  assignment context is present, so admin/preview views never pollute telemetry.

### Server-trusted monotonic tracker

- **One row per (assignment, work-item)**, created lazily on first report,
  tenant-scoped, never deleted by user action.
- The client self-reports position + duration, but completion fraction is computed
  **only from a server-trusted denominator** (a duration/size snapshot pinned at
  assignment time, or the canonical item record). The **client-reported duration
  is stored as advisory telemetry only** — never as the denominator.
- Progress advances with a **monotonic-max** operator so it never regresses;
  **"completed" is a one-way latch** that trips past a threshold fraction and never
  unsets.
- Writes are a single **upsert**: `max` for the monotonic latches, `set` for
  last-activity, `set-on-insert` for immutable fields. A duplicate-key on
  concurrent first-insert is caught and retried once.
- Roll-up summaries use a **bounded query budget per page** (one aggregation + one
  lookup), never per-item fan-out.

### Trust-boundary honesty note (per signal)

- Alongside each metric's contract, write down **what it can and cannot prove**:
  it is self-reported; the server hardens it against *accidental* corruption
  (trusted denominators, monotonic latches, server-side attribution) but the
  measured party can still inflate their *own* diligence — accepted when the only
  person they can fool is themselves.
- Enumerate known races and their **bounded, non-security** impact as accepted
  limitations rather than hiding them.
- This makes each signal's **trust class explicit** so nothing downstream — least
  of all a self-improvement loop — optimizes against a gameable proxy.

### Dual-scope audit log (complementary event source)

- Two append-only logs: a **tenant-scoped** one (actions a tenant should see,
  flowing through the tenant-isolated repository) and a **system-global** one
  (provider-less background/operational events).
- Entries carry actor, action, target, timestamp, optional metadata. A rich,
  immutable event source for behavioral and operational analysis.

## Parameters

| Parameter | What it is |
|--|--|
| `work_item` | the entity whose engagement is tracked |
| `completion_threshold` | fraction past which the one-way "completed" latch trips |
| `trusted_denominator` | server-side source of truth for the completion denominator (pinned-at-assignment snapshot or canonical record) |
| `throttle_interval` | steady-state client report cadence |
| `event_source` | the player/content events the client emitter subscribes to |
| `signal_trust` | per-metric trust class annotation (feeds the improvement loop) |

## Success metrics

This pattern *is* a monitoring pattern, so its own metrics are about the health of
the instrument — the signal quality every other pattern's metrics depend on:

| Metric | Goal | What it measures |
|--|--|--|
| `signal_coverage` | ↑ | % of assigned work-items with tracked engagement |
| `latch_integrity` | ↑ | the monotonic-latch never-regressed invariant holds (violations → 0) |
| `funnel_resolvability` | ↑ | drop-off computable per funnel step (trusted denominator, no gaps) |

## Relationship to other patterns

- **Feeds `improvement-loop`.** The per-item completion rates, drop-off points,
  and last-activity timestamps are the funnel/retention signal the loop consumes;
  the audit logs are a second event source. The per-signal trust annotation is the
  input to the loop's trust model.
- **Depends on a tenant-isolation floor.** Mining per-tenant behavioral data is
  only safe atop server-derived tenant scoping (see the
  `multi-tenant-isolation` candidate in [`registry.json`](../registry.json)) —
  the fail-closed data layer is what lets the loop read across tenants without
  leaking between them.
- **Reuses CW disciplines.** The trust-boundary note is the product-signal analog
  of CW's contract discipline; the monotonic latch + fail-closed race handling are
  the same "ratchet, never slide" stance CW applies to quality, applied here to a
  metric.
