# Pattern: Bidirectional Reconciliation Sweep

- **Category:** process-loop
- **Trust class:** sweep rules are destructive-capable automation — the sweep set, its guards, and its thresholds are a protected path
- **Status:** specified (spec complete; `scaffold/` not yet built)
- **Depends on:** [`fetch-on-webhook-reconcile`](../fetch-on-webhook-reconcile) — the same "the provider's live state is authoritative, never your cached belief" discipline, applied on a timer instead of a webhook
- **Feeds:** [`improvement-loop`](../improvement-loop) — per-run counts and provenance-tagged repair events are operational-health signal

## What it is

A periodic in-process job that repairs divergence between local records and an
external system (payment provider, storage, CDN config) **in both directions**,
plus a stale-in-flight sweep for records stuck mid-transition. Mined from a
shipped production SaaS's payment-hold reconciler (a fixed-order set of sweeps
on a short ticker, run once at startup, each returning a count).

Why a pattern and not "just write a cron job": a reconciler is **automation
that is allowed to destroy things** (cancel holds, abandon requests, release
capacity). The mined discipline is what keeps that safe: every repairing write
is a status-guarded compare-and-set, every transition is legality-checked
against the domain state machine first, age-based destruction refuses to act
on records with missing timestamps, and destructive repairs re-confirm against
the external system's live truth. Each of these exists because its absence is
a real bug class — double-repair under concurrent runs, illegal transitions
forced by a job, records fabricated into expiry, and repairs applied on stale
local belief.

## When to apply

- Local records mirror an external system (payments, storage, DNS/CDN config)
  and divergence accumulates: webhooks get missed, in-flight operations die.
- Some repairs are destructive (cancel, expire, abandon, release) and must be
  safe under retries, replicas, and races with live traffic.
- Operators need per-run evidence of what the sweep did (counts, provenance).

## Mechanism — generic components

- **Fixed-order sweep set on a ticker.** One scheduler runs the sweeps in a
  declared order with a per-run time budget, once at startup then on the
  interval; each sweep returns its repair count. *(INV-RSW-007.)*
- **Status-guarded CAS repair.** Every repairing write re-asserts the expected
  pre-state in the write filter; a zero-row result means a concurrent actor
  already handled it — the sweep skips, and no follow-on side effect fires.
  *(INV-RSW-001 — mined.)*
- **Legality before write.** Each repair transition is validated against the
  domain state machine before the write; an illegal transition is skipped and
  surfaced, never forced by the job. *(INV-RSW-002 — mined.)*
- **Fail-closed on unknown age.** Age/expiry sweeps exclude records whose
  ordering timestamp is absent at the query level (`$ne: nil`), and upstream
  never fabricates a timestamp to fill the gap. *(INV-RSW-003 — mined.)*
- **Re-confirm before destroy.** Before a destructive repair, fetch the
  external system's live state and treat it as authoritative; if the external
  system is unreachable, skip fail-closed. *(INV-RSW-004 — mined for the
  stale-in-flight sweep; the pattern requires it for every destructive sweep
  wherever an external truth exists.)*
- **Deterministic external idempotency keys.** Every outbound repair against
  the external system carries a namespaced deterministic idempotency key
  (`reconcile:<op>:<record-id>`), so replays across runs and replicas cannot
  double-apply. *(INV-RSW-005 — mined.)*
- **Idempotent, bidirectional flag sweeps.** Alert-flag sweeps are no-ops on
  re-run (the filter excludes already-flagged rows) and every flag-set sweep
  has a paired flag-clear sweep — flags track current state, they don't decay
  into set-once noise. *(INV-RSW-006 — mined.)*
- **Per-run counts, surfaced skips.** The run emits per-sweep counts and
  per-item provenance-tagged events (`source: <sweep-name>`); a sweep that
  cannot run (store or client unavailable) reports *skipped*, never a silent
  zero. *(INV-RSW-007 — design-derived: it strengthens the mined
  per-run-counts mechanism, whose missing-store path returned an
  indistinguishable 0.)*

## Grounding

INV-RSW-001/002/003/005/006 are **mined** from a shipped production SaaS's
payment-hold reconciler (per this registry's provenance policy, private-repo
paths are held out of the public registry; the manifest describes the realized
mechanisms). INV-RSW-004 is mined for the stale-in-flight sweep and
**generalized by design** to all destructive sweeps — the mined system applied
it to one of three destructive sweeps, and the uncovered two are exactly where
its review found risk. INV-RSW-007's surfaced-skip clause is design-derived
from the mined gap (unavailable store indistinguishable from clean pass — the
same "no-op wearing a green checkmark" failure the gate-validation protocol
names). Known v1 boundaries, stated not hidden: no leader election (the CAS
guards make concurrent runs safe but wasteful) and no persisted run ledger
(counts are emitted, not stored) — both are parameters, not invariants.

## Parameters

| Parameter | Required | Meaning |
|--|--|--|
| `sweeps` | yes | The ordered sweep set: name, selector, repair, direction (local→external / external→local / local-only). |
| `interval` | yes | Ticker period (the mined loop ran every 60s with a 2-minute per-run budget). |
| `state_machine` | yes | The transition-legality authority repairs are checked against. |
| `idempotency_namespace` | yes | Prefix for deterministic outbound repair keys. |
| `run_budget` | no | Per-run context timeout. |
| `run_ledger` | no | Where per-run counts persist (default: emitted only). |

## Success metrics

`repairs_per_run` trending ↓ (divergence shrinking), `skipped_sweeps` = 0,
`illegal_transition_skips` = 0 (each one is a model/code drift finding),
`stale_inflight_age_p95` ↓, `double_repair_count` = 0 (CAS efficacy).

## Trust

The sweep set, selectors, thresholds, and repair actions are destructive-capable
automation — a protected path. A worker adding a sweep or widening a selector is
parked for human review, exactly as with any goalpost.
