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
     provider?, adapter?, requested_model?, usage_status?, pricing_version?,
     tokens_in?, tokens_out?, cost_usd?, summary?, severity?,
     missed_by?, found_in?, invariant?, fixed?, seed_class?, details?,
     previous_authority?, verb?, path?, hit_count?}

  event: "gate" | "consult" | "worker" | "skill" | "escape" | "demotion" | "query"
  A consult record's `adapter`/`usage_status`/`pricing_version` are chief-wiggum#134
  additions — pre-#134 records simply lack them (readers must tolerate their
  absence, not assume a value). `adapter` names which parser produced the usage
  (codex-cli|gemini-cli|vertex-sdk|claude-cli|claude-interactive); `usage_status`
  is provider-json|sdk-metadata|partial|unavailable (never silent, INV-fh-011);
  `requested_model` is the --model override/provider default, distinct from
  `name` (the RESOLVED billed model id — never a bare CLI alias, CTR-fh-013).
  A gate records name/result/duration_ms/caught; a consult records
  provider/tokens/cost; an **escape** records a manually-found bug — especially
  one that slipped PAST a gate and was caught later (`missed_by` the gate/stage
  that should have caught it, `found_in` the review/verification step that
  actually caught it) — so `aggregate()` can compute gate RECALL
  (caught / (caught + escaped)), not just catches. A **query** records one
  `code_query.py` call — `verb` (orient/governs/writers/...), `hit_count` (facts
  found before the response cap), `path` (the queried path/field/ID, when it's
  short/stable enough to be useful) — so `aggregate()` can show which structural
  questions agents actually ask (#159), not just that the tool exists. Each call
  site fills what it KNOWS and omits the rest.

  A **demotion** (docs/gate-validation.md) fires when an escape's `--seed-class`
  matches a seed class the `missed_by` gate's validation record certified it
  catches: the validation was wrong about production recall, so the gate must
  drop back to report-only and the seed class gets re-derived — not just logged.
  A demotion can ALSO fire without any escape at all: chief-wiggum#198/IT-fh-06's
  stale-while-blocking auto-demotion (state-machines.json's Gate
  Blocking-Authority Lifecycle, G-008/G-014) — `check_gate_validation.py`
  detects a BLOCKING gate's record went stale (scanner_version/journal drift) or
  missing/invalid, and demotes it. That path has no `seed_class` (nothing
  escaped in production; the record itself just rotted), so it calls the
  GENERIC `emit_stale_demotion` (`details='stale'|'record_missing'`,
  `previous_authority` recorded), never `emit_demotion` (which requires one).

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
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.hashing import stable_hash  # noqa: E402

DEFAULT_LOG = Path.home() / ".chief-wiggum" / "factory-log.jsonl"
PRICING_PATH = Path(__file__).resolve().parent.parent / "config" / "model_pricing.json"

# ConsultUsageRecord.usage_status (chief-wiggum#134 / ADR-fh-05). Pre-#134
# records simply lack this field — readers must tolerate its absence rather
# than assume a value.
CONSULT_USAGE_STATUSES = ("provider-json", "sdk-metadata", "partial", "unavailable")

# A resolved billed model id must never be the bare CLI/tool name a consult
# was invoked with — that's indistinguishable from an unpriced model and
# silently nulls cost (CTR-fh-013). Enforced here, the single write path for
# ConsultUsageRecord (INV-fh-002).
_BARE_CLI_ALIASES = frozenset({"codex", "gemini", "gemini-vertex", "claude", "claude-interactive"})

GATE = "gate"
CONSULT = "consult"
WORKER = "worker"
SKILL = "skill"
ESCAPE = "escape"  # a manually-found bug, especially one a gate missed
DEMOTION = "demotion"  # a gate reverted to report-only after a validated seed class escaped
QUERY = "query"  # one code_query.py verb call (#159)
PHASE = "phase"  # wall-clock for one workflow phase (#375 proposal 6)
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


def emit_stale_demotion(gate: str, reason: str, *, previous_authority: str | None = None,
                       repo: str | None = None, ticket: str | None = None) -> bool:
    """Record that `gate` auto-demoted because its gate-validation record went
    stale or missing/invalid WHILE BLOCKING (state-machines.json's Gate
    Blocking-Authority Lifecycle, G-008/G-014 — chief-wiggum#198/IT-fh-06).

    This is deliberately the GENERIC `DEMOTION` event, not `emit_demotion`:
    `emit_demotion` requires a `seed_class` (an escape-driven demotion always
    has one — production proved a *validated* seed wrong); a staleness or
    missing-record demotion has no escape and no seed_class at all, only a
    `reason` ('stale' | 'record_missing') and the `previous_authority` the
    gate is coming down from — recorded so a later re-derived/re-journaled
    record can be told it is being *restored*, not freshly promoted.
    @cw-trace guards INV-fh-003"""
    assert reason in ("stale", "record_missing"), f"unknown stale-demotion reason: {reason!r}"
    return emit(DEMOTION, name=gate, details=reason, previous_authority=previous_authority,
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


def emit_query(verb: str, *, repo: str | None = None, path: str | None = None,
               hit_count: int | None = None) -> bool:
    """Record one ``code_query.py`` verb call — usage telemetry, not a gate.

    The point isn't pass/fail; it's learning which structural questions agents
    actually ask (#159) — `aggregate()` tallies calls per verb from these events.
    """
    return emit(QUERY, verb=verb, repo=repo, path=path, hit_count=hit_count)


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


def load_cache_multipliers() -> dict:
    """Prompt-cache price multipliers from the grounded pricing table.

    Empty dict when absent — callers then price cache tokens at the plain input
    rate rather than inventing a discount.
    """
    try:
        block = json.loads(PRICING_PATH.read_text()).get("cache_multipliers", {})
    except (OSError, json.JSONDecodeError):
        return {}
    return block if isinstance(block, dict) else {}


def cost_for_usage(model: str, tokens_in: int, tokens_out: int, *,
                   cache_read: int = 0, cache_write_5m: int = 0, cache_write_1h: int = 0,
                   pricing: dict | None = None, multipliers: dict | None = None) -> float | None:
    """USD cost of a call whose usage separates cached from uncached input.

    ``cost_for`` prices two buckets (in/out) because that is all a consult
    provider reports. A Claude Code turn reports four: fresh input, cache reads
    (~0.1x input), and cache writes at two TTLs (1.25x at 5m, 2x at 1h). Pricing
    cached tokens at the full input rate would badly overstate an agent session,
    where cache reads routinely dwarf fresh input — so they get their own
    multipliers, read from config/model_pricing.json rather than hardcoded.

    Returns None (not 0) for an unpriced model, exactly like ``cost_for``: an
    unpriced call records its tokens without a fabricated dollar figure
    (INV-fh-002 — cost is derived here or not at all).
    """
    table = pricing if pricing is not None else load_pricing()
    row = table.get(model)
    if not row:
        return None
    pin, pout = row.get("input_per_mtok"), row.get("output_per_mtok")
    if pin is None or pout is None:
        return None
    m = multipliers if multipliers is not None else load_cache_multipliers()
    # An absent multiplier means "price it like fresh input" (1.0) — never a
    # silent discount we can't point at a vendor page for.
    m_read = m.get("cache_read", 1.0)
    m_w5 = m.get("cache_write_5m", 1.0)
    m_w1 = m.get("cache_write_1h", 1.0)
    billed_in = (tokens_in
                 + cache_read * m_read
                 + cache_write_5m * m_w5
                 + cache_write_1h * m_w1)
    return round((billed_in / 1_000_000) * pin + (tokens_out / 1_000_000) * pout, 6)


def _coerce_token(value) -> int | None:
    """Coerce an untrusted token count to ``int``; anything unusable (bool, junk
    string, list, non-integral float, ...) is ``None`` — a malformed count must
    degrade the record's usage, never crash the emit (which would vanish the
    whole telemetry event inside a best-effort caller)."""
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _pricing_version(path: Path = PRICING_PATH) -> str | None:
    """Hash of config/model_pricing.json's raw text (chief_wiggum.hashing.stable_hash),
    recorded on each consult so a historical run can be re-priced later by replaying
    cost_for over its recorded tokens against whichever pricing table version was
    live at record time. None (never raises) when the file is unreadable."""
    try:
        return stable_hash(path.read_text())
    except OSError:
        return None


def emit_consult(provider: str, model: str | None, tokens_in: int | None = None,
                 tokens_out: int | None = None, *, usage_status: str | None = None,
                 adapter: str | None = None, requested_model: str | None = None,
                 repo: str | None = None, ticket: str | None = None) -> bool:
    """Record an AI consultation, with token usage + grounded cost when known
    (ConsultUsageRecord, chief-wiggum#134).

    ``usage_status`` names the TRUE source of the usage data (``provider-json``
    | ``sdk-metadata`` | ``partial`` | ``unavailable``) and is never silently
    implied (INV-fh-011); an unrecognised value is dropped to unset rather than
    trusted. Both-tokens-or-null: a one-sided OR malformed token count (only
    one of tokens_in/tokens_out usable as an int) is recorded as NO usage
    rather than half-priced — both are nulled, and a status that claimed a
    real source downgrades to 'partial'. Malformed usage degrades the EVENT,
    it never vanishes it: token values are coerced at this boundary
    (``_coerce_token``) and cost derivation is exception-proof, so a drifted
    payload can't raise inside a best-effort caller and silently drop the
    whole record. ``model`` (the resolved billed model id) must never be a
    bare CLI/tool alias (CTR-fh-013) — that's a caller bug, not a
    degraded-usage case, so it raises rather than silently recording a wrong
    id.

    cost_usd is computed ONLY here, from config/model_pricing.json and the two
    (both-or-null) recorded token counts (INV-fh-002) — never author-supplied,
    never a fabricated 0; omitted (null) when the model is unpriced or tokens
    are unknown. ``pricing_version`` lets a historical record be re-priced by
    replaying cost_for against whichever pricing table was live at emit time.

    @cw-trace ensures CTR-fh-013 CTR-fh-014 CTR-fh-015 INV-fh-002 INV-fh-011
    """
    if model in _BARE_CLI_ALIASES:
        raise ValueError(
            f"emit_consult: resolved model {model!r} is a bare CLI alias, not a "
            "billed model id (CTR-fh-013) — the caller failed to resolve it."
        )
    if usage_status is not None and usage_status not in CONSULT_USAGE_STATUSES:
        usage_status = None
    coerced_in, coerced_out = _coerce_token(tokens_in), _coerce_token(tokens_out)
    malformed = (tokens_in is not None and coerced_in is None) or (
        tokens_out is not None and coerced_out is None
    )
    tokens_in, tokens_out = coerced_in, coerced_out
    if malformed or (tokens_in is None) != (tokens_out is None):
        # One-sided or malformed payload: both-tokens-or-null (INV-fh-011) —
        # never a half-priced record, never a crashed emit.
        tokens_in = tokens_out = None
        if usage_status in ("provider-json", "sdk-metadata"):
            usage_status = "partial"
    try:
        cost = cost_for(model, tokens_in, tokens_out) if (model and tokens_in is not None and tokens_out is not None) else None
    except Exception:
        # A broken pricing row must degrade cost to null, not vanish the event.
        cost = None
    return emit(CONSULT, provider=provider, adapter=adapter, requested_model=requested_model,
                name=model, usage_status=usage_status, tokens_in=tokens_in, tokens_out=tokens_out,
                cost_usd=cost, pricing_version=_pricing_version(), repo=repo, ticket=ticket)


def emit_phase(name: str, *, duration_ms: float, repo: str | None = None,
               ticket: str | None = None, outcome: str = "ok", **fields) -> bool:
    """Record how long one workflow phase took (chief-wiggum#375).

    ticket_cost tracks tokens but not time-in-phase, so loop latency was felt
    rather than measured and there was no data to rank the fixes against. A
    phase that raised is recorded too, with outcome="error": a phase that blew
    up fast would otherwise look like a phase that went well.
    """
    return emit("phase", phase=name, duration_ms=round(duration_ms, 1),
                outcome=outcome, repo=repo, ticket=ticket, **fields)


class phase_timer:
    """Time a workflow phase and emit on exit.

        with phase_timer("step4a_consults", ticket="#42") as p:
            run_consults()
            p.detail = f"{len(providers)} providers"

    Mirrors gate_timer deliberately: one timing shape in the ledger rather than
    two, so phase and gate latency can be read together.
    """

    def __init__(self, name: str, *, repo: str | None = None, ticket: str | None = None):
        self.name, self.repo, self.ticket = name, repo, ticket
        self.outcome = "ok"
        self.detail: str | None = None
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.time()
        return self

    @property
    def elapsed_ms(self) -> float:
        return (time.time() - self._t0) * 1000

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.outcome = "error"
        emit_phase(self.name, duration_ms=self.elapsed_ms, repo=self.repo,
                   ticket=self.ticket, outcome=self.outcome, detail=self.detail)
        return False  # never suppress


def phase_summary(records: list[dict]) -> dict:
    """Aggregate phase events into per-phase wall-clock.

    Returns total and count per phase plus the overall total, so the slowest
    phase is a number rather than an impression.
    """
    phases: dict[str, dict] = {}
    for record in records:
        if record.get("event") != "phase":
            continue
        name = str(record.get("phase", "unknown"))
        bucket = phases.setdefault(
            name, {"phase": name, "total_ms": 0.0, "runs": 0, "errors": 0}
        )
        bucket["total_ms"] += float(record.get("duration_ms") or 0.0)
        bucket["runs"] += 1
        if record.get("outcome") == "error":
            bucket["errors"] += 1
    ordered = sorted(phases.values(), key=lambda item: (-item["total_ms"], item["phase"]))
    for bucket in ordered:
        bucket["total_ms"] = round(bucket["total_ms"], 1)
        bucket["mean_ms"] = round(bucket["total_ms"] / bucket["runs"], 1) if bucket["runs"] else 0.0
    return {
        "phases": ordered,
        "total_ms": round(sum(item["total_ms"] for item in ordered), 1),
        "slowest": ordered[0]["phase"] if ordered else None,
    }


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


DEFAULT_TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"


def _repo_from_cwd(cwd: str | None) -> str | None:
    """Best-effort repo name for a transcript record's working directory.

    A worktree cwd (``<repo>/.claude/worktrees/<branch>``) belongs to its parent
    repo, not to a repo named after the branch — otherwise every worktree looks
    like a separate project and per-repo aggregation fragments.
    """
    if not cwd:
        return None
    marker = "/.claude/worktrees/"
    if marker in cwd:
        cwd = cwd.split(marker, 1)[0]
    name = Path(cwd).name
    return name or None


def cwd_matches_prefix(cwd: str | None, prefix: str | None) -> bool:
    """Exact match or true PATH CHILD of ``prefix`` — never a lexical prefix
    match. ``str.startswith`` alone would match ``.../t420`` against a prefix
    of ``.../t42``: a sibling worktree, not a child of it, and exactly the
    cross-billing chief-wiggum#345's review caught (a bare startswith let a
    sibling worktree's spend tag/count/slice onto this ticket). Shared by the
    three cwd_prefix call sites: ``ingest_claude_transcripts``,
    ``count_transcript_turns``, and ``ticket_cost._in_window``.
    """
    if not cwd or not prefix:
        return False
    prefix = str(prefix).rstrip("/")
    cwd = str(cwd)
    return cwd == prefix or cwd.startswith(prefix + "/")


def _iter_transcript_files(root: Path):
    """Every Claude Code transcript under ``root``.

    Claude Code writes the orchestrator's turns to ``<project>/<session>.jsonl``
    but every SUB-AGENT's turns to a deeper path —
    ``<project>/<session>/subagents/agent-<id>.jsonl``, and workflow sub-agents
    deeper still (``.../subagents/workflows/<wf>/agent-<id>.jsonl``). A
    ``*/*.jsonl`` glob sees only the first level, which is why the sub-agent
    layer read $0 while carrying ~75% of the turn volume (chief-wiggum#345).
    Recursing is safe: the record filter below (``type == "assistant"`` plus a
    ``message.usage`` object) is what decides what counts, not the path shape,
    and request ids do not repeat across levels.
    """
    if not root.is_dir():
        return
    yield from sorted(root.rglob("*.jsonl"))


def ingested_request_ids() -> set[str]:
    """Request IDs already present as claude_code records in the log.

    The ingest is re-run as sessions accumulate, and transcripts are append-only
    files that keep their old turns — without this, every re-run would re-add
    every previously-ingested turn and inflate cost without bound. Keyed on the
    API request ID, which is unique per turn and stable across re-reads.
    """
    seen: set[str] = set()
    path = log_path()
    if not path.is_file():
        return seen
    try:
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("event") == CLAUDE_CODE and r.get("request_id"):
                    seen.add(r["request_id"])
    except OSError:
        return seen
    return seen


def ingest_claude_transcripts(root: Path | None = None, repo: str | None = None,
                              since: float | None = None, ticket: str | None = None,
                              cwd_prefix: str | None = None,
                              until: float | None = None) -> int:
    """Fold Claude Code's own per-turn token usage into the factory log.

    This is the end-to-end cost half of the ledger. `factory_log` already records
    what CW *runs* (gates) and what it *consults* (codex/gemini/...), but the
    orchestrator plus every sub-agent it spawns is usually the largest line item,
    and nothing was recording it: `aggregate`'s `claude_code_cost_usd` read
    $0.00 not because the sessions were free but because no ingest had ever run.

    Claude Code already writes everything needed, per assistant turn, to
    ``~/.claude/projects/<project>/<session>.jsonl``:

      * ``message.model`` + ``message.usage`` — input/output and, separately,
        cache read and cache-creation tokens at each TTL (priced via
        ``cost_for_usage``, so a cache-heavy agent session isn't billed as if
        every cached token were fresh input)
      * ``isSidechain`` — the orchestrator (``repl_main_thread``) vs sub-agent
        (``subagent``) split, which is what makes "what did delegation cost"
        answerable
      * ``requestId`` — a stable per-turn key, so the ingest is idempotent and
        can be re-run as sessions accumulate
      * ``cwd`` — repo attribution, worktrees folded back to their parent repo

    Reading these needs no configuration and works **retroactively** over
    sessions that have already happened — unlike the OTEL console-exporter route
    (``ingest_claude_code``), which only captures a run you remembered to wrap
    beforehand and doesn't fit an interactive TUI session. That one stays for
    OTEL pipelines; this is the path that works by default.

    Explicit ingest — always writes (does not require CW_TELEMETRY). Returns the
    number of NEW records written. See docs/factory-telemetry.md.

    ``ticket`` tags matching turns for per-ticket cost slicing
    (``scripts/ticket_cost.py``). Transcripts carry no ticket number, so the tag
    is applied by attribution guard, never blindly: a turn is tagged only when
    its ``cwd`` sits under ``cwd_prefix`` (worktree-precise — what
    ``/implement-wave`` workers should pass) or, absent a prefix, when the
    turn's cwd-derived repo matches ``repo`` (right for a solo ``/implement``
    session windowed by ``since``; a concurrent session on the SAME repo in the
    same window would bleed in — pass ``cwd_prefix`` when that matters).
    Dedup is by request id, so already-ingested turns cannot be re-tagged by a
    later run: tag at first ingest.

    ``until`` bounds a CATCH-UP ingest so it can never consume an in-flight
    ticket's turns. Dedup is by request id and tagging happens at first
    ingest, so an unbounded catch-up run during a build would permanently
    strand that build's turns untagged.
    """
    root = Path(root) if root is not None else DEFAULT_TRANSCRIPT_ROOT
    if ticket is not None and repo is None and cwd_prefix is None:
        raise ValueError("ingest ticket tagging needs an attribution guard: pass repo and/or cwd_prefix")
    pricing, multipliers = load_pricing(), load_cache_multipliers()
    seen = ingested_request_ids()
    n = 0
    for f in _iter_transcript_files(root):
        try:
            lines = f.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") != "assistant":
                continue
            msg = e.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            req = e.get("requestId") or e.get("uuid")
            if not req or req in seen:
                continue

            ts = _parse_iso_ts(e.get("timestamp"))
            if since is not None and ts is not None and ts < since:
                continue
            if until is not None and ts is not None and ts > until:
                continue

            tin = _coerce_token(usage.get("input_tokens")) or 0
            tout = _coerce_token(usage.get("output_tokens")) or 0
            cache_read = _coerce_token(usage.get("cache_read_input_tokens")) or 0
            creation = usage.get("cache_creation")
            creation = creation if isinstance(creation, dict) else {}
            w5 = _coerce_token(creation.get("ephemeral_5m_input_tokens"))
            w1 = _coerce_token(creation.get("ephemeral_1h_input_tokens"))
            if w5 is None and w1 is None:
                # Older/flatter shape: one undifferentiated cache-creation count.
                # Price it at the 5m rate — the default TTL, and the cheaper of
                # the two, so an unknown TTL can't silently overstate cost.
                w5 = _coerce_token(usage.get("cache_creation_input_tokens")) or 0
                w1 = 0
            w5, w1 = w5 or 0, w1 or 0

            model = msg.get("model")
            # `<synthetic>` turns are harness-generated (e.g. cancellations) and
            # were never billed — they carry no real model to price.
            if not model or model == "<synthetic>":
                continue

            # An all-zero usage line is not a billable turn. It matters because a
            # request's usage is repeated on EVERY content-block line it produced
            # (thinking, text, tool_use), and dedup keeps the first line seen — so
            # a stray zero line arriving first would shadow the real usage and
            # undercount the request. Skipping zeros makes the dedup
            # order-independent instead of relying on file ordering.
            if (tin + tout + cache_read + w5 + w1) == 0:
                continue

            rec = {
                "ts": ts or 0,
                "event": CLAUDE_CODE,
                "request_id": req,
                "model": model,
                # Belt-and-braces: `isSidechain` is the primary signal, but a
                # sub-agent transcript living under a `/subagents/` path
                # counts too, even on an older record shape that omits the
                # flag (chief-wiggum#345).
                "query_source": ("subagent" if (e.get("isSidechain") or "/subagents/" in str(f))
                                 else "repl_main_thread"),
                "tokens_in": tin,
                "tokens_out": tout,
                "cache_read": cache_read,
                "cache_creation": w5 + w1,
                # Split by TTL as well as totalled: a 5m write bills 1.25x and a
                # 1h write 2x, so the mix is the difference between two very
                # different bills for identical work (session_cost_report).
                "cache_write_5m": w5,
                "cache_write_1h": w1,
                "source": "transcript",
            }
            if e.get("sessionId"):
                rec["session_id"] = e["sessionId"]
            cwd = e.get("cwd")
            if cwd:
                # Recorded so a turn can be attributed to a ticket at READ time
                # (ticket_cost's cwd+window slice) as well as by the tag applied
                # here — dedup is by request id, so a turn ingested untagged by a
                # catch-up run could otherwise never be attributed (#345).
                rec["cwd"] = str(cwd)
            agent = e.get("attributionAgent")
            if agent:
                # The sub-agent TYPE (e.g. general-purpose, Explore) — a type
                # name, never prompt content. Deliberately NOT stored in `skill`:
                # aggregate()'s cost_by_loop join is keyed by gate name and would
                # be polluted by agent-type keys.
                rec["agent_type"] = str(agent)
            derived = _repo_from_cwd(cwd)
            attributed = repo or derived
            if attributed:
                rec["repo"] = attributed
            if ticket is not None:
                # Attribution guard, never a blind stamp: worktree-precise when a
                # cwd_prefix is given, else cwd-derived-repo match. `repo` may be
                # owner/repo while the cwd only yields the basename.
                if cwd_prefix is not None:
                    tag = cwd_matches_prefix(cwd, cwd_prefix)
                else:
                    tag = bool(derived) and derived in (repo, str(repo).split("/")[-1])
                if tag:
                    rec["ticket"] = str(ticket)
            cost = cost_for_usage(model, tin, tout, cache_read=cache_read,
                                  cache_write_5m=w5, cache_write_1h=w1,
                                  pricing=pricing, multipliers=multipliers)
            if cost is not None:
                rec["cost_usd"] = cost
            if _append(rec):
                seen.add(req)
                n += 1
    return n


def count_transcript_turns(root: Path | None = None, *, since: float | None = None,
                          until: float | None = None, repo: str | None = None,
                          cwd_prefix: str | None = None) -> dict:
    """Count billable assistant turns visible in the transcript corpus, WITHOUT
    ingesting them — the independent evidence behind ticket_cost's coverage line.

    "Zero records in the ledger" cannot by itself distinguish *no work happened*
    from *nothing was captured*; this answers the second half from a source other
    than the ledger. Returns
    ``{"scanned": bool, "repl_main_thread": int, "subagent": int}``.
    ``scanned: False`` means the corpus was not readable (no transcript root — a
    non-Claude harness, or a fresh machine): the caller must report ``unknown``,
    never ``captured`` and never ``$0``.

    Bounded on purpose: with ``since`` set, whole FILES whose mtime precedes the
    window are skipped without being read (a cheap pre-filter, sound for
    append-only files — a stale mtime means every line predates it too), so
    this stays cheap enough to run at PR time. The actual window membership is
    still enforced per TURN once a file is read (symmetric with
    ``ingest_claude_transcripts``'s own since/until checks), and a turn whose
    timestamp can't be parsed is excluded whenever a window bound is active —
    count only what can be confirmed to fall inside it.
    """
    root = Path(root) if root is not None else DEFAULT_TRANSCRIPT_ROOT
    if not root.is_dir():
        return {"scanned": False, "repl_main_thread": 0, "subagent": 0}

    counts = {"repl_main_thread": 0, "subagent": 0}
    for f in _iter_transcript_files(root):
        if since is not None:
            try:
                if f.stat().st_mtime < since:
                    continue
            except OSError:
                continue
        try:
            lines = f.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") != "assistant":
                continue
            msg = e.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue

            model = msg.get("model")
            if not model or model == "<synthetic>":
                continue

            tin = _coerce_token(usage.get("input_tokens")) or 0
            tout = _coerce_token(usage.get("output_tokens")) or 0
            cache_read = _coerce_token(usage.get("cache_read_input_tokens")) or 0
            creation = usage.get("cache_creation")
            creation = creation if isinstance(creation, dict) else {}
            w5 = _coerce_token(creation.get("ephemeral_5m_input_tokens"))
            w1 = _coerce_token(creation.get("ephemeral_1h_input_tokens"))
            if w5 is None and w1 is None:
                w5 = _coerce_token(usage.get("cache_creation_input_tokens")) or 0
                w1 = 0
            w5, w1 = w5 or 0, w1 or 0
            if (tin + tout + cache_read + w5 + w1) == 0:
                continue

            cwd = e.get("cwd")
            if cwd_prefix is not None:
                if not cwd_matches_prefix(cwd, cwd_prefix):
                    continue
            elif repo is not None:
                derived = _repo_from_cwd(cwd)
                if not (derived and derived in (repo, str(repo).split("/")[-1])):
                    continue

            # AC4 promises an IN-WINDOW denominator, so since/until are
            # enforced per-turn here too (symmetric with the ingest's own
            # since/until checks) — the mtime check above is only a cheap
            # skip-whole-file pre-filter, not a substitute for this. A turn
            # whose timestamp can't be parsed can't be placed in the window,
            # so once a bound is active it is excluded, not counted on faith
            # (chief-wiggum#345 review: count only what you can confirm).
            ts = _parse_iso_ts(e.get("timestamp"))
            if since is not None or until is not None:
                if ts is None:
                    continue
                if since is not None and ts < since:
                    continue
                if until is not None and ts > until:
                    continue

            bucket = "subagent" if (e.get("isSidechain") or "/subagents/" in str(f)) else "repl_main_thread"
            counts[bucket] += 1

    return {"scanned": True, **counts}


def _parse_iso_ts(value) -> float | None:
    """ISO-8601 timestamp -> epoch seconds; None if unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class IngestCount(int):
    """How many records were ingested, carrying how many duplicates were skipped.

    An int subclass rather than a tuple so existing callers and their
    assertions keep working unchanged, while the CLI can still report the
    skips. Reporting them is the point: silently dropping records is the
    failure this log is supposed to make impossible to commit accidentally.
    """

    skipped: int

    def __new__(cls, ingested: int, skipped: int = 0):
        obj = super().__new__(cls, ingested)
        obj.skipped = skipped
        return obj


def _ingested_claude_keys() -> tuple[set[str], set[str]]:
    """Identities already on claude_code records: (request ids, contentless hashes).

    Two sets rather than one union, because the two identities are not
    interchangeable and treating them as such breaks in both directions.

    Matching only on id misses the crossover: a record written from an id-less
    console capture is keyed by content, so a later OTLP capture of the same
    run — which does carry `request.id` — finds nothing and appends a
    duplicate. That is the inflation direction.

    Matching on the union over-corrects: two genuinely distinct requests whose
    every extracted field is identical except the request id share a content
    hash, so the second is dropped. That is the under-count direction, and the
    suite has exactly that case.

    So: a content hash is matchable only for records the exporter gave no id
    for. `ingested_request_ids()` is left alone — the transcript route depends
    on its current meaning.
    """
    ids = ingested_request_ids()
    contentless: set[str] = set()
    path = log_path()
    if not path.is_file():
        return ids, contentless
    try:
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("event") != CLAUDE_CODE:
                    continue
                content = r.get("content_key")
                # `request_id == content_key` marks a record the exporter gave
                # no id for. Only those may be matched BY content: a record
                # that had a real id keeps its identity, so a later capture
                # carrying a different id but identical content is a genuinely
                # different request, not a repeat.
                if content and r.get("request_id") == content:
                    contentless.add(content)
    except OSError:
        return ids, contentless
    return ids, contentless


def _otel_request_keys(event: dict, rec: dict) -> tuple[str, str | None]:
    """Every identity one `api_request` can be known by: (content_key, id_key).

    BOTH are returned and dedup matches on either, because choosing one
    silently double-counts across capture shapes. This function serves the
    console-exporter capture AND the OTLP file, and they do not agree on
    whether a request id is emitted. Ingest a run's console capture (keyed by
    content hash), then an OTLP export of the same run (keyed by
    `request.id`), and the second key misses the first, so every event lands
    twice. That is the inflation direction, the one a cost ledger most needs
    to avoid, and an exporter upgrade produces exactly this mix.

    The content key hashes EVERY parsed field of the turn, not just session,
    timestamp, model and tokens. Narrowing it to those collapses records that
    differ elsewhere: the existing suite has two `code-review` requests
    distinguished only by `cost_usd`, and a key blind to cost silently dropped
    the second one and under-reported the loop. Anything the parser bothered
    to extract is identity for this purpose.

    `repo` and `ticket` are excluded deliberately — they are supplied by the
    caller rather than read from the event, so including them would let the
    same capture ingested under two tickets count twice.

    Residual limits, stated rather than hidden:

    - Two requests identical in every extracted field are indistinguishable
      and collapse into one. That direction is the safe one for a ledger,
      since it under-counts rather than inflating.
    - An exporter emitting a non-unique id (a placeholder, or a per-session
      counter that repeats across sessions) collapses every event sharing it.
      Nothing here can tell that apart from a genuine repeat.
    - Dedup is only as durable as the log. Rotate, truncate or break the
      permissions on `$CW_FACTORY_LOG` and the baseline is gone, so a
      re-ingest of an archived capture appends everything again and honestly
      reports `skipped 0`. Records written before this change carry no keys
      at all and cannot participate.
    """
    id_key = None
    for name in ("request.id", "request_id", "requestId", "api_request.id"):
        value = _cc_field(event, name)
        # A stripped-string check, not truthiness: an id of `0` is falsy but
        # perfectly valid, and would otherwise fall to the content path while
        # its siblings used the id path — two identity bases in one capture.
        if value is not None and str(value).strip():
            id_key = str(value).strip()
            break

    material = "|".join(
        f"{field}={rec[field]!r}"
        for field in sorted(rec)
        if field not in ("repo", "ticket", "request_id", "content_key")
    )
    content_key = "otel-sha256:" + hashlib.sha256(
        material.encode("utf-8")).hexdigest()
    return content_key, id_key


def ingest_claude_code(path: Path, repo: str | None = None,
                       ticket: str | None = None) -> IngestCount:
    """Fold a Claude Code OTEL export (console-exporter stderr capture, or OTLP file)
    into the factory log so /reflect sees end-to-end orchestrator+subagent token cost
    alongside consult/gate telemetry.

    Parses per-request `api_request` events (model, input/output/cache tokens,
    cost_usd, query_source that separates repl_main_thread vs subagent). Tolerant of
    both flat-key and OTLP attributes shapes; skips anything that isn't an
    api_request. Explicit ingest — always writes (does not require CW_TELEMETRY).
    Returns the number of records ingested. See docs/factory-telemetry.md.

    ``ticket`` tags every ingested record — unconditional here (unlike the
    transcript ingest's attribution guard) because an OTEL capture is a
    deliberate wrap of exactly one run: the caller already scoped what's in it.
    """
    path = Path(path)
    if not path.is_file():
        return IngestCount(0)
    n = 0
    skipped = 0
    failed = 0
    # Both the log's existing ids AND the ones seen in this pass: a single
    # capture can contain the same event twice (overlapping wraps), so
    # checking only what is already on disk would still double-count within
    # one run.
    seen_ids, seen_contentless = _ingested_claude_keys()
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
        if ticket is not None:
            rec["ticket"] = str(ticket)

        content_key, id_key = _otel_request_keys(e, rec)
        # An id-bearing event is a repeat if that id is known, or if the same
        # content was already recorded from a capture that had no id (the
        # crossover). An id-less event can only be matched by content.
        duplicate = (id_key in seen_ids if id_key else False) \
            or content_key in seen_contentless
        if duplicate:
            skipped += 1
            continue
        # Both are persisted so a later pass can match on either, whichever
        # identity that capture happens to carry.
        rec["request_id"] = id_key or content_key
        rec["content_key"] = content_key

        if _append(rec):
            # Claim the key only once the record is actually on disk. Marking
            # it seen before the write means a failed append consumes the key:
            # the record is not written, yet a later copy of the same event in
            # this pass is skipped as a duplicate of something that does not
            # exist. Losing a record to a full disk is bad; losing it and
            # reporting it as a duplicate is worse.
            if id_key:
                seen_ids.add(id_key)
            else:
                seen_contentless.add(content_key)
            n += 1
        else:
            failed += 1
    if failed:
        # ingested + skipped would otherwise not account for the whole file,
        # and a ledger whose arithmetic does not close is not evidence.
        print(f"factory_log: WARNING {failed} record(s) could not be written"
              f" while ingesting {path}", file=sys.stderr)
    return IngestCount(n, skipped)


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
    queries: dict[str, dict] = {}
    # Claude Code's own token cost, split orchestrator (repl_main_thread) vs subagent,
    # and (when the OTEL events carry skill.name/agent.name) by loop/validation.
    claude_code: dict[str, dict] = {}
    by_loop: dict[str, dict] = {}
    consult_cost = cc_cost = 0.0
    for r in records:
        if r.get("event") == GATE and r.get("name"):
            g = gates.setdefault(r["name"], {"runs": 0, "passed": 0, "failed": 0,
                                             "caught": 0, "caught_max": 0,
                                             "runs_with_findings": 0, "total_ms": 0.0})
            g["runs"] += 1
            g["passed"] += 1 if r.get("result") == "pass" else 0
            g["failed"] += 1 if r.get("result") in ("fail", "error") else 0
            caught = r.get("caught") or 0
            # `caught` is a per-run finding COUNT, so summing it counts the same
            # unfixed finding once per run: a gate re-run 140 times over one
            # repo's unchanged orphan list scored 75,517. Keep the sum (readers
            # depend on it) but carry the two honest denominators alongside it.
            g["caught"] += caught
            g["caught_max"] = max(g["caught_max"], caught)
            g["runs_with_findings"] += 1 if caught else 0
            g["total_ms"] += r.get("duration_ms") or 0.0
            ids = r.get("finding_ids")
            if isinstance(ids, list):
                g.setdefault("_ids", set()).update(str(i) for i in ids)
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
                                              "tokens_out": 0, "cache_read": 0,
                                              "cache_creation": 0, "cost_usd": 0.0})
            cc["calls"] += 1
            cc["tokens_in"] += r.get("tokens_in") or 0
            cc["tokens_out"] += r.get("tokens_out") or 0
            # Cache tokens are reported separately from fresh input because they
            # are priced separately (see cost_for_usage) — folding them into
            # tokens_in would make the token column disagree with the cost column.
            cc["cache_read"] += r.get("cache_read") or 0
            cc["cache_creation"] += r.get("cache_creation") or 0
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
        elif r.get("event") == QUERY:
            key = r.get("verb") or "unknown"
            q = queries.setdefault(key, {"calls": 0, "hits": 0, "misses": 0})
            q["calls"] += 1
            hc = r.get("hit_count")
            if hc:
                q["hits"] += 1
            else:
                q["misses"] += 1
    # value/noise hint: a gate with runs but zero caught is a noise candidate.
    # The value SIGNAL is distinct findings, not the re-counted sum — see the
    # `caught` accumulation above.
    for g in gates.values():
        ids = g.pop("_ids", None)
        if ids is not None:
            g["caught_distinct"] = len(ids)
            g["caught_basis"] = "finding-ids"
        else:
            # No finding IDs emitted: the largest single run is the best
            # available LOWER BOUND on distinct findings, and unlike the sum it
            # cannot be inflated by re-running the gate on unchanged state.
            g["caught_distinct"] = g["caught_max"]
            g["caught_basis"] = "max-run (lower bound; gate emits no finding_ids)"
        g["value"] = ("earning" if g["caught_distinct"] > 0
                      else ("noise-candidate" if g["runs"] >= 3 else "unproven"))
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
            # #375 proposal 6: loop latency measured rather than felt. Before
            # this, phase_summary() existed and nothing ever called it.
            "phases": phase_summary(records),
            "cost_by_loop": by_loop, "verdict": cost_value_verdict(gates, by_loop),
            "escapes": escapes, "escapes_total": sum(es["escaped"] for es in escapes.values()),
            "queries": queries, "queries_total": sum(q["calls"] for q in queries.values()),
            "records": len(records),
            "consult_cost_usd": round(consult_cost, 4),
            "claude_code_cost_usd": round(cc_cost, 4),
            "cost_usd_total": round(consult_cost + cc_cost, 4)}


def cost_value_verdict(gates: dict, by_loop: dict) -> dict:
    """Join cost (per loop/validation) with value (findings caught) into a keep/demote
    verdict per validation — the "every loop is costed and its value quantified" view.

    A gate's value is its DISTINCT findings (`caught_distinct`, not the re-counted
    `caught` sum); its cost is what its loop spent. cost_per_catch is the dollars
    spent per finding surfaced. The verdict:
      - earning          — caught > 0 (deterministic gates: free value; LLM loops: paid but productive)
      - demote-candidate — spent real $ over >=3 runs and caught nothing (noise you're paying for)
      - noise-candidate  — ran >=3 times, caught nothing, MEASURED ~free (noisy but cheap)
      - unproven         — too few runs to judge
      - unpriced         — ran >=3 times, caught nothing, and no cost was attributed
                           to it, so we cannot say whether it is cheap noise or
                           expensive noise

    **`cost_usd` is None, not 0.0, when nothing was attributed.** That distinction is
    the whole point of this function. Cost reaches a gate only via `cost_by_loop`,
    which is populated from Claude Code records carrying a loop/skill name; a gate
    with no such record has UNKNOWN cost, not zero cost. Defaulting it to 0.0 (the
    prior behaviour) made every gate that caught anything read `earning` at a
    confident `$0.000/catch`, and made `demote-candidate` — the one verdict that
    tells you to switch a gate off — unreachable, since it requires cost > 0. A
    fabricated denominator is worse than an absent one: it reads as a measurement.
    """
    # Only validations get a verdict — a name must have emitted gate events. A loop
    # with cost but no gate events (e.g. `implement`, the build loop / orchestrator)
    # is build cost, not a validation, and belongs in cost_by_loop, not the verdict.
    out: dict[str, dict] = {}
    for name in gates:
        g, loop = gates.get(name, {}), by_loop.get(name, {})
        raw_cost = loop.get("cost_usd")
        cost = round(raw_cost, 6) if raw_cost is not None else None
        caught = g.get("caught_distinct", g.get("caught", 0))
        runs = g.get("runs", 0) or loop.get("calls", 0)
        if caught > 0:
            v = "earning"
        elif runs < 3:
            v = "unproven"
        elif cost is None:
            v = "unpriced"
        elif cost > 0:
            v = "demote-candidate"
        else:
            v = "noise-candidate"
        out[name] = {"cost_usd": cost, "caught": caught, "runs": runs,
                     "caught_total": g.get("caught", 0),
                     "caught_basis": g.get("caught_basis"),
                     "cost_per_catch": (round(cost / caught, 6)
                                        if caught and cost is not None else None),
                     "verdict": v}
    return out


_VERDICT_ORDER = {"demote-candidate": 0, "unpriced": 1, "noise-candidate": 2,
                  "unproven": 3, "earning": 4}


# ---- session cost shape (report-only) ---------------------------------------
# Thresholds for session_cost_report. Calibrated against a real 65-session /
# 13k-turn corpus where sessions >=100 turns were 49% of sessions but 97% of
# cache-read cost, so the bar sits above the point where amplification starts
# dominating rather than at the median.
LONG_SESSION_TURNS = 200
HIGH_PEAK_CONTEXT = 500_000       # half the 1M window
TTL_1H_DOMINANT_SHARE = 0.9


def session_cost_report(records: list[dict], *, top: int = 10) -> dict:
    """What a factory run's Claude Code cost is actually made of, and why.

    **Report-only, and deliberately not a gate.** Everything it can find is
    something CW cannot act on: it can't end a long session, set a cache TTL, or
    turn a whole-file read into a grep — those are operator and harness
    behaviours. A checker that can never block has no business holding
    `--gate` (docs/gate-rollout.md), so this only ever prints.

    The thing worth measuring is **amplification, not verbosity**. Tool output is
    small in absolute terms; what makes it expensive is that a token placed in
    context is re-read on every later turn of the session. So cost tracks
    session length x context size, not how chatty any single tool was — and the
    report is built to show that: cost split by component, per-session
    amplification (cache reads / peak context), and concentration across
    sessions.

    Denominators are always printed alongside counts: a finding that says "3
    long sessions" without "out of 65, carrying 64% of spend" is unactionable.
    """
    pricing, mult = load_pricing(), load_cache_multipliers()
    m_read = mult.get("cache_read", 1.0)
    m_w5 = mult.get("cache_write_5m", 1.0)
    m_w1 = mult.get("cache_write_1h", 1.0)

    comp = {"cache_read": 0.0, "cache_write": 0.0, "output": 0.0, "input": 0.0}
    sessions: dict[str, dict] = {}
    turns = 0
    w5_tokens = w1_tokens = 0
    unpriced_turns = 0
    legacy_ttl_turns = 0

    for r in records:
        if r.get("event") != CLAUDE_CODE:
            continue
        turns += 1
        tin = r.get("tokens_in") or 0
        tout = r.get("tokens_out") or 0
        cr = r.get("cache_read") or 0
        # Fall back to the combined figure for records written before the
        # per-TTL split existed; assume the cheaper 5m rather than overstate.
        w5 = r.get("cache_write_5m")
        w1 = r.get("cache_write_1h")
        if w5 is None and w1 is None:
            # Legacy record (pre-TTL-split ingest): the TTL mix is unknown, so
            # price at the cheaper 5m rate and COUNT it, so the report can say
            # the write figure is a lower bound rather than quietly understating.
            w5, w1 = r.get("cache_creation") or 0, 0
            legacy_ttl_turns += 1 if w5 else 0
        w5, w1 = w5 or 0, w1 or 0
        w5_tokens += w5
        w1_tokens += w1

        row = pricing.get(r.get("model")) or {}
        pin, pout = row.get("input_per_mtok"), row.get("output_per_mtok")
        if pin is None or pout is None:
            unpriced_turns += 1
            pin = pout = 0.0
        comp["cache_read"] += cr / 1e6 * pin * m_read
        comp["cache_write"] += (w5 * m_w5 + w1 * m_w1) / 1e6 * pin
        comp["output"] += tout / 1e6 * pout
        comp["input"] += tin / 1e6 * pin

        sid = r.get("session_id") or "unknown"
        s = sessions.setdefault(sid, {"session_id": sid, "turns": 0, "cache_read": 0,
                                      "peak_context": 0, "context_sum": 0,
                                      "cost_usd": 0.0, "repo": r.get("repo")})
        s["turns"] += 1
        s["cache_read"] += cr
        s["cost_usd"] += r.get("cost_usd") or 0.0
        ctx = cr + w5 + w1 + tin
        s["peak_context"] = max(s["peak_context"], ctx)
        s["context_sum"] += ctx

    total = sum(comp.values())
    for s in sessions.values():
        s["cost_usd"] = round(s["cost_usd"], 4)
        # How many times over the session re-read its own context. The honest
        # denominator for "was this session too long".
        s["amplification"] = (round(s["cache_read"] / s["peak_context"], 1)
                              if s["peak_context"] else None)
        s["mean_context"] = s["context_sum"] // s["turns"] if s["turns"] else 0
        # The normalised unit cost, and the one number that is comparable across
        # sessions of different lengths: dollars per turn per 100k of context
        # carried. On a real corpus, cost-per-turn correlates +0.75 with mean
        # context but only +0.40 with turn count — width is the driver, not
        # length — so this is what to drive down.
        s["usd_per_turn_per_100k"] = (
            round((s["cost_usd"] / s["turns"]) / (s["mean_context"] / 100_000), 4)
            if s["turns"] and s["mean_context"] else None)
        s.pop("context_sum", None)

    ranked = sorted(sessions.values(), key=lambda s: -s["cost_usd"])
    cost_total = sum(s["cost_usd"] for s in ranked)
    findings: list[dict] = []

    def share(x: float) -> float:
        return round(100 * x / cost_total, 1) if cost_total else 0.0

    if total:
        ctx_share = 100 * (comp["cache_read"] + comp["cache_write"]) / total
        if ctx_share >= 50:
            findings.append({
                "code": "context-dominates-cost", "severity": "info",
                "detail": (f"{ctx_share:.0f}% of Claude Code spend is context handling "
                           f"(cache reads ${comp['cache_read']:,.2f} + writes "
                           f"${comp['cache_write']:,.2f}), not generation "
                           f"(output ${comp['output']:,.2f}). Shortening sessions moves "
                           f"this number; trimming tool output barely does."),
            })

    long_sessions = [s for s in ranked if s["turns"] >= LONG_SESSION_TURNS]
    if long_sessions:
        cost = sum(s["cost_usd"] for s in long_sessions)
        findings.append({
            "code": "long-sessions", "severity": "warn",
            "detail": (f"{len(long_sessions)} of {len(ranked)} session(s) ran >= "
                       f"{LONG_SESSION_TURNS} turns and carry {share(cost)}% of Claude Code "
                       f"cost (${cost:,.2f} of ${cost_total:,.2f}). Length is where the spend "
                       f"sits, but see 'narrow-the-context-not-the-session' before acting: "
                       f"turn count is the weaker of the two drivers."),
            "evidence": [f"{s['turns']} turns, {s['peak_context']//1000}k peak context, "
                         f"{s['amplification']}x re-read, ${s['cost_usd']:,.2f}"
                         for s in long_sessions[:5]],
        })

    wide = [s for s in ranked if s["peak_context"] >= HIGH_PEAK_CONTEXT]
    if wide:
        findings.append({
            "code": "high-peak-context", "severity": "warn",
            "detail": (f"{len(wide)} of {len(ranked)} session(s) peaked above "
                       f"{HIGH_PEAK_CONTEXT // 1000}k tokens of context. Every subsequent "
                       f"turn re-reads that whole window, so anything loaded early is "
                       f"paid for repeatedly."),
            "evidence": [f"{s['peak_context']//1000}k peak, {s['turns']} turns, "
                         f"${s['cost_usd']:,.2f}" for s in wide[:5]],
        })

    # Width, not length, is the lever — so say so, and name the one intervention
    # that narrows context without dropping work: push wide exploration into
    # sub-agents, which run their own context and return only findings.
    wide_cost = sum(s["cost_usd"] for s in ranked if s["mean_context"] >= HIGH_PEAK_CONTEXT // 2)
    if wide_cost and share(wide_cost) >= 25:
        findings.append({
            "code": "narrow-the-context-not-the-session", "severity": "info",
            "detail": (f"${wide_cost:,.2f} ({share(wide_cost)}%) is spent in sessions averaging "
                       f">= {HIGH_PEAK_CONTEXT // 2000}k context per turn. Cost per turn tracks "
                       f"context WIDTH far more than session LENGTH, so splitting a session in "
                       f"two saves little if both halves stay wide. Delegating wide, noisy work "
                       f"(broad searches, log trawls, multi-file reads) to sub-agents does: they "
                       f"carry their own context and return only findings."),
        })

    if ranked and len(ranked) > top:
        head = sum(s["cost_usd"] for s in ranked[:top])
        if share(head) >= 50:
            findings.append({
                "code": "cost-concentrated", "severity": "info",
                "detail": (f"the {top} most expensive of {len(ranked)} sessions carry "
                           f"{share(head)}% of Claude Code cost (${head:,.2f} of "
                           f"${cost_total:,.2f}) — optimising the long tail is not worth it."),
            })

    writes = w5_tokens + w1_tokens
    if writes and (w1_tokens / writes) >= TTL_1H_DOMINANT_SHARE:
        # Advisory, NOT a recommendation to switch: the 1h TTL exists to survive
        # idle gaps, and on bursty work 5m can cause more re-writes than it saves.
        saving = (w1_tokens * (m_w1 - m_w5)) / 1e6 * 5.0
        findings.append({
            "code": "cache-ttl-1h-dominant", "severity": "info",
            "detail": (f"{100 * w1_tokens / writes:.0f}% of cache-creation tokens "
                       f"({w1_tokens:,} of {writes:,}) use the 1h TTL, which bills {m_w1}x "
                       f"vs {m_w5}x for 5m — order ${saving:,.0f} at Opus input rates. "
                       f"Worth checking against your turn cadence, NOT worth switching "
                       f"blind: 1h exists to survive idle gaps, and on bursty work 5m can "
                       f"trigger more re-writes than it saves."),
        })

    return {
        "turns": turns,
        "sessions": len(ranked),
        "cost_usd": round(cost_total, 4),
        "unpriced_turns": unpriced_turns,
        "legacy_ttl_turns": legacy_ttl_turns,
        "composition_usd": {k: round(v, 4) for k, v in comp.items()},
        "composition_share": ({k: round(100 * v / total, 1) for k, v in comp.items()}
                              if total else {}),
        "top_sessions": ranked[:top],
        "findings": findings,
        "thresholds": {"long_session_turns": LONG_SESSION_TURNS,
                       "high_peak_context": HIGH_PEAK_CONTEXT,
                       "ttl_1h_dominant_share": TTL_1H_DOMINANT_SHARE},
        "gate": False,  # report-only by construction — see the docstring
    }


def render_cost_report(rep: dict) -> str:
    L = [f"Claude Code session cost — {rep['sessions']} session(s), {rep['turns']} turn(s), "
         f"${rep['cost_usd']:,.2f}", ""]
    if not rep["turns"]:
        L.append("  No claude_code records. Run:")
        L.append("    factory_log.py ingest-claude-transcripts")
        return "\n".join(L)
    if rep["unpriced_turns"]:
        L.append(f"  NOTE: {rep['unpriced_turns']} of {rep['turns']} turn(s) ran on a model with "
                 f"no price row — their tokens count, their cost does not.")
        L.append("")
    if rep.get("legacy_ttl_turns"):
        L.append(f"  NOTE: {rep['legacy_ttl_turns']} turn(s) predate the cache-TTL split and are "
                 f"priced at the cheaper 5m rate,\n        so cache_write below is a LOWER BOUND. "
                 f"Re-run ingest-claude-transcripts on a fresh log to resolve.")
        L.append("")
    L.append("  Where the money goes:")
    for k, v in sorted(rep["composition_usd"].items(), key=lambda kv: -kv[1]):
        L.append(f"    {k:<14}${v:>10,.2f}   {rep['composition_share'].get(k, 0):>5.1f}%")
    if rep["top_sessions"]:
        L.append("\n  Most expensive sessions:")
        L.append(f"    {'COST':>10}{'TURNS':>7}{'PEAK CTX':>10}{'RE-READ':>9}  SESSION")
        L.append("    " + "-" * 56)
        for s in rep["top_sessions"]:
            L.append(f"    ${s['cost_usd']:>9,.2f}{s['turns']:>7}"
                     f"{s['peak_context'] // 1000:>9}k{str(s['amplification']) + 'x':>9}  "
                     f"{str(s['session_id'])[:8]}")
    if rep["findings"]:
        L.append("\n  Findings (report-only — CW cannot act on these, you can):")
        for f in rep["findings"]:
            L.append(f"    [{f['severity']}] {f['code']}: {f['detail']}")
            for ev in f.get("evidence", [])[:5]:
                L.append(f"        - {ev}")
    return "\n".join(L)


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
                      key=lambda kv: (_VERDICT_ORDER.get(kv[1]["verdict"], 9),
                                      -(kv[1]["cost_usd"] or 0.0)))
        for name, v in rows:
            # "—" for an unattributed cost, never "$0.00": no cost reached this
            # gate, which is not the same as measuring that it was free.
            cost = f"${v['cost_usd']:.2f}" if v["cost_usd"] is not None else "—"
            cpc = f"${v['cost_per_catch']:.3f}" if v["cost_per_catch"] is not None else "—"
            L.append(f"    {name:<22}{v['runs']:>5}{v['caught']:>7}{cost:>10}{cpc:>10}   {v['verdict']}")
        L.append("    CAUGHT is distinct findings (max single run unless the gate emits "
                 "finding_ids), not\n    the re-counted per-run sum. '—' cost = nothing "
                 "attributed, not measured-free.")

    escapes = agg.get("escapes") or {}
    if escapes:
        L.append(f"\n  Escapes — bugs a gate missed ({agg.get('escapes_total', 0)} total). "
                  "Recall = caught / (caught + escaped):")
        L.append(f"    {'MISSED BY':<22}{'CAUGHT':>7}{'ESCAPED':>9}{'FIXED':>7}   RECALL")
        L.append("    " + "─" * 55)
        for name, e in sorted(escapes.items(), key=lambda kv: -kv[1]["escaped"]):
            recall = f"{e['recall']:.0%}" if e["recall"] is not None else "—"
            L.append(f"    {name:<22}{e['caught']:>7}{e['escaped']:>9}{e['fixed']:>7}   {recall}")

    queries = agg.get("queries") or {}
    if queries:
        L.append(f"\n  code_query.py verbs asked ({agg.get('queries_total', 0)} total calls):")
        L.append(f"    {'VERB':<16}{'CALLS':>7}{'HITS':>7}{'MISSES':>8}")
        L.append("    " + "─" * 40)
        for name, q in sorted(queries.items(), key=lambda kv: -kv[1]["calls"]):
            L.append(f"    {name:<16}{q['calls']:>7}{q['hits']:>7}{q['misses']:>8}")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Factory telemetry emitter / aggregator.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("emit", help="Append a telemetry event")
    e.add_argument("--event", required=True, choices=[GATE, CONSULT, WORKER, SKILL, PHASE])
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

    ph = sub.add_parser(
        "phase",
        help="Record how long one workflow phase took (#375). Skills are bash, "
             "so this verb is how a phase boundary reaches the ledger at all.")
    ph.add_argument("--name", required=True, help="Phase name, e.g. step4a_consults")
    ph.add_argument("--since", type=float,
                    help="Epoch seconds captured by `factory_log.py now` at the phase start")
    ph.add_argument("--duration-ms", type=float, help="Explicit duration instead of --since")
    ph.add_argument("--ticket")
    ph.add_argument("--repo")
    ph.add_argument("--outcome", default="ok", choices=["ok", "error"],
                    help="A phase that blew up fast would otherwise look like a "
                         "phase that went well")
    ph.add_argument("--detail")

    sub.add_parser("now",
                   help="Print epoch seconds for `phase --since`. A verb rather "
                        "than `date` because BSD date has no %%3N and CW targets macOS.")

    a = sub.add_parser("aggregate", help="Summarize the log")
    a.add_argument("--repo")
    a.add_argument("--format", choices=["text", "json"], default="json")

    ic = sub.add_parser("ingest-claude-code", help="Fold a Claude Code OTEL export into the log")
    ic.add_argument("--ticket", help="Tag every ingested record with this ticket number "
                                     "(the capture is a deliberate wrap of one run)")
    ic.add_argument("otel_file", help="JSONL from `... 2>capture.jsonl` with the console OTEL exporter")
    ic.add_argument("--repo", help="Tag ingested records with this repo")

    it = sub.add_parser("ingest-claude-transcripts",
                        help="Fold Claude Code's own session transcripts (token cost) into the log")
    it.add_argument("--root", default=str(DEFAULT_TRANSCRIPT_ROOT),
                    help=f"Transcript root (default: {DEFAULT_TRANSCRIPT_ROOT})")
    it.add_argument("--repo", help="Force this repo tag instead of deriving it from each record's cwd")
    it.add_argument("--since-days", type=float,
                    help="Only ingest turns newer than N days ago")
    it.add_argument("--since-ts", type=float,
                    help="Only ingest turns newer than this epoch timestamp "
                         "(e.g. a per-ticket build-start stamp; overrides --since-days)")
    it.add_argument("--ticket",
                    help="Tag matching turns with this ticket number for per-ticket cost "
                         "slicing (scripts/ticket_cost.py). Guarded, never blind: needs "
                         "--cwd-prefix or a cwd-derived repo match against --repo")
    it.add_argument("--cwd-prefix",
                    help="Only tag turns whose cwd starts with this path (worktree-precise "
                         "attribution for parallel /implement-wave workers)")
    it.add_argument("--until-ts", type=float,
                    help="Only ingest turns at/before this epoch timestamp — bounds a "
                         "catch-up ingest so it cannot consume an in-flight ticket's turns")

    cr_ = sub.add_parser("cost-report",
                         help="What Claude Code session cost is made of (report-only, never a gate)")
    cr_.add_argument("--repo")
    cr_.add_argument("--top", type=int, default=10)
    cr_.add_argument("--format", choices=["text", "json"], default="text")

    sub.add_parser("path", help="Print the log path")
    args = parser.parse_args(argv)

    if args.cmd == "path":
        print(log_path())
        return 0
    if args.cmd == "ingest-claude-code":
        n = ingest_claude_code(Path(args.otel_file), repo=args.repo, ticket=args.ticket)
        skipped = getattr(n, "skipped", 0)
        # Always say what was skipped, including zero. A count that appears
        # only when non-zero leaves the reader unable to tell "no duplicates"
        # from "this build does not report them".
        print(f"factory_log: ingested {int(n)} api_request event(s) from "
              f"{args.otel_file}, skipped {skipped} duplicate(s)")
        return 0
    if args.cmd == "cost-report":
        records = read_log()
        if args.repo:
            records = [r for r in records if r.get("repo") == args.repo]
        rep = session_cost_report(records, top=args.top)
        # Always exit 0 — report-only by construction (see session_cost_report).
        print(json.dumps(rep, indent=2) if args.format == "json" else render_cost_report(rep))
        return 0
    if args.cmd == "ingest-claude-transcripts":
        since = (time.time() - args.since_days * 86400) if args.since_days else None
        if args.since_ts:
            since = args.since_ts
        try:
            n = ingest_claude_transcripts(Path(args.root), repo=args.repo, since=since,
                                          ticket=args.ticket, cwd_prefix=args.cwd_prefix,
                                          until=args.until_ts)
        except ValueError as exc:
            print(f"factory_log: {exc}", file=sys.stderr)
            return 2
        # Re-running is expected and safe (dedup is by request id), so say
        # explicitly that "0 new" means up-to-date rather than broken.
        print(f"factory_log: ingested {n} new Claude Code turn(s) from {args.root}"
              f"{' (already up to date)' if n == 0 else ''}")
        return 0
    if args.cmd == "now":
        print(repr(time.time()))
        return 0
    if args.cmd == "phase":
        if args.duration_ms is None and args.since is None:
            print("factory_log: pass --since (from `factory_log.py now`) or --duration-ms",
                  file=sys.stderr)
            return 2
        duration = (args.duration_ms if args.duration_ms is not None
                    else max(0.0, (time.time() - args.since) * 1000.0))
        extra = {"detail": args.detail} if args.detail else {}
        ok = emit_phase(args.name, duration_ms=duration, repo=args.repo,
                        ticket=args.ticket, outcome=args.outcome, **extra)
        if not ok:
            print("factory_log: telemetry disabled (set CW_TELEMETRY=1 to enable)",
                  file=sys.stderr)
            return 1
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
