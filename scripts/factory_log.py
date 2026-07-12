#!/usr/bin/env python3
"""Factory telemetry — a production-time event log CW writes as it produces.

Post-hoc git archaeology (`reflect.py`) can't recover a gate's duration, how many
findings it caught, or an AI consultation's token cost — those have to be emitted
as the factory runs. This is the append-only ledger for that.

**Opt-in by default.** Emitting is a no-op unless telemetry is enabled
(`CW_TELEMETRY=1`, or `CW_FACTORY_LOG=<path>`), so tests/CI have no side effects.
Enable it when you want to measure a factory run; `reflect.py` reads whatever log
exists.

Event schema (one JSON object per line):
    {ts, event, repo?, ticket?, name?, result?, duration_ms?, caught?,
     provider?, tokens_in?, tokens_out?, cost_usd?, details?}

  event: "gate" | "consult" | "worker" | "skill"
  A gate records name/result/duration_ms/caught; a consult records
  provider/tokens/cost; each call site fills what it KNOWS and omits the rest.

    factory_log.py emit --event gate --repo acme/app --name ratchet --result pass --caught 0
    factory_log.py aggregate [--repo acme/app]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_LOG = Path.home() / ".chief-wiggum" / "factory-log.jsonl"
PRICING_PATH = Path(__file__).resolve().parent.parent / "config" / "model_pricing.json"

GATE = "gate"
CONSULT = "consult"
WORKER = "worker"
SKILL = "skill"
CLAUDE_CODE = "claude_code"  # per-request api_request events from Claude Code's own OTEL telemetry


def log_path() -> Path:
    env = os.environ.get("CW_FACTORY_LOG")
    return Path(env).expanduser() if env else DEFAULT_LOG


def telemetry_enabled() -> bool:
    return bool(os.environ.get("CW_TELEMETRY") or os.environ.get("CW_FACTORY_LOG"))


def _append(record: dict) -> bool:
    """Write one record to the log. Never raises."""
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
        return True
    except OSError:
        return False


def emit(event: str, *, ts: float | None = None, **fields) -> bool:
    """Append one telemetry record. No-op (returns False) unless telemetry is on
    (passive emission from live gates/consults). Never raises.
    """
    if not telemetry_enabled():
        return False
    record = {"ts": ts if ts is not None else time.time(), "event": event}
    record.update({k: v for k, v in fields.items() if v is not None})
    return _append(record)


def emit_gate(name: str, result: str, *, caught: int | None = None,
              duration_ms: float | None = None, repo: str | None = None,
              ticket: str | None = None) -> bool:
    return emit(GATE, name=name, result=result, caught=caught,
                duration_ms=duration_ms, repo=repo, ticket=ticket)


def load_pricing(path: Path = PRICING_PATH) -> dict:
    """Load the grounded per-model pricing table (config/model_pricing.json)."""
    try:
        return json.loads(path.read_text()).get("models", {})
    except (OSError, json.JSONDecodeError):
        return {}


def cost_for(model: str, tokens_in: int, tokens_out: int, pricing: dict | None = None) -> float | None:
    """USD cost of a call from the grounded pricing table, or None if unpriced.

    Returns None (not 0) when the model is unknown or its price is null — an
    un-priced consult records its tokens without a fabricated dollar figure.
    """
    table = pricing if pricing is not None else load_pricing()
    row = table.get(model)
    if not row:
        return None
    pin, pout = row.get("input_per_mtok"), row.get("output_per_mtok")
    if pin is None or pout is None:
        return None
    return round((tokens_in / 1_000_000) * pin + (tokens_out / 1_000_000) * pout, 6)


def emit_consult(provider: str, model: str | None, tokens_in: int | None = None,
                 tokens_out: int | None = None, *, repo: str | None = None,
                 ticket: str | None = None) -> bool:
    """Record an AI consultation, with token usage + grounded cost when known.

    When token counts are known, cost is computed from config/model_pricing.json
    (omitted when the model is unpriced — never logged as $0). When tokens are
    unknown (a CLI provider that didn't surface a usage summary), the event still
    records that a consult happened, for whom, in which repo — honest frequency
    telemetry without a fabricated token count.
    """
    cost = cost_for(model, tokens_in, tokens_out) if (model and tokens_in is not None and tokens_out is not None) else None
    return emit(CONSULT, provider=provider, name=model,
                tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost,
                repo=repo, ticket=ticket)


class gate_timer:
    """Context manager that times a gate and emits on exit.

        with gate_timer("check_patterns", repo=repo) as g:
            errors = run()
            g.caught = len(errors)
            g.result = "fail" if errors else "pass"
    """

    def __init__(self, name: str, *, repo: str | None = None, ticket: str | None = None):
        self.name, self.repo, self.ticket = name, repo, ticket
        self.result = "pass"
        self.caught: int | None = None
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.time()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.result = "error"
        emit_gate(self.name, self.result, caught=self.caught,
                  duration_ms=round((time.time() - self._t0) * 1000, 1),
                  repo=self.repo, ticket=self.ticket)
        return False  # never suppress


# ---- Claude Code OTEL ingestion (the end-to-end top layer) -------------------

def _cc_field(event: dict, *names):
    """Pull a field from a Claude Code OTEL record — flat key or nested attributes."""
    attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    body = event.get("body") if isinstance(event.get("body"), dict) else {}
    for n in names:
        for src in (event, attrs, body):
            if n in src and src[n] is not None:
                return src[n]
    return None


def ingest_claude_code(path: Path, repo: str | None = None) -> int:
    """Fold a Claude Code OTEL export (console-exporter stderr capture, or OTLP file)
    into the factory log so /reflect sees end-to-end orchestrator+subagent token cost
    alongside consult/gate telemetry.

    Parses per-request `api_request` events (model, input/output/cache tokens,
    cost_usd, query_source that separates repl_main_thread vs subagent). Tolerant of
    both flat-key and OTLP attributes shapes; skips anything that isn't an
    api_request. Explicit ingest — always writes (does not require CW_TELEMETRY).
    Returns the number of records ingested. See docs/factory-telemetry.md.
    """
    path = Path(path)
    if not path.is_file():
        return 0
    n = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = _cc_field(e, "event.name", "name", "event")
        if name != "api_request":
            continue
        rec = {"ts": _cc_field(e, "ts") or 0, "event": CLAUDE_CODE}
        for key, srcnames in (
            ("model", ("model",)),
            ("query_source", ("query_source",)),
            ("tokens_in", ("input_tokens",)),
            ("tokens_out", ("output_tokens",)),
            ("cache_read", ("cache_read_tokens",)),
            ("cache_creation", ("cache_creation_tokens",)),
            ("cost_usd", ("cost_usd",)),
            ("session_id", ("session.id", "session_id")),
            ("skill", ("skill.name", "agent.name", "skill", "agent")),
        ):
            v = _cc_field(e, *srcnames)
            if v is not None:
                rec[key] = v
        if repo:
            rec["repo"] = repo
        if _append(rec):
            n += 1
    return n


# ---- reading / aggregation ---------------------------------------------------

def read_log(path: Path | None = None) -> list[dict]:
    path = path or log_path()
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def aggregate(records: list[dict], repo: str | None = None) -> dict:
    if repo:
        records = [r for r in records if r.get("repo") == repo]
    gates: dict[str, dict] = {}
    consults: dict[str, dict] = {}
    # Claude Code's own token cost, split orchestrator (repl_main_thread) vs subagent,
    # and (when the OTEL events carry skill.name/agent.name) by loop/validation.
    claude_code: dict[str, dict] = {}
    by_loop: dict[str, dict] = {}
    consult_cost = cc_cost = 0.0
    for r in records:
        if r.get("event") == GATE and r.get("name"):
            g = gates.setdefault(r["name"], {"runs": 0, "passed": 0, "failed": 0,
                                             "caught": 0, "total_ms": 0.0})
            g["runs"] += 1
            g["passed"] += 1 if r.get("result") == "pass" else 0
            g["failed"] += 1 if r.get("result") in ("fail", "error") else 0
            g["caught"] += r.get("caught") or 0
            g["total_ms"] += r.get("duration_ms") or 0.0
        elif r.get("event") in (CONSULT, WORKER):
            key = r.get("provider") or r.get("name") or r.get("event")
            c = consults.setdefault(key, {"calls": 0, "tokens_in": 0,
                                          "tokens_out": 0, "cost_usd": 0.0})
            c["calls"] += 1
            c["tokens_in"] += r.get("tokens_in") or 0
            c["tokens_out"] += r.get("tokens_out") or 0
            c["cost_usd"] += r.get("cost_usd") or 0.0
            consult_cost += r.get("cost_usd") or 0.0
        elif r.get("event") == CLAUDE_CODE:
            src = r.get("query_source") or "unknown"
            cc = claude_code.setdefault(src, {"calls": 0, "tokens_in": 0,
                                              "tokens_out": 0, "cost_usd": 0.0})
            cc["calls"] += 1
            cc["tokens_in"] += r.get("tokens_in") or 0
            cc["tokens_out"] += r.get("tokens_out") or 0
            cc["cost_usd"] += r.get("cost_usd") or 0.0
            cc_cost += r.get("cost_usd") or 0.0
            if r.get("skill"):
                bl = by_loop.setdefault(r["skill"], {"calls": 0, "cost_usd": 0.0})
                bl["calls"] += 1
                bl["cost_usd"] += r.get("cost_usd") or 0.0
    # value/noise hint: a gate with runs but zero caught is a noise candidate
    for g in gates.values():
        g["value"] = "earning" if g["caught"] > 0 else ("noise-candidate" if g["runs"] >= 3 else "unproven")
    for cc in claude_code.values():
        cc["cost_usd"] = round(cc["cost_usd"], 6)
    for bl in by_loop.values():
        bl["cost_usd"] = round(bl["cost_usd"], 6)
    return {"gates": gates, "consults": consults, "claude_code": claude_code,
            "cost_by_loop": by_loop, "verdict": cost_value_verdict(gates, by_loop),
            "records": len(records),
            "consult_cost_usd": round(consult_cost, 4),
            "claude_code_cost_usd": round(cc_cost, 4),
            "cost_usd_total": round(consult_cost + cc_cost, 4)}


def cost_value_verdict(gates: dict, by_loop: dict) -> dict:
    """Join cost (per loop/validation) with value (findings caught) into a keep/demote
    verdict per validation — the "every loop is costed and its value quantified" view.

    A gate's value is its `caught` count; its cost is what its loop spent (LLM
    validations via cost_by_loop; deterministic gates are ~$0). cost_per_catch is the
    dollars spent per finding surfaced. The verdict:
      - earning         — caught > 0 (deterministic gates: free value; LLM loops: paid but productive)
      - demote-candidate — spent real $ over >=3 runs and caught nothing (noise you're paying for)
      - noise-candidate  — ran >=3 times, caught nothing, ~free (noisy but cheap)
      - unproven         — too few runs to judge
    """
    out: dict[str, dict] = {}
    for name in set(gates) | set(by_loop):
        g, loop = gates.get(name, {}), by_loop.get(name, {})
        cost = round(loop.get("cost_usd", 0.0), 6)
        caught = g.get("caught", 0)
        runs = g.get("runs", 0) or loop.get("calls", 0)
        if caught > 0:
            v = "earning"
        elif runs >= 3 and cost > 0:
            v = "demote-candidate"
        elif runs >= 3:
            v = "noise-candidate"
        else:
            v = "unproven"
        out[name] = {"cost_usd": cost, "caught": caught, "runs": runs,
                     "cost_per_catch": round(cost / caught, 6) if caught else None,
                     "verdict": v}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Factory telemetry emitter / aggregator.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("emit", help="Append a telemetry event")
    e.add_argument("--event", required=True, choices=[GATE, CONSULT, WORKER, SKILL])
    for opt in ("repo", "ticket", "name", "result", "provider", "details"):
        e.add_argument(f"--{opt}")
    for opt in ("caught", "tokens-in", "tokens-out"):
        e.add_argument(f"--{opt}", type=int)
    e.add_argument("--duration-ms", type=float)
    e.add_argument("--cost-usd", type=float)

    a = sub.add_parser("aggregate", help="Summarize the log")
    a.add_argument("--repo")
    a.add_argument("--format", choices=["text", "json"], default="json")

    ic = sub.add_parser("ingest-claude-code", help="Fold a Claude Code OTEL export into the log")
    ic.add_argument("otel_file", help="JSONL from `... 2>capture.jsonl` with the console OTEL exporter")
    ic.add_argument("--repo", help="Tag ingested records with this repo")

    sub.add_parser("path", help="Print the log path")
    args = parser.parse_args()

    if args.cmd == "path":
        print(log_path())
        return 0
    if args.cmd == "ingest-claude-code":
        n = ingest_claude_code(Path(args.otel_file), repo=args.repo)
        print(f"factory_log: ingested {n} api_request event(s) from {args.otel_file}")
        return 0
    if args.cmd == "emit":
        fields = {k: getattr(args, k) for k in
                  ("repo", "ticket", "name", "result", "provider", "details",
                   "caught", "cost_usd", "duration_ms")}
        fields["tokens_in"] = args.tokens_in
        fields["tokens_out"] = args.tokens_out
        ok = emit(args.event, **fields)
        if not ok:
            print("factory_log: telemetry disabled (set CW_TELEMETRY=1 to enable)", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "aggregate":
        agg = aggregate(read_log(), repo=args.repo)
        print(json.dumps(agg, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
