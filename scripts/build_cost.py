#!/usr/bin/env python3
"""Build-cost tracking per bet: nominal (API-priced) and actual (plan-share) — chief-wiggum#257.

The bet ledger already meters cash (``bet.py spend``) and operator hours, but the
dominant input to a bet is often **factory compute**, and it was invisible: a bet can
consume a large share of a fixed monthly plan while its recorded cash spend reads
`$8.48`. This extends the existing pricing/attribution machinery
(``scripts/factory_log.py`` + ``config/model_pricing.json``, INV-fh-002: prices come
from the config, never memory; a cost is *omitted*, not zeroed, when a model has no
listed price) from gates to bets.

**Two fields, never one**, per record:

- ``nominal_usd`` — tokens x ``config/model_pricing.json`` (via
  ``factory_log.cost_for``). The comparable, portable number across bets and against
  cash spend. Omitted (never ``0``) when the model has no listed price.
- ``plan_share_pct`` — share of the billing period's plan CAPACITY this record
  consumed. The true economic cost under a fixed-price plan, where marginal API price
  overstates real outlay. This script never derives it (a harness token-accounting
  API is not assumed to exist): the caller supplies it, or it is recorded
  ``{unresolved}`` — reported honestly, never guessed, never silently zero.

**Attribution** is by explicit ``--attribute-to``: a bet id, the literal ``factory``
(CW's own development), or the literal ``unattributed`` — session/task -> bet mapping
is recorded AT THE TIME OF WORK, never reconstructed later. ``unattributed`` usage is
its own explicit bucket, always reported, never silently dropped or spread pro-rata
across bets (the same sum-preserving-attribution discipline as the in-flight
`platform-cost-observability` pattern, #229).

Records are journaled into the SAME portfolio hash chain ``bet.py`` uses (this module
imports its verified journal reader/writer, the same importing-sibling shape as
``channel.py``/``assumption.py``) — an append-only, tamper-evident ``build-cost`` event
per record.

Usage:
    build_cost.py record --attribute-to <bet-id|factory|unattributed> \\
        --model <model-id> --tokens-in N --tokens-out N \\
        [--plan-share-pct X] [--note "..."]
    build_cost.py summary --attribute-to <bet-id|factory|unattributed> [--format json]
    build_cost.py portfolio [--format json]   # every attribution bucket, sum-preserving

Exit codes: 0 = ok, 2 = usage/config error, 4 = journal tamper detected (fail closed).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bet as betlib  # noqa: E402  (journal reader/writer + portfolio_root, reused not forked)
import factory_log  # noqa: E402  (cost_for — the grounded per-model pricing table)
from bet import BetError  # noqa: E402
from ratchet import TamperError  # noqa: E402

FACTORY = "factory"
UNATTRIBUTED = "unattributed"
EVENT = "build-cost"


def record_build_cost(
    root: Path, attribute_to: str, model: str, tokens_in: int, tokens_out: int,
    plan_share_pct: float | None, note: str,
) -> dict:
    """Journal one build-cost record. ``attribute_to`` is a bet id, 'factory', or
    'unattributed' — never validated against the bet ledger (a bet may not exist yet
    when factory work on it begins), consistent with attribution being recorded at
    the time of work, not reconstructed later."""
    if tokens_in < 0 or tokens_out < 0:
        raise BetError("--tokens-in/--tokens-out must be non-negative")
    nominal = factory_log.cost_for(model, tokens_in, tokens_out)
    details: dict = {
        "attribute_to": attribute_to,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "note": note or "",
    }
    if nominal is not None:
        details["nominal_usd"] = nominal
    # plan_share_pct is honestly {unresolved} when not supplied — never guessed,
    # never silently zero (the harness does not generally expose true consumption).
    details["plan_share_pct"] = plan_share_pct
    details["plan_share_unresolved"] = plan_share_pct is None
    return betlib.append_event(root, EVENT, attribute_to, details)


def load_build_costs(root: Path, attribute_to: str | None = None) -> list[dict]:
    records = betlib.load_journal(root)
    out = [r for r in records if r.get("event") == EVENT]
    if attribute_to is not None:
        out = [r for r in out if r.get("ref") == attribute_to]
    return out


def summarize(records: list[dict]) -> dict:
    """Aggregate a list of build-cost journal records. Sum-preserving: every
    record counts exactly once, `nominal_partial`/`plan_share_partial` name whether
    any entry was priced/estimated-only, never silently averaged away."""
    nominal_total = 0.0
    nominal_partial = False
    plan_share_total = 0.0
    plan_share_partial = False
    n = len(records)
    for r in records:
        d = r.get("details", {}) or {}
        if d.get("nominal_usd") is not None:
            nominal_total += d["nominal_usd"]
        else:
            nominal_partial = True
        if d.get("plan_share_pct") is not None:
            plan_share_total += d["plan_share_pct"]
        else:
            plan_share_partial = True
    return {
        "records": n,
        "nominal_usd": round(nominal_total, 6) if n else None,
        "nominal_partial": nominal_partial,
        "plan_share_pct": round(plan_share_total, 4) if (n and not plan_share_partial) else None,
        "plan_share_partial": plan_share_partial,
    }


def portfolio_summary(root: Path) -> dict:
    """Every attribution bucket seen in the journal — bets, `factory`, and
    `unattributed` — each summarized independently. Sum-preserving: nothing is
    dropped or spread pro-rata; a bucket with zero records simply doesn't appear."""
    records = betlib.load_journal(root)
    build_costs = [r for r in records if r.get("event") == EVENT]
    by_bucket: dict[str, list[dict]] = {}
    for r in build_costs:
        by_bucket.setdefault(r.get("ref", UNATTRIBUTED), []).append(r)
    return {bucket: summarize(recs) for bucket, recs in sorted(by_bucket.items())}


def cmd_record(args) -> int:
    root = betlib.portfolio_root(args.portfolio_dir)
    rec = record_build_cost(
        root, args.attribute_to, args.model, args.tokens_in, args.tokens_out,
        args.plan_share_pct, args.note,
    )
    d = rec["details"]
    nominal = f"${d['nominal_usd']:g}" if d.get("nominal_usd") is not None else "UNRESOLVED (unpriced model)"
    share = f"{d['plan_share_pct']:g}%" if d.get("plan_share_pct") is not None else "UNRESOLVED (not supplied)"
    print(
        f"build_cost: recorded for {args.attribute_to} ({rec['record_id']}) — "
        f"nominal {nominal}, plan-share {share}"
    )
    return 0


def cmd_summary(args) -> int:
    root = betlib.portfolio_root(args.portfolio_dir)
    records = load_build_costs(root, args.attribute_to)
    summary = summarize(records)
    summary["attribute_to"] = args.attribute_to
    if args.format == "json":
        print(json.dumps(summary, indent=2))
    else:
        print(f"build_cost summary for {args.attribute_to}: {summary['records']} record(s)")
        print(f"  nominal_usd: {summary['nominal_usd']}"
              + (" (partial — some entries unpriced)" if summary["nominal_partial"] else ""))
        print(f"  plan_share_pct: {summary['plan_share_pct']}"
              + (" (partial — some entries unresolved)" if summary["plan_share_partial"] else ""))
    return 0


def cmd_portfolio(args) -> int:
    root = betlib.portfolio_root(args.portfolio_dir)
    summary = portfolio_summary(root)
    if args.format == "json":
        print(json.dumps(summary, indent=2))
    else:
        if not summary:
            print("build_cost: no records journaled yet")
        for bucket, s in summary.items():
            print(f"{bucket}: {s['records']} record(s), nominal_usd={s['nominal_usd']}"
                  f"{' (partial)' if s['nominal_partial'] else ''}, "
                  f"plan_share_pct={s['plan_share_pct']}"
                  f"{' (partial)' if s['plan_share_partial'] else ''}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument(
            "--portfolio-dir", default=None,
            help="portfolio repo (default: $CHIEF_WIGGUM_PORTFOLIO or ~/.chief-wiggum/portfolio)",
        )

    sp = sub.add_parser("record", help="journal one build-cost record")
    common(sp)
    sp.add_argument("--attribute-to", required=True, metavar="BET_ID|factory|unattributed")
    sp.add_argument("--model", required=True, help="model id (config/model_pricing.json)")
    sp.add_argument("--tokens-in", type=int, required=True)
    sp.add_argument("--tokens-out", type=int, required=True)
    sp.add_argument("--plan-share-pct", type=float, default=None, metavar="PCT",
                     help="share of the billing period's plan capacity consumed "
                          "(absent -> UNRESOLVED, never guessed)")
    sp.add_argument("--note", default="")

    sp = sub.add_parser("summary", help="aggregate build-cost records for one attribution bucket")
    common(sp)
    sp.add_argument("--attribute-to", required=True, metavar="BET_ID|factory|unattributed")
    sp.add_argument("--format", choices=["text", "json"], default="text")

    sp = sub.add_parser("portfolio", help="every attribution bucket, sum-preserving")
    common(sp)
    sp.add_argument("--format", choices=["text", "json"], default="text")

    args = p.parse_args()
    dispatch = {"record": cmd_record, "summary": cmd_summary, "portfolio": cmd_portfolio}
    try:
        return dispatch[args.cmd](args)
    except BetError as e:
        sys.stderr.write(f"build_cost: {e}\n")
        return 2
    except TamperError as e:
        sys.stderr.write(f"build_cost: {e}\n")
        return 4


if __name__ == "__main__":
    sys.exit(main())
