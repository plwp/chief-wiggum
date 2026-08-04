#!/usr/bin/env python3
"""Per-ticket implementation cost — nominal estimate on issues, measured actual on PRs.

The factory ledger (``scripts/factory_log.py``) already meters everything a build
spends — consult tokens (tagged with ``--ticket`` at call time), and the
orchestrator + subagent turns folded in by the transcript/OTEL ingests (taggable
with a ticket since the same change that added this script). What was missing is
the per-ticket slice and the surfacing: this script answers "what did building
owner/repo#42 cost" and "what will an M-sized ticket probably cost", so
``/create-issue`` can stamp a nominal estimate on the issue and ``/implement``
can stamp the measured actual on the PR.

Cost basis — **nominal model spend only**, the same definition as
``build_cost.py``'s ``nominal_usd``: tokens x ``config/model_pricing.json``
(INV-fh-002: prices come from the grounded table, never memory; an unpriced
model's cost is *omitted* and flagged ``cost_partial``, never zeroed). Human
time, CI minutes, and plan-share economics are out of scope here.

Honesty rules (the fail-open bug class this repo hunts, chief-wiggum#289):

- **No records is UNMETERED, never $0.** An empty slice means telemetry didn't
  flow (ingest not run, ``CW_TELEMETRY`` off at consult time), not that the
  build was free. The markdown says so explicitly.
- **An estimate below ``--min-samples`` calibration points is UNRESOLVED, never
  guessed.** Partially-priced actuals are excluded from the estimator (a lower
  bound would drag the p50 down) and the exclusion is reported.
- **Denominators are printed**: every estimate names its sample count.

Calibration closes the loop: ``record`` snapshots a ticket's measured actual
(plus its Effort size and, when known, the estimate that was stamped on the
issue) as a ``ticket_cost`` event in the same ledger; ``estimate`` is the p50 of
those actuals for the same Effort class. Recording is an explicit act, so —
like the ingests — it always writes, regardless of ``CW_TELEMETRY``.

Usage:
    ticket_cost.py actual   --repo owner/repo --ticket 42 [--estimate 3.20] \
                            [--format json|markdown|text]
    ticket_cost.py estimate --effort S|M|L|XL [--repo owner/repo] \
                            [--min-samples 3] [--format json|markdown|text]
    ticket_cost.py record   --repo owner/repo --ticket 42 [--effort M] [--estimate 3.20]

Report-only by construction (exit 0; 2 on usage error) — cost is information
for the human on the issue/PR, never a gate.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import factory_log  # noqa: E402  (ledger reader + the single cost-derivation path)

TICKET_COST = "ticket_cost"  # calibration event: one per shipped ticket
EFFORTS = ("S", "M", "L", "XL")
DEFAULT_MIN_SAMPLES = 3

# Consults/workers are tagged per-call; claude_code records are tagged at ingest.
_LAYERED_EVENTS = (factory_log.CONSULT, factory_log.WORKER, factory_log.CLAUDE_CODE)


def _matches_ticket(r: dict, repo: str, ticket: str) -> bool:
    """A record belongs to the slice when its ticket tag matches and its repo tag
    doesn't contradict. Repo tags come from two vocabularies — consults carry the
    ``owner/repo`` the workflow passed, transcript ingests may only derive the
    basename from cwd — so both spellings match; an absent repo defers to the
    ticket tag (which is only ever applied under an attribution guard)."""
    if str(r.get("ticket")) != str(ticket):
        return False
    rrepo = r.get("repo")
    return rrepo is None or rrepo in (repo, repo.split("/")[-1])


def _layer_of(r: dict) -> str:
    if r.get("event") == factory_log.CONSULT:
        return "consults"
    if r.get("event") == factory_log.WORKER:
        return "subagents"
    return "orchestrator" if r.get("query_source") == "repl_main_thread" else "subagents"


def summarize_ticket(records: list[dict], repo: str, ticket: str) -> dict:
    """Slice the ledger to one ticket and fold it into per-layer totals.

    ``status: "unmetered"`` (with every total ``None``) when nothing matches —
    absence of telemetry, not a $0 build. ``cost_partial`` is set when any
    matched record carries tokens but no priced cost, so a table with a dollar
    total can never silently understate."""
    layers = {
        name: {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cache_tokens": 0,
               "cost_usd": 0.0, "unpriced_calls": 0}
        for name in ("orchestrator", "subagents", "consults")
    }
    providers: set[str] = set()
    matched = 0
    for r in records:
        if r.get("event") not in _LAYERED_EVENTS or not _matches_ticket(r, repo, ticket):
            continue
        matched += 1
        layer = layers[_layer_of(r)]
        layer["calls"] += 1
        layer["tokens_in"] += r.get("tokens_in") or 0
        layer["tokens_out"] += r.get("tokens_out") or 0
        layer["cache_tokens"] += (r.get("cache_read") or 0) + (r.get("cache_creation") or 0)
        if r.get("cost_usd") is not None:
            layer["cost_usd"] += r["cost_usd"]
        else:
            layer["unpriced_calls"] += 1
        if r.get("event") == factory_log.CONSULT and r.get("provider"):
            providers.add(r["provider"])
    if not matched:
        return {"repo": repo, "ticket": str(ticket), "status": "unmetered",
                "records": 0, "layers": None, "total_cost_usd": None,
                "cost_partial": None, "consult_providers": []}
    for layer in layers.values():
        layer["cost_usd"] = round(layer["cost_usd"], 4)
    total = round(sum(l["cost_usd"] for l in layers.values()), 4)
    partial = any(l["unpriced_calls"] for l in layers.values())
    return {"repo": repo, "ticket": str(ticket), "status": "metered",
            "records": matched, "layers": layers, "total_cost_usd": total,
            "cost_partial": partial, "consult_providers": sorted(providers)}


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _variance_line(estimate_usd: float, actual_usd: float) -> str:
    if estimate_usd <= 0:
        return f"Estimated ~${estimate_usd:.2f} → actual ${actual_usd:.2f}."
    pct = (actual_usd - estimate_usd) / estimate_usd * 100
    return (f"Estimated ~${estimate_usd:.2f} → actual ${actual_usd:.2f} "
            f"({pct:+.0f}%).")


def render_actual_markdown(summary: dict, estimate_usd: float | None = None) -> str:
    """Markdown body for the PR's ``## Implementation Cost`` section (no heading —
    ``shipping.build_pr_body`` owns the heading, like Model Conformance)."""
    if summary["status"] == "unmetered":
        return (
            f"**Unmetered** — no cost records for {summary['repo']}#{summary['ticket']} "
            "in the factory ledger. That is absence of telemetry, not a $0 build: "
            "run `factory_log.py ingest-claude-transcripts --repo <owner/repo> "
            "--ticket <n>` (and set `CW_TELEMETRY=1` before consults) to meter the "
            "next one."
        )
    rows = [
        "| Layer | Calls | Tokens in | Cache | Tokens out | Cost |",
        "|---|---|---|---|---|---|",
    ]
    labels = {"orchestrator": "Orchestrator", "subagents": "Subagents", "consults": "Consults"}
    for key, label in labels.items():
        l = summary["layers"][key]
        if not l["calls"]:
            continue
        if key == "consults" and summary["consult_providers"]:
            label = f"Consults ({', '.join(summary['consult_providers'])})"
        cost = f"${l['cost_usd']:.2f}" + (" *" if l["unpriced_calls"] else "")
        rows.append(f"| {label} | {l['calls']} | {_fmt_tokens(l['tokens_in'])} | "
                    f"{_fmt_tokens(l['cache_tokens'])} | {_fmt_tokens(l['tokens_out'])} | {cost} |")
    total = f"**${summary['total_cost_usd']:.2f}**"
    rows.append(f"| **Total** | | | | | {total} |")
    lines = ["\n".join(rows), ""]
    if estimate_usd is not None:
        lines.append(_variance_line(estimate_usd, summary["total_cost_usd"]))
        lines.append("")
    note = ("Nominal model spend (tokens × `config/model_pricing.json`, cache-aware "
            "for Claude Code layers). Human time and CI excluded.")
    if summary["cost_partial"]:
        unpriced = sum(l["unpriced_calls"] for l in summary["layers"].values())
        note += (f" **Partial**: {unpriced} of {summary['records']} calls had no "
                 "priced model — the total understates.")
    lines.append(f"<sub>{note}</sub>")
    return "\n".join(lines)


# ---- calibration (estimate side) ---------------------------------------------


def record_calibration(summary: dict, effort: str | None = None,
                       estimate_usd: float | None = None) -> dict:
    """Journal one ``ticket_cost`` calibration event from a measured summary.

    Explicit act → always writes (``factory_log._append``, the same
    does-not-require-CW_TELEMETRY shape as the ingests). An unmetered ticket is
    still recorded — as ``actual_usd: None`` — so the calibration history shows
    how often metering actually flowed, not just its successes."""
    rec = {
        "ts": time.time(),
        "event": TICKET_COST,
        "repo": summary["repo"],
        "ticket": summary["ticket"],
        "status": summary["status"],
        "actual_usd": summary["total_cost_usd"],
        "cost_partial": summary["cost_partial"],
        "records": summary["records"],
    }
    if effort:
        rec["effort"] = effort
    if estimate_usd is not None:
        rec["estimate_usd"] = estimate_usd
    factory_log._append(rec)
    return rec


def estimate_for_effort(records: list[dict], effort: str, repo: str | None = None,
                        min_samples: int = DEFAULT_MIN_SAMPLES) -> dict:
    """p50 of fully-priced calibration actuals for one Effort class.

    ``repo`` narrows to one repo's history when given; the default is
    cross-repo — effort sizing is a factory-wide notion and per-repo samples
    are scarce early on. Partial/unmetered calibrations are excluded (their
    actuals are lower bounds) and counted in ``excluded`` so the denominator
    stays honest. Below ``min_samples`` the estimate is UNRESOLVED."""
    samples: list[float] = []
    excluded = 0
    for r in records:
        if r.get("event") != TICKET_COST or r.get("effort") != effort:
            continue
        if repo and r.get("repo") != repo:
            continue
        if r.get("actual_usd") is None or r.get("cost_partial"):
            excluded += 1
            continue
        samples.append(r["actual_usd"])
    out = {"effort": effort, "samples": len(samples), "excluded_partial": excluded,
           "min_samples": min_samples}
    if len(samples) >= min_samples:
        out["status"] = "ok"
        out["p50_usd"] = round(statistics.median(samples), 2)
    else:
        out["status"] = "insufficient-samples"
        out["p50_usd"] = None
    return out


def render_estimate_line(est: dict) -> str:
    """One line for the issue template's ``Nominal cost`` field."""
    if est["status"] == "ok":
        return (f"~${est['p50_usd']:.2f} (p50 of {est['samples']} prior "
                f"{est['effort']} tickets)")
    return (f"UNRESOLVED ({est['samples']} prior {est['effort']} tickets logged, "
            f"need >={est['min_samples']} — calibrates as PRs merge)")


# ---- CLI ---------------------------------------------------------------------


def _print_summary(summary: dict, fmt: str, estimate_usd: float | None) -> None:
    if fmt == "json":
        print(json.dumps(summary, indent=2))
    elif fmt == "markdown":
        print(render_actual_markdown(summary, estimate_usd))
    else:
        if summary["status"] == "unmetered":
            print(f"ticket_cost: {summary['repo']}#{summary['ticket']} UNMETERED "
                  "(no ledger records — not $0)")
        else:
            partial = " (partial — some calls unpriced)" if summary["cost_partial"] else ""
            print(f"ticket_cost: {summary['repo']}#{summary['ticket']} "
                  f"${summary['total_cost_usd']:.2f}{partial} over "
                  f"{summary['records']} metered calls")
            if estimate_usd is not None:
                print("  " + _variance_line(estimate_usd, summary["total_cost_usd"]))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("actual", help="Measured cost of one ticket from the ledger")
    a.add_argument("--repo", required=True)
    a.add_argument("--ticket", required=True)
    a.add_argument("--estimate", type=float,
                   help="The nominal estimate stamped on the issue, for the variance line")
    a.add_argument("--format", choices=["json", "markdown", "text"], default="text")

    e = sub.add_parser("estimate", help="Nominal estimate for an Effort class (p50 of calibrations)")
    e.add_argument("--effort", required=True, choices=EFFORTS)
    e.add_argument("--repo", help="Narrow calibration history to one repo (default: factory-wide)")
    e.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    e.add_argument("--format", choices=["json", "markdown", "text"], default="text")

    r = sub.add_parser("record", help="Journal one ticket's measured actual as a calibration point")
    r.add_argument("--repo", required=True)
    r.add_argument("--ticket", required=True)
    r.add_argument("--effort", choices=EFFORTS,
                   help="The issue's Effort label; omit if unknown (recorded but "
                        "won't feed the estimator)")
    r.add_argument("--estimate", type=float,
                   help="The nominal estimate that was stamped on the issue")

    args = p.parse_args()
    records = factory_log.read_log()

    if args.cmd == "actual":
        summary = summarize_ticket(records, args.repo, args.ticket)
        _print_summary(summary, args.format, args.estimate)
        return 0
    if args.cmd == "estimate":
        est = estimate_for_effort(records, args.effort, repo=args.repo,
                                  min_samples=args.min_samples)
        if args.format == "json":
            print(json.dumps(est, indent=2))
        else:
            print(render_estimate_line(est))
        return 0
    if args.cmd == "record":
        summary = summarize_ticket(records, args.repo, args.ticket)
        rec = record_calibration(summary, effort=args.effort, estimate_usd=args.estimate)
        actual = (f"${rec['actual_usd']:.2f}" if rec["actual_usd"] is not None
                  else "UNMETERED")
        print(f"ticket_cost: recorded calibration for {args.repo}#{args.ticket} — "
              f"actual {actual}" + (f", effort {args.effort}" if args.effort else ""))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
