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

Token counts come from the provider's own usage summary — every consult provider
surfaces one (the CLIs via their `--output-format json` mode, the SDKs via the
response `usage`), so a `consult` event should carry `tokens_in`/`tokens_out`.
**Cost is computed, not logged raw:** `emit_consult(provider, model, tokens_in,
tokens_out)` multiplies the tokens by the grounded per-model rate in
[`config/model_pricing.json`](../config/model_pricing.json) (`factory_log.cost_for`).
That table is fetched from each vendor's live pricing page and refreshed by
`/update` — never keyed from memory. `cost_usd` is omitted (not `0`) when a model
has no price in the table, so an un-priced call still records its tokens without a
fabricated dollar figure.

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

## End-to-end token cost (Claude Code's own telemetry)

`factory_log` captures the pieces CW *runs* — gate outcomes and **consults**
(`consult_ai` emits a `consult` event per provider call: provider · model · repo,
with token/cost where a provider surfaces usage). But the biggest cost is the
**Claude Code session itself** — the orchestrator plus every sub-agent it spawns.
Claude Code emits that natively via OpenTelemetry, and CW folds it into the same
log so `/reflect` reports one end-to-end number.

**Capture it with the console exporter (no collector needed):**

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=console
export OTEL_LOGS_EXPORTER=console
export OTEL_METRIC_EXPORT_INTERVAL=10000
# run the factory session, capturing the telemetry stream:
claude <args> 2> "$CW_TMP/claude-otel.jsonl"
```

Claude Code emits per-request `api_request` events carrying `model`,
`input_tokens`/`output_tokens` (+ `cache_read_tokens`/`cache_creation_tokens`),
`cost_usd`, and **`query_source`** — which separates `repl_main_thread` (the
orchestrator) from `subagent` (delegated work). Fold them in:

```bash
python3 "$CW_HOME/scripts/factory_log.py" ingest-claude-code "$CW_TMP/claude-otel.jsonl" --repo acme/app
```

`ingest-claude-code` parses the `api_request` events (tolerant of the flat console
shape and OTLP `attributes` shape; skips everything else) and writes `claude_code`
records. It's an **explicit** ingest — it always writes, unlike passive emit. Then:

```bash
python3 "$CW_HOME/scripts/factory_log.py" aggregate --repo acme/app
# → { consults, claude_code: {repl_main_thread, subagent}, consult_cost_usd,
#     claude_code_cost_usd, cost_usd_total }   ← end-to-end
```

`aggregate` splits Claude Code cost by `query_source` (orchestrator vs subagent)
and reports `cost_usd_total = consult_cost + claude_code_cost` — the nominal cost
of a factory run end to end. `/reflect` surfaces it as a factory-log finding.

## Reading

```bash
python3 "$CW_HOME/scripts/factory_log.py" aggregate --repo acme/app   # per-gate value + consult cost
```

`aggregate` marks each gate `earning` (caught > 0), `noise-candidate` (≥3 runs, 0
caught), or `unproven` (too few runs to judge) — the input to the gate-rollout
question in `docs/gate-rollout.md`: a gate that never catches anything on real code
is a candidate to demote or delete before it trains operators to `--force` past it.
