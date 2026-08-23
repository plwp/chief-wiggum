# External-integration smoke

*Gate: `scripts/check_external_smoke.py` — report-only (chief-wiggum#353).*

## The escape this exists for

An epic shipped with a live smoke test that was **optional** — behind a build tag and `LIVE_SMOKE=1` — and which only asserted `Connect`, never a real turn.

Four separate production bugs in that one epic each needed exactly **one** real end-to-end interaction to surface, and none was required to pass:

- the native-audio turn-flow bug
- the missing-system-prompt bug (the agent never called its tools)
- the trailing-newline token bug — the fakes used clean tokens; the real Secret-Manager-mounted value had a trailing `\n`, so `Bearer <token>\n` was an invalid HTTP header and every call died with an opaque transport error
- the guessed-route bug (see `docs/interface-provenance.md`)

The unit tests were green throughout. They were green because they never touched the real system.

## The rule

Every external system the epic declares — `"external": true` with an `external_system` name in `contracts.json` — needs one real round-trip, marked at the test that performs it:

```go
// @cw-smoke SCP case=TestSCPLiveVenueInfo
func TestSCPLiveVenueInfo(t *testing.T) { ... }
```

`@cw-smoke` is the third tag in the `@cw-*` family, after `@cw-writes` (#93) and `@cw-emits` (#170), and shares their module (`chief_wiggum.annotations`).

One system per tag, deliberately unlike `@cw-emits`' comma list: a test that round-trips two external systems is not one smoke, it's two smokes sharing a function, and each deserves its own verdict.

## The states, and why they're separate

| state | meaning |
| --- | --- |
| `verified` | the smoke ran and passed. **The only state that is evidence** |
| `unverified` | the smoke was **SKIPPED** — credentials absent, build tag off. Loud, blocking-eligible, never a pass |
| `failed` | the smoke ran and failed |
| `never_ran` | annotated, but no case in the results matched it — the suite didn't contain it at all |
| `no_smoke` | no `@cw-smoke` site anywhere in the source tree |
| `smoke_declared` | a static-only answer. An annotation exists; whether it ran is unknown. Explicitly **not** `verified` |

`inapplicable` means the epic declares no external system at all. If the epic *does* integrate a third party and this says `inapplicable`, the gap is the **declaration** — see the declaration-gap note in `docs/interface-provenance.md`.

## Why it parses junit itself

`ratchet.parse_junit_xml` returns passing case ids only, and collapses `skipped` in with `failure`/`error`:

```python
if outcomes & {"failure", "error", "skipped"}:
    continue
```

That collapse is correct for the ratchet — a skipped test didn't pass, so it doesn't belong in a high-water pass-set. It is exactly wrong here. This gate's entire reason to exist is that **a skip is not a failure and is not a pass**: it's an integration nobody exercised, and the epic report has to say so. Reusing that parser would have rebuilt the silent-green this ticket was filed against.

## Two things it deliberately refuses to do

**A passing sibling never masks a skipped smoke.** A system is only as verified as its weakest smoke. If one smoke passes and another skips, the system is `unverified` — masking would be the same silent-green in a smaller box.

**Weak evidence may raise an alarm; it may never grant one.** Without `case=`, matching falls back to the annotated *file*, which matches every case in that file. An unrelated passing test would otherwise award the integration a pass it never earned, so an unpinned annotation cannot reach `verified` — its best outcome is `smoke_declared`, with a note to pin it. It *can* still reach `unverified` or `failed`, because a skip or failure in the smoke's own file is worth knowing even when the annotation is imprecise. Fail-closed, in the direction that matters.

## What this does not prove

Static presence of a `@cw-smoke` site proves a smoke is *wired*, not that it ran — same limitation `check_instrumentation.py` states about `@cw-emits`. That's what `--results` is for, and why a run without it reports `smoke_declared` and says `STATIC ONLY` in the output rather than quietly returning `pass`-shaped comfort.

It also can't prove the round-trip was *meaningful*: a smoke that connects and asserts nothing satisfies this gate, exactly as the original optional smoke did. That's a review question, not a mechanical one.

## Dry-run evidence

```
epics scanned: 22
  inapplicable: 22
crashes: 0
```

Every real epic is `inapplicable` because none has adopted `"external": true` yet (it shipped in #350). That's the honest starting state for this gate, not a clean bill of health.

## Promotion

Report-only per `docs/gate-rollout.md`. No workflow passes `--gate` until a passing `docs/quality/validation/check_external_smoke.json` record exists (`docs/gate-validation.md`). The seed classes that record must cover include:

- **omission** — an external system with no `@cw-smoke` site
- **instrumentation-deleted** — the annotation removed while the test remains
- **sampling gaps** — a skipped smoke reported as a pass
- **config indirection** — a smoke disabled by a build tag or absent env var rather than by editing the test
