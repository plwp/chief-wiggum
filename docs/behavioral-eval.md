# Behavioural eval

*Gate: `scripts/check_behavioral_eval.py` — report-only (chief-wiggum#354).*

## The escape this exists for

The Gemini config was built with its tools declared. The engine tests asserted the tools were **passed** to the model (`toGenaiTools` and friends). Structurally correct, and green.

There was no system instruction. The model never called any tool and answered *"I can't retrieve that."*

Nothing checked the behaviour — given a tool-shaped question, does the agent actually reach for the tool. Every test asked whether the config was well-formed, which it was.

## The rule

The target declares a **behavioural eval spec**, by default `docs/quality/behavioral-evals.json`:

```json
{
  "tools": ["get_venue_info", "list_bookings", "get_date_info"],
  "cases": [
    { "id": "venue-hours", "prompt": "what time do you open?",
      "expect_tool": "get_venue_info", "expect_contains": "9am" },
    { "id": "bookings-today", "prompt": "any bookings today?",
      "expect_tool": "list_bookings" }
  ]
}
```

It runs that set against the **real model** and emits results:

```json
{ "results": [
  { "id": "venue-hours", "status": "ran",
    "called_tools": ["get_venue_info"], "output": "We open at 9am" }
]}
```

Running the set is the target's job — it needs the target's model, credentials and harness. Requiring it, and refusing to call an unrun set a pass, is the gate's. Same division as `docs/external-smoke.md` and `docs/boot-and-hit.md`.

| state | meaning |
| --- | --- |
| `verified` | the case ran and the expected tool was called |
| `tool_not_called` | it ran, and the expected tool was **not** called. **The finding** — this is the missing-system-instruction bug exactly |
| `wrong_answer` | the tool was called and `expect_contains` was absent — it ran and its data did not reach the user |
| `unverified` | the case was SKIPPED, **or** it passed without recording which tools it called |
| `never_ran` | declared in the spec, absent from the results |
| `no_case` | a declared tool that no golden case exercises |

No spec at all is `inapplicable`, not `pass`. For a product that ships an agent, **its absence is the gap**.

## Exit status is not behaviour

A green case with no `called_tools` gets `unverified`, not `verified`.

This matters more than it looks. The original bug produced a *passing* test suite: every structural assertion held. A results file that only records pass/fail reproduces exactly that blindness — it proves the case ran, never that the tool was reached. Emitting `called_tools` is what turns the eval from a structural check into a behavioural one, and the gate refuses to pretend otherwise.

Consequently junit-xml results can never reach `verified` for a case with an `expect_tool`: junit carries no tool-call detail. It's accepted as an ingestion format because a skipped or failed eval is still worth knowing, but the full verdict needs the native shape.

## What this deliberately does not do

#354 also notes that `get_date_info`'s schema was well-typed but **semantically circular**: it required an `isoDate` in order to tell you what today's date is. The ticket is right that schema validation cannot see a purpose-level contradiction.

No lint for it ships here.

The plausible heuristic — a `get_<X>` tool whose *required* parameters include something matching `<X>` — is the same unmeasured prose-intent guess that was measured and rejected for `docs/interface-provenance.md`'s hedge lint, and there is no corpus of real tool schemas to measure its precision against. Shipping it unmeasured would be guessing, which is the habit this whole family of gates exists to break.

Recorded here as unmechanized rather than silently dropped. If a corpus of tool schemas becomes available, measure first.

## Promotion

Report-only per `docs/gate-rollout.md`. No `--gate` until a passing `docs/quality/validation/check_behavioral_eval.json` record exists. Seed classes that record must cover:

- **omission** — a declared tool with no golden case
- **instrumentation-deleted** — `called_tools` removed from the results emitter while the evals still pass
- **sampling gaps** — a skipped eval reported as a pass
- **config indirection** — the system instruction removed, which is the original bug and must produce `tool_not_called`
