# The performance gate

`scripts/check_perf.py`, run by `/saas-gate` (chief-wiggum#524). It is report-only by default and blocks under `--gate` ([gate-rollout.md](gate-rollout.md)).

## Why it exists

A delivered staff list took 20 s on its first production read. A write-side sweep ran inline on a GET, one store write per item, under a 20 s deadline. Nothing caught it:
- Staging measured 1.4 s because it had no backlog.
- A static review two days earlier looked at a different caller of the same function.
- No screen had a latency budget, so no test could fail.

A measured follow-up found six more slow paths. Most were one of three shapes: writes on reads, unbounded windows and N+1 reads.

## The budget

The budget is one field, `latency_budget_ms`:
- on an operation in `contracts.json` (request to last byte);
- on a page in `ui-spec.json` (first load to the screen's last API response).

`/architect` writes it. If it is left out:
- a GET operation gets 2000 ms;
- a signed-in (staff) screen gets 2000 ms;
- a public screen has no budget and is not walked against one.

The gate reports which budgets were declared and which were defaulted.

## What it checks

| Part | Command | Fails when |
| --- | --- | --- |
| Static rules | `check_perf.py static <repo>` | a finding (file:line) |
| Cold walk, API | `check_perf.py walk --models <dir> --base-url <url>` | an operation is over budget on the first or the warm load, errors, or does not answer |
| Cold walk, screens | `check_perf.py walk --models <dir> --cold-har <f> --warm-har <f>` | a screen's time-to-usable is over budget |
| Production p95 | `check_perf.py telemetry --project P --service S --region R` | never; it prints one line for the morning briefing, flagging endpoints over budget |
| Self-test | `check_perf.py self-test` | the planted defect does not fire, or the fixed shape does |

`saas_gate.py` runs the static rules on every run. With `--perf-models`, it also runs the walk: against `--base-url`, and against `--perf-cold-har` / `--perf-warm-har` when given. All of these land in its `performance` category.

### Static rules (Go services)

- **`write-on-read`**: a persistence write reachable from a GET handler on the request path. Writes include Firestore transactions and `Set`/`Create`/`Update`/`Delete`, SQL `ExecContext`, and Mongo writes. A goroutine the handler starts and never joins is off the path, which is how the #434 fix runs its sweep. A goroutine followed by `.Wait()` or a channel receive is on the path.
- **`unbounded-list`**: a list query (`Documents`, `DocumentRefs`) with no `Limit`, no range filter and no key lookup. A filter on a tenant field alone (`site`, `tenant`, ... or `--scope-field`) is every record the tenant ever had, so it is not a window.
- **`read-in-loop`**: a store read inside a loop over a result set in GET-reachable code. The read can be direct or through a call. Ranges over literal lists and three-clause counter loops (retries, pages) are not result sets.

GET handlers are functions taking `http.ResponseWriter` and `*http.Request` that are registered or referenced as values. Whole handlers are left out when they are registered for other methods only: Go 1.22 `"POST /path"` patterns, router verbs (`r.Post`), and route-table rows naming `http.MethodPost` next to the handler.

Inside a handler, code is cut away only where it is **proven** not to serve a GET. When in doubt it is kept, because pruning on a guess is how a write on GET goes unreported (PR #527 review):
- A block is cut when no GET can enter it. Its condition is either a pure method test that excludes GET (`r.Method == http.MethodPost`, `r.Method != http.MethodGet && r.Method != http.MethodHead`) or a conjunction with such a conjunct (`verb == "cancel" && r.Method == http.MethodPost`). The same goes for `switch r.Method` cases and tagless-switch cases.
- The code after a block is cut only when every GET enters the block (a pure method test admitting GET) and the block's last top-level statement is a `return`. The cut runs only to the end of the enclosing block. A `return` nested inside another `if` proves nothing.
- Compound conditions such as `r.Method != http.MethodGet || needsRepair` are never pruned.
- A `default:` is cut only when a proven GET case precedes it in the same switch.

Each finding gives the site as file:line, the GET handler, and the call chain.

A generic store verb (`x.Set(ctx, …)`, `x.Get(ctx)`) counts as the library's call unless the receiver is provably the repo's own type: the call is on the enclosing method's receiver, and that type defines the verb. A same-named method elsewhere in the repo (`Settings.Set(key, value)`) proves nothing about the receiver, so it does not hide the call.

The scanner never reads `node_modules`, `.next`, `vendor`, `dist`, `build`, `out`, `coverage`, `testdata`, `fixtures` or dot-directories. It also skips test files and generated files. Only call sites count, never declarations: an interface method `AppendAction(ctx ...) error` and a struct field `Set func(...)` are not writes.

A reviewed exception is marked `// cw-perf: ok <reason>` on the line:
- on a call, the gate does not follow that call;
- on a finding's line, the finding is dropped.

Suppressions are counted in the output.

### The cold, production-shaped walk

The walk needs a non-production instance whose data keeps production's backlogs.

1. Shape the seed from aggregate counts only, for example requests, replica bookings and pending automatic actions per site. Never copy records out of production.
2. Use invented names, `*.invalid` emails and 0491 570 phones.
3. Time each declared GET twice, first load then warm, with a QA principal's `Authorization` header from `--auth-header-env`. The header is never printed.

The walk never fires a mutating request without `--probe-mutating`. Like `check_boot_and_hit.py`, whose route inventory and path substitution it reuses, it never invents a path parameter. A parameterised route with no `--path-param` is `unprobeable`, and a 401/403 on either load is `gated`. Neither counts as a pass. Both loads' statuses are kept (`cold_status`, `warm_status`): an error on either load is `error_status`, even when the other succeeded.

For screens, record a HAR from the browser walk (Playwright `record_har_path`), once on first load and once warm, and pass both files. Time-to-usable is the end of the screen's last request. The record keeps the full waterfall. A screen fails when:
- it is over budget;
- its document or any of its API calls answered with an error or never completed (`error_status`; timing an error page is not a walk);
- it appears in only one of the two HARs (`incomplete`; a missing cold load misses exactly the regression this gate is for).

A declared screen that neither HAR contains is reported as `not_walked`.

### Production telemetry

`telemetry` reads Cloud Run request logs with `gcloud logging read`, which is read-only. It groups requests by the contracts' routes, plus any `--route` templates for routes the contracts do not declare, so tenant slugs are grouped rather than printed. It prints, for example:

```
Perf (24h): 26 endpoints, 1648 requests, 2 over budget. OVER GET /api/staff/{site}/bookings p95 20.1 s (p50 2.1 s, budget 2.0 s, n=14); ...
```

## What it does not see

- **Go only.** Static rules on another stack report `inapplicable`, never `pass`. A Next.js `route.ts` handler is not read.
- **The call graph is by name.** An interface call reaches every method of that name whose first parameter is a context, and a call on the method's own receiver reaches only its own type. That is deliberate: it over-reaches rather than miss a write behind an interface. Calls through function values (callbacks passed by name) are not followed.
- **No flow-sensitivity.** A branch that is POST-only because of an earlier compound condition (`case photo:` after `if !(GET && !photo) && != POST { return }`) still reads as GET-reachable.
- **No cache awareness.** A read inside a loop is reported even when a cache absorbs it (a per-tenant pricing cache, for example).
- **`unbounded-list` checks that a bound exists, not that it is the asked window.** A query built by a caller and passed in as a parameter is not judged.
- **The walk is as good as its seed.** A walk with no backlog measures what staging measured.

## Validation

[`docs/quality/validation/perf-gate.json`](quality/validation/perf-gate.json) holds:
- the planted-defect trials (the self-test), including the six repros of the five fail-open bugs the cross-family review found on PR #527;
- the clean trial;
- the dry runs on a real shipped repo: its main, and main with the #434 fix reverted in a local scratch branch, for both the static rules and the cold walk;
- the false-positive triage.

The record is report-only. It does not yet carry the full [gate-validation protocol](gate-validation.md) seed classes or a ratchet-journal entry, so no workflow passes `--gate` to this gate yet. `/status` lists it as `report_only`: it has trial evidence but no blocking authority.
