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
     provider?, tokens_in?, tokens_out?, cost_usd?, summary?, severity?,
     missed_by?, found_in?, invariant?, fixed?, seed_class?, details?}

  event: "gate" | "consult" | "worker" | "skill" | "escape" | "demotion"
  A gate records name/result/duration_ms/caught; a consult records
  provider/tokens/cost; an **escape** records a manually-found bug — especially
  one that slipped PAST a gate and was caught later (`missed_by` the gate/stage
  that should have caught it, `found_in` the review/verification step that
  actually caught it) — so `aggregate()` can compute gate RECALL
  (caught / (caught + escaped)), not just catches. Each call site fills what it
  KNOWS and omits the rest.

  A **demotion** (docs/gate-validation.md) fires when an escape's `--seed-class`
  matches a seed class the `missed_by` gate's validation record certified it
  catches: the validation was wrong about production recall, so the gate must
  drop back to report-only and the seed class gets re-derived — not just logged.

    factory_log.py emit --event gate --repo acme/app --name ratchet --result pass --caught 0
    factory_log.py bug --repo acme/app --summary "reset endpoint leaks account existence" \
      --severity high --missed-by ticket-gate --found-in close-epic-review
    factory_log.py bug --repo acme/app --summary "..." --severity high \
      --missed-by check_single_writer --seed-class evasion-omission \
      --found-in close-epic-review   # triggers a DEMOTION instruction if validated
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
ESCAPE = "escape"  # a manually-found bug, especially one a gate missed
DEMOTION = "demotion"  # a gate reverted to report-only after a validated seed class escaped
CLAUDE_CODE = "claude_code"  # per-request api_request events from Claude Code's own OTEL telemetry

ESCAPE_SEVERITIES = ("low", "medium", "high", "critical")
ESCAPE_FOUND_IN = ("implement-verify", "close-epic-review", "saas-gate", "manual", "prod")

# CW's own gates ship their validation records with chief-wiggum (see
# docs/gate-validation.md); default to that so demotion works out of the box.
DEFAULT_VALIDATION_DIR = str(Path(__file__).resolve().parent.parent / "docs" / "quality" / "validation")


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


def emit_escape(summary: str, *, severity: str, missed_by: str, found_in: str,
                repo: str | None = None, ticket: str | None = None,
                invariant: str | None = None, fixed: bool | None = None,
                seed_class: str | None = None) -> bool:
    """Record a manually-found bug — especially an ESCAPE that slipped PAST a gate
    and was only caught later (e.g. close-epic's adversarial review catching a bug
    the ticket's own gates missed).

    A `gate` event's `caught` count is only ever what THAT gate caught at THAT
    time — it has no way to see what it missed. `escape` is the other half: a
    human/agent records `missed_by` (the gate/stage that SHOULD have caught this,
    e.g. `ticket-gate`, `traceability`, `ratchet`, `close-epic-review`,
    `saas-gate`) and `found_in` (where it actually surfaced). `aggregate()` joins
    the two into gate RECALL — caught / (caught + escaped) — which `caught` alone
    can never show: a gate can report 100% catches on everything it looked at and
    still have terrible recall if real bugs keep slipping past it unnoticed.
    """
    return emit(ESCAPE, summary=summary, severity=severity, missed_by=missed_by,
                found_in=found_in, repo=repo, ticket=ticket, invariant=invariant,
                fixed=fixed, seed_class=seed_class)


def emit_demotion(gate: str, seed_class: str, *, repo: str | None = None,
                  ticket: str | None = None) -> bool:
    """Record that `gate` was demoted to report-only after a production escape
    matched a seed class its gate-validation record certified it catches
    (see `demotion_check` / docs/gate-validation.md)."""
    return emit(DEMOTION, name=gate, details=f"seed_class={seed_class}",
                repo=repo, ticket=ticket)


def load_validation_record(gate: str, validation_dir: str | Path) -> dict | None:
    """Load a gate-validation-protocol record (docs/gate-validation.md) for `gate`.
    Returns None (never raises) when absent or malformed — a missing record is
    not itself an error here; `check_gate_validation.py` is the authority on that."""
    path = Path(validation_dir) / f"{gate}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def demotion_check(missed_by: str, seed_class: str | None,
                   validation_dir: str | Path = DEFAULT_VALIDATION_DIR) -> dict | None:
    """Return a demotion instruction when a real escape's `seed_class` matches a
    class `missed_by`'s gate-validation record (docs/gate-validation.md)
    certified it PASSED — i.e. the record claimed this gate catches exactly this
    evasion technique, and production just proved that claim wrong.

    This is the mechanical half of "quality ratchets, never slides" applied to a
    gate's own blocking authority: a validated seed class that then escapes in
    production is not a one-off miss to log and forget, it is evidence the
    validation itself was insufficient. Returns None (nothing to demote) when no
    `seed_class` was given, no record exists, or the class wasn't one the record
    claims to have validated.

    Only classes certified as CAUGHT ground a demotion: the trial must have
    `expected: "fire"` with `result: "fired"` and `passed: true`. A passing
    `expected: "no-fire"` trial certifies a documented NON-coverage boundary
    (e.g. an evasion-sampling-gap seed proving vendor/ is out of scope) — an
    escape through that boundary is consistent with the record's authority
    statement, not a refutation of it, so it must not demote the gate.
    """
    if not seed_class:
        return None
    record = load_validation_record(missed_by, validation_dir)
    if not record:
        return None
    validated_classes = {
        t.get("seed_class") for t in record.get("seeded_defect_trials", []) or []
        if t.get("passed") is True and t.get("expected") == "fire" and t.get("result") == "fired"
    }
    if seed_class not in validated_classes:
        return None
    return {
        "gate": missed_by,
        "seed_class": seed_class,
        "instruction": (
            f"DEMOTE {missed_by} to report-only (drop --gate from its workflow wiring) — "
            f"a production escape matched seed class {seed_class!r}, which {missed_by}'s "
            f"gate-validation record ({validation_dir}/{missed_by}.json) certified it catches. "
            "File a tracking ticket to re-derive and re-run that seed class before "
            "re-promoting the gate to blocking."
        ),
    }


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
    escapes: dict[str, dict] = {}
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
        elif r.get("event") == ESCAPE:
            key = r.get("missed_by") or "unknown"
            es = escapes.setdefault(key, {"escaped": 0, "fixed": 0, "by_severity": {}})
            es["escaped"] += 1
            if r.get("fixed"):
                es["fixed"] += 1
            sev = r.get("severity") or "unknown"
            es["by_severity"][sev] = es["by_severity"].get(sev, 0) + 1
    # value/noise hint: a gate with runs but zero caught is a noise candidate
    for g in gates.values():
        g["value"] = "earning" if g["caught"] > 0 else ("noise-candidate" if g["runs"] >= 3 else "unproven")
    for cc in claude_code.values():
        cc["cost_usd"] = round(cc["cost_usd"], 6)
    for bl in by_loop.values():
        bl["cost_usd"] = round(bl["cost_usd"], 6)
    # recall = caught / (caught + escaped) — joins the gate's own catches (what it
    # saw) with escapes attributed to it (what it missed). None when we have
    # neither a catch count nor an escape count to reason from.
    for name, es in escapes.items():
        caught = gates.get(name, {}).get("caught", 0)
        escaped = es["escaped"]
        es["caught"] = caught
        es["recall"] = round(caught / (caught + escaped), 4) if (caught + escaped) > 0 else None
    return {"gates": gates, "consults": consults, "claude_code": claude_code,
            "cost_by_loop": by_loop, "verdict": cost_value_verdict(gates, by_loop),
            "escapes": escapes, "escapes_total": sum(es["escaped"] for es in escapes.values()),
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
    # Only validations get a verdict — a name must have emitted gate events. A loop
    # with cost but no gate events (e.g. `implement`, the build loop / orchestrator)
    # is build cost, not a validation, and belongs in cost_by_loop, not the verdict.
    out: dict[str, dict] = {}
    for name in gates:
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


_VERDICT_ORDER = {"demote-candidate": 0, "noise-candidate": 1, "unproven": 2, "earning": 3}


def render_report(agg: dict, repo: str | None = None) -> str:
    """Human-readable cost/value report from an aggregate() result."""
    L: list[str] = []
    scope = f" — {repo}" if repo else ""
    total, cc, cons = agg["cost_usd_total"], agg["claude_code_cost_usd"], agg["consult_cost_usd"]
    L.append(f"Factory cost/value report{scope}")
    L.append(f"  {agg['records']} telemetry events · end-to-end cost ${total}"
             f"  (Claude Code ${cc} + consults ${cons})")

    if agg.get("claude_code"):
        L.append("\n  Claude Code tokens by source:")
        for src, d in sorted(agg["claude_code"].items(), key=lambda kv: -kv[1]["cost_usd"]):
            L.append(f"    {src:<18} {d['calls']:>4} calls   ${d['cost_usd']}")
    if agg.get("cost_by_loop"):
        L.append("\n  Cost by loop:")
        for name, d in sorted(agg["cost_by_loop"].items(), key=lambda kv: -kv[1]["cost_usd"]):
            L.append(f"    {name:<20} ${d['cost_usd']}  ({d['calls']} calls)")

    verdict = agg.get("verdict") or {}
    if verdict:
        L.append("\n  Validation verdicts (worst first — demote-candidates need action):")
        L.append(f"    {'VALIDATION':<22}{'RUNS':>5}{'CAUGHT':>7}{'COST':>10}{'$/CATCH':>10}   VERDICT")
        L.append("    " + "─" * 70)
        rows = sorted(verdict.items(),
                      key=lambda kv: (_VERDICT_ORDER.get(kv[1]["verdict"], 9), -kv[1]["cost_usd"]))
        for name, v in rows:
            cost = f"${v['cost_usd']:.2f}"
            cpc = f"${v['cost_per_catch']:.3f}" if v["cost_per_catch"] is not None else "—"
            L.append(f"    {name:<22}{v['runs']:>5}{v['caught']:>7}{cost:>10}{cpc:>10}   {v['verdict']}")

    escapes = agg.get("escapes") or {}
    if escapes:
        L.append(f"\n  Escapes — bugs a gate missed ({agg.get('escapes_total', 0)} total). "
                  "Recall = caught / (caught + escaped):")
        L.append(f"    {'MISSED BY':<22}{'CAUGHT':>7}{'ESCAPED':>9}{'FIXED':>7}   RECALL")
        L.append("    " + "─" * 55)
        for name, e in sorted(escapes.items(), key=lambda kv: -kv[1]["escaped"]):
            recall = f"{e['recall']:.0%}" if e["recall"] is not None else "—"
            L.append(f"    {name:<22}{e['caught']:>7}{e['escaped']:>9}{e['fixed']:>7}   {recall}")
    return "\n".join(L)


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

    b = sub.add_parser("bug", help="Log a manually-found bug — especially an escape a gate missed")
    b.add_argument("--repo", required=True)
    b.add_argument("--summary", required=True, help="What the bug was")
    b.add_argument("--severity", required=True, choices=ESCAPE_SEVERITIES)
    b.add_argument("--missed-by", required=True,
                   help="The gate/stage that SHOULD have caught it, e.g. ticket-gate|traceability|ratchet|close-epic-review|saas-gate")
    b.add_argument("--found-in", required=True, choices=ESCAPE_FOUND_IN,
                   help="Where it was actually caught")
    b.add_argument("--ticket", help="Issue/ticket number, e.g. 42")
    b.add_argument("--invariant", help="Related invariant ID, e.g. INV-012")
    b.add_argument("--fixed", action="store_true", help="Set if already fixed at log time")
    b.add_argument("--seed-class",
                   help="Gate-validation seed class this escape resembles (docs/gate-validation.md), "
                        "e.g. evasion-omission. Triggers a DEMOTION instruction when --missed-by's "
                        "validation record certified it catches this class.")
    b.add_argument("--validation-dir", default=DEFAULT_VALIDATION_DIR,
                   help=f"Directory of <gate>.json validation records (default: {DEFAULT_VALIDATION_DIR})")

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
    if args.cmd == "bug":
        # Demotion is a structural check against the gate's validation record —
        # independent of whether telemetry logging is enabled. It must not be
        # silenced just because CW_TELEMETRY is off.
        demotion = demotion_check(args.missed_by, args.seed_class, args.validation_dir)
        if demotion:
            print(f"factory_log: DEMOTION — {demotion['instruction']}", file=sys.stderr)
        ok = emit_escape(args.summary, severity=args.severity, missed_by=args.missed_by,
                          found_in=args.found_in, repo=args.repo, ticket=args.ticket,
                          invariant=args.invariant, fixed=True if args.fixed else None,
                          seed_class=args.seed_class)
        if not ok:
            print("factory_log: telemetry disabled (set CW_TELEMETRY=1 to enable)", file=sys.stderr)
            return 1
        if demotion:
            emit_demotion(demotion["gate"], demotion["seed_class"], repo=args.repo, ticket=args.ticket)
        return 0
    if args.cmd == "aggregate":
        agg = aggregate(read_log(), repo=args.repo)
        print(render_report(agg, repo=args.repo) if args.format == "text"
              else json.dumps(agg, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
