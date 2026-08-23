# Boot-and-hit

*Gate: `scripts/check_boot_and_hit.py` — report-only (chief-wiggum#352).*

## The escape this exists for

`cmd/server` had **no test files**. The mux wiring was never exercised by anything. A duplicate `mux.Handle("/")` — the server package had already registered it — panicked at startup, so the container crash-looped on Cloud Run and it was found on deploy. The demo page and `/widget.js` were likewise never wired into the server until the deploy branch.

`go build ./...` was green. Package tests were green. Neither composes the binary and asks it for a route.

`/implement-wave`'s integration check said, in full:

> **Smoke test**: If services can be started, start them and verify health endpoints respond

Two holes in one sentence: *"if services can be started"* makes it optional, and *"health endpoints"* is one route out of however many the epic declares.

## The rule

Given a **running instance** (`--base-url`), probe every route the epic's `contracts.json` says this service *serves* — every operation not marked `"external": true` (that direction is `docs/external-smoke.md`).

Reaching a base URL at all is the startup check: a binary that crash-loops has no URL to give. The route sweep is the wiring check.

Booting is the operator's job, probing is the gate's — the same division `saas_gate.py --base-url` already uses.

| state | meaning |
| --- | --- |
| `served` | 2xx/3xx, or a status the operation itself declares in `error_cases` |
| `served_gated` | 401/403 — the route **is** registered; an auth layer answered first |
| `not_served` | 404/405 — declared and not wired. **The finding this exists for** |
| `error_status` | 5xx — registered and erroring |
| `not_probed` | a mutating method, not probed by default |
| `unprobeable` | a parameterized path with no supplied substitution |
| `unreachable` | the request itself failed — the service is not up |

Without `--base-url` the outcome is `inapplicable`. Nothing was composed and nothing was asked, and a green build is precisely the false comfort this gate exists to replace.

## Two safety rules that shape the design

**It never fires a mutating request by default.** Probing `DELETE /orders/:id` against a real staging service destroys data. GET and HEAD are probed; POST/PUT/PATCH/DELETE report `not_probed` unless the operator passes `--probe-mutating`, having decided the target is disposable.

A gate that damages the system it inspects is worse than no gate.

**It does not guess path parameters.** `/orders/:id` can't be probed literally, and a 404 from a substituted id is ambiguous — it means "no such record" just as readily as "route not registered", which is the exact question being asked. Parameterized routes report `unprobeable` until the operator supplies `--path-param id=123`.

Reporting a route as broken because the gate invented an id would be a false positive on correct code, and one of those teaches the operator to `--force` past everything (`docs/gate-rollout.md`).

Neither abstention is a finding. Declining to probe is honest ignorance, not a defect in the code.

## The entrypoint rule is a conjunction

#352 asks that a service entrypoint not be allowed zero tests. It asks for the conjunction, and the conjunction is right:

> flag "entrypoint has no test **and** no boot-and-hit coverage"

An entrypoint with no unit tests whose routes were just successfully probed **is** exercised — demanding a test file as well would be cargo cult. The finding fires only when nothing tested it and nothing booted it.

"Booted" means at least one route came back `served` or `served_gated`. A sweep where every route was `unreachable` is not coverage.

## Verification

The unit tests inject a fake probe, which proves the classification logic and nothing about the transport. So the gate was also run against a **real** `http.server` serving the shape of the original bug:

```
GET   /health        -> 200   served
GET   /admin         -> 401   served_gated
GET   /widget.js     -> 404   not_served      <- the declared-but-unwired route
GET   /boom          -> 503   error_status
GET   /orders/:id    -> 200   served          (--path-param id=123)
POST  /orders        -> None  not_probed
entrypoint cmd/server/main.go: covered_by_probe
```

Then the server was shut down and the same check re-run — the crash-loop case:

```
all routes -> unreachable
entrypoint cmd/server/main.go: untested_and_unbooted
```

## Promotion

Report-only per `docs/gate-rollout.md`. No workflow passes `--gate` until a passing `docs/quality/validation/check_boot_and_hit.json` record exists. Seed classes that record must cover:

- **omission** — a declared route never registered (the original bug)
- **config indirection** — a route registered only under a build tag or env flag
- **sampling gaps** — a sweep that probed only `/health` and reported clean
- **concurrency** — a service that answers during warmup and 503s under load
