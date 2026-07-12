# Factory Telemetry

Post-hoc git archaeology (`/reflect`) can tell you *that* a fix commit happened,
but not a gate's duration, how many findings it caught, or an AI consultation's
token cost. Those have to be emitted **as the factory produces**. `scripts/factory_log.py`
is the append-only ledger for that, and `/reflect` reads it as the authoritative
source for the value-vs-noise and token-cost questions.

## Opt-in by default

Emitting is a **no-op** unless telemetry is enabled — so tests and CI have no side
effects:

- `CW_TELEMETRY=1` — enable, writing to the default log `~/.chief-wiggum/factory-log.jsonl`.
- `CW_FACTORY_LOG=/path/to/log.jsonl` — enable *and* redirect the log.

Enable it when you want to measure a factory run (an `/implement-wave`, an
`/architect`, a batch of gates). `/reflect` then folds the aggregates into its
report and flags gates that ran repeatedly but caught nothing (`noise-candidate`)
and the total logged consult cost.

## Event schema

One JSON object per line. Each call site fills what it **knows** and omits the rest:

```
{ts, event, repo?, ticket?, name?, result?, duration_ms?, caught?,
 provider?, tokens_in?, tokens_out?, cost_usd?, details?}
```

| event | who emits | key fields |
|--|--|--|
| `gate` | a gate script | `name`, `result` (pass/fail/error), `duration_ms`, `caught` |
| `consult` | an AI consultation | `provider`, `tokens_in`, `tokens_out`, `cost_usd` |
| `worker` | a sub-agent run | `name`/role, tokens/cost if the harness surfaces them |
| `skill` | a workflow step | `name`, `result` |

Token/cost fields are only present where the call site can measure them (e.g. an
SDK response with usage). CLI-provider consults that don't expose usage simply omit
them — better an honest gap than a fabricated number.

## Emitting

From Python (the ergonomic path for gates):

```python
from factory_log import gate_timer
with gate_timer("check_patterns", repo="chief-wiggum") as g:
    errors = run()
    g.caught = len(errors)
    g.result = "fail" if errors else "pass"
# emits {event: gate, name: check_patterns, result, caught, duration_ms} — or nothing if disabled
```

Or one-shot: `factory_log.emit_gate("ratchet", "pass", caught=0, repo=repo)`.

From a skill / shell:

```bash
python3 "$CW_HOME/scripts/factory_log.py" emit --event gate --repo "$owner_repo" \
  --name ratchet --result pass --caught 0
python3 "$CW_HOME/scripts/factory_log.py" emit --event consult --repo "$owner_repo" \
  --provider opus --tokens-in 1200 --tokens-out 400 --cost-usd 0.03
```

`check_patterns.py` is the wired exemplar (guarded so it's a no-op when telemetry is
off). Other gates and skills adopt the same one-liner as telemetry proves its worth.

## Reading

```bash
python3 "$CW_HOME/scripts/factory_log.py" aggregate --repo acme/app   # per-gate value + consult cost
```

`aggregate` marks each gate `earning` (caught > 0), `noise-candidate` (≥3 runs, 0
caught), or `unproven` (too few runs to judge) — the input to the gate-rollout
question in `docs/gate-rollout.md`: a gate that never catches anything on real code
is a candidate to demote or delete before it trains operators to `--force` past it.
