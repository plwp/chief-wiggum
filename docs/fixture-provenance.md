# Fixture provenance

*Gate: `scripts/check_fixture_provenance.py` — report-only (chief-wiggum#351).*
*Recorder: `scripts/record_capture.py`.*

## Mock–model collusion

`harness/scp_fixture.go` routed requests by `TrimPrefix(path, "/api/gx-agent/")` and looked up scripts by tool **name** — the exact same wrong assumption as `internal/scp/client.go`'s endpoint builder.

Client and fixture agreed. Every test was green. The route bug stayed invisible until a real call.

The same shape in the engine: `FakeVoiceEngine` emitted turn events in the cascade order the state machine expected. The real native-audio API never emits those, so the session sat in `listening` dropping model output — found only on a real audio turn.

> When one worker authors the code and its test double from a single assumption, the test validates the assumption rather than reality.

TDD-with-fakes makes the fake the spec, and a green suite then measures nothing but the author's self-consistency. This is the root cause the other three gates in this family are symptoms of.

## The rule

A double standing in for a declared external system (`"external": true` in `contracts.json`) must be derived from at least one real captured interaction, and must say where it lives:

```go
// @cw-fixture SCP capture=testdata/captures/scp-venue-info.json
func newSCPFixture(t *testing.T) *httptest.Server { ... }
```

and the capture must carry its own provenance:

```json
{
  "captured_at": "2026-08-23T04:11:00Z",
  "source": "GET https://scp.example.com/venue-info",
  "response": { "status": 200, "headers": {...}, "body": {...} }
}
```

`@cw-fixture` is the fourth tag in the `@cw-*` family, after `@cw-writes` (#93), `@cw-emits` (#170) and `@cw-smoke` (#353).

| state | meaning |
| --- | --- |
| `recorded` | a fixture citing a capture that exists and is attributed |
| `hand_authored` | a fixture with **no** `capture=` — invented, so it can agree with the code and both be wrong. **The finding** |
| `missing_capture` | a capture is cited and the file is not there |
| `unattributed` | the capture exists and records neither when nor whence |
| `no_fixture` | no double for this system at all — **reported, never a finding** |

`no_fixture` is deliberately not a finding. Not every external system needs a double, and inventing that requirement would be noise on correct work.

## The evasion this closes

Requiring only that a `capture=` file *exists* would be theatre: `touch testdata/capture.json` satisfies it. That's why the capture must record `captured_at` or `source` — without them an invented file satisfies the gate exactly as well as a real recording does, and the whole check means nothing.

For a non-JSON capture (a raw body dump is legitimate), non-empty content is the most that can honestly be asserted. An empty file is `unattributed`.

## One good fixture does not vouch for a bad sibling

A system is only as recorded as its least-sourced double. If one fixture cites a capture and another for the same system doesn't, the system is `hand_authored` — the unrecorded one is exactly where the collusion lives.

## The recorder

```bash
python3 scripts/record_capture.py \
    --url https://scp.example.com/venue-info \
    --system SCP \
    --out testdata/captures/scp-venue-info.json
```

It stamps the provenance the gate requires and prints the annotation to paste.

**Secrets never enter a capture.** Request header values are recorded as `<not recorded>` — *every* header, not just the obviously-secret names, because guessing which custom header carries a credential is exactly the guess that leaks one. Request bodies are recorded as present/absent only. Response headers are kept in full: they are what the double must reproduce.

A 4xx/5xx is still a real interaction and is recorded — the double has to reproduce error shapes too. A failure to *connect* writes nothing, because a capture of a connection error is not a capture of the system.

## What this cannot do

It cannot tell whether the capture is **faithful**. A recorded response can be hand-edited afterwards, and nothing here re-runs it.

That's what `docs/external-smoke.md` is for: one real round-trip, whose skip is loud. The two are complementary — this one asks *where did the double come from*, that one asks *does the real thing still answer that way*.

## Promotion

Report-only per `docs/gate-rollout.md`. No `--gate` until a passing `docs/quality/validation/check_fixture_provenance.json` record exists. Seed classes that record must cover:

- **omission** — a double with no `@cw-fixture` annotation at all
- **config indirection** — a capture path that resolves only under a build tag
- **instrumentation-deleted** — the `capture=` attribute removed while the fixture stays
- **sampling gaps** — one recorded fixture masking a hand-authored sibling for the same system
