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


def _in_window(r: dict, cwd_prefix: str | None, since: float | None,
              until: float | None) -> bool:
    """A claude_code record attributable to the slice by WHERE and WHEN it ran.

    Complements the ticket tag rather than replacing it. The tag is applied at
    first ingest and dedup makes it un-reapplicable, so a turn swept up by a
    catch-up ingest (or by a concurrent worker's ingest) can never be tagged
    afterwards — but its cwd and timestamp still say which build it belongs to.
    Consult records carry no cwd; they rely on the call-time tag (#345 AC2).
    """
    if cwd_prefix is None:
        return False
    cwd = r.get("cwd")
    if not cwd or not str(cwd).startswith(str(cwd_prefix).rstrip("/")):
        return False
    ts = r.get("ts") or 0
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


def summarize_ticket(records: list[dict], repo: str, ticket: str, *,
                     cwd_prefix: str | None = None, since: float | None = None,
                     until: float | None = None) -> dict:
    """Slice the ledger to one ticket and fold it into per-layer totals.

    ``status: "unmetered"`` (with every total ``None``) when nothing matches —
    absence of telemetry, not a $0 build. ``cost_partial`` is set when any
    matched record carries tokens but no priced cost, so a table with a dollar
    total can never silently understate.

    ``cwd_prefix``/``since``/``until`` add a READ-TIME window slice on top of
    the ticket tag (never a replacement — a record matching either counts once,
    see ``_in_window``): a turn a catch-up ingest swept up untagged, or one a
    sibling ``/implement-wave`` worker ingested, can still be recovered by
    where and when it ran (chief-wiggum#345)."""
    layers = {
        name: {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cache_tokens": 0,
               "cost_usd": 0.0, "unpriced_calls": 0}
        for name in ("orchestrator", "subagents", "consults")
    }
    providers: set[str] = set()
    matched = 0
    for r in records:
        if r.get("event") not in _LAYERED_EVENTS:
            continue
        if not (_matches_ticket(r, repo, ticket)
                or _in_window(r, cwd_prefix, since, until)):
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
    total = round(sum(layer["cost_usd"] for layer in layers.values()), 4)
    partial = any(layer["unpriced_calls"] for layer in layers.values())
    return {"repo": repo, "ticket": str(ticket), "status": "metered",
            "records": matched, "layers": layers, "total_cost_usd": total,
            "cost_partial": partial, "consult_providers": sorted(providers)}


# ---- coverage: the capture denominator (AC4) ----------------------------------

COVERAGE_STATUSES = ("captured", "captured-partial", "uncaptured", "unknown")


def _layer_coverage(calls: int, unpriced_calls: int, evidence_n: int | None,
                    scanned: bool, *, fix: str | None) -> dict:
    """One layer's coverage verdict, derived from evidence INDEPENDENT of the
    slice being rendered (see ``coverage``'s docstring for the taxonomy)."""
    if calls > 0:
        if unpriced_calls:
            return {"status": "captured-partial",
                   "basis": f"{calls} calls, {unpriced_calls} unpriced", "fix": None}
        return {"status": "captured", "basis": f"{calls} calls captured", "fix": None}
    if not scanned:
        return {"status": "unknown",
               "basis": "no independent evidence available for this window", "fix": None}
    if evidence_n:
        return {"status": "uncaptured",
               "basis": f"{evidence_n:,} transcript turns in window, 0 ingested", "fix": fix}
    return {"status": "captured", "basis": "no turns in window", "fix": None}


def coverage(summary: dict, records: list[dict], *, transcript_root=None,
             since: float | None = None, until: float | None = None,
             cwd_prefix: str | None = None) -> dict:
    """Per-layer capture status for the rendered slice — the denominator.

    A cost table that silently omits the layers it never saw invites a reader to
    take a consult-only figure as the total (#345). Each layer's status is
    derived from a source INDEPENDENT of the slice, and the four outcomes stay
    distinct (the #289 taxonomy — "failed to observe" must never read as "pass"):

      captured          — records present, every call priced
      captured-partial  — records present, n of m calls unpriced (both printed)
      uncaptured        — zero records AND independent evidence the layer ran
      unknown           — zero records and no visible evidence source; never
                          "captured", never $0

    ``records`` is the FULL ledger slice (not just what matched the ticket) —
    the consults check needs to see untagged same-repo consults to catch the
    #381 fingerprint (a review quorum that ran but never attributed).
    """
    repo = summary.get("repo")
    ticket = summary.get("ticket")
    layers_in = summary.get("layers") or {}

    # An unbounded scan of the whole transcript corpus at PR time is not worth
    # its cost — with no window, the Claude layers report `unknown`, never a
    # fabricated `captured`/`$0`.
    if since is None:
        evidence = {"scanned": False, "repl_main_thread": 0, "subagent": 0}
    else:
        evidence = factory_log.count_transcript_turns(
            transcript_root, since=since, until=until, repo=repo, cwd_prefix=cwd_prefix)

    fix_cmd = (f"factory_log.py ingest-claude-transcripts --repo {repo} "
              f"--ticket {ticket} --since-ts <build-start>")
    layers: dict[str, dict] = {}
    for key, bucket in (("orchestrator", "repl_main_thread"), ("subagents", "subagent")):
        layer = layers_in.get(key) or {"calls": 0, "unpriced_calls": 0}
        layers[key] = _layer_coverage(layer.get("calls", 0), layer.get("unpriced_calls", 0),
                                      evidence.get(bucket, 0), evidence["scanned"], fix=fix_cmd)

    consult_layer = layers_in.get("consults") or {"calls": 0, "unpriced_calls": 0}
    calls = consult_layer.get("calls", 0)
    if calls > 0:
        unpriced = consult_layer.get("unpriced_calls", 0)
        if unpriced:
            layers["consults"] = {"status": "captured-partial",
                                  "basis": f"{calls} calls, {unpriced} unpriced", "fix": None}
        else:
            layers["consults"] = {"status": "captured", "basis": f"{calls} calls captured",
                                  "fix": None}
    elif since is None:
        layers["consults"] = {"status": "unknown",
                              "basis": "no window supplied — untagged-consult evidence not scanned",
                              "fix": None}
    else:
        untagged = 0
        for r in records:
            if r.get("event") != factory_log.CONSULT or r.get("ticket") is not None:
                continue
            rrepo = r.get("repo")
            if repo is not None and rrepo is not None and rrepo not in (repo, str(repo).split("/")[-1]):
                continue
            ts = r.get("ts") or 0
            if ts < since or (until is not None and ts > until):
                continue
            untagged += 1
        if untagged:
            layers["consults"] = {
                "status": "uncaptured",
                "basis": (f"{untagged} untagged consult(s) in window for this repo — the "
                         "review quorum's spend did not attribute"),
                "fix": "pass --ticket to run_review.py / consult_ai.py",
            }
        else:
            layers["consults"] = {"status": "captured", "basis": "no consult spend in window",
                                  "fix": None}

    captured_statuses = {"captured", "captured-partial"}
    captured_layers = sum(1 for L in layers.values() if L["status"] in captured_statuses)
    if captured_layers == 3:
        top_status = "complete"
    elif (layers["consults"]["status"] in captured_statuses
          and layers["orchestrator"]["status"] not in captured_statuses
          and layers["subagents"]["status"] not in captured_statuses):
        top_status = "consults-only"
    elif captured_layers > 0:
        top_status = "partial"
    elif any(L["status"] == "uncaptured" for L in layers.values()):
        top_status = "uncaptured"
    else:
        top_status = "unknown"

    return {"layers": layers, "captured_layers": captured_layers, "total_layers": 3,
            "status": top_status}


def render_coverage_markdown(cov: dict) -> str:
    """Markdown table naming which layers were captured and which weren't — the
    denominator that keeps a partial slice from being mistaken for the total
    (chief-wiggum#345)."""
    labels = {"orchestrator": "Orchestrator", "subagents": "Sub-agents", "consults": "Consults"}
    status_labels = {
        "captured": "captured",
        "captured-partial": "captured (partial)",
        "uncaptured": "UNCAPTURED",
        "unknown": "unknown",
    }
    header = (f"**Coverage — {cov['captured_layers']} of {cov['total_layers']} "
             "layers captured.**")
    rows = ["| Layer | Status | Basis |", "|---|---|---|"]
    for key in ("orchestrator", "subagents", "consults"):
        layer = cov["layers"][key]
        basis = layer["basis"]
        if layer.get("fix"):
            basis += f" — run `{layer['fix']}`"
        rows.append(f"| {labels[key]} | {status_labels[layer['status']]} | {basis} |")
    return header + "\n\n" + "\n".join(rows)


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


def render_actual_markdown(summary: dict, estimate_usd: float | None = None,
                           cov: dict | None = None) -> str:
    """Markdown body for the PR's ``## Implementation Cost`` section (no heading —
    ``shipping.build_pr_body`` owns the heading, like Model Conformance).

    ``cov`` (from ``coverage()``) appends the layer-capture denominator above
    the footnote — in BOTH branches, so a consult-only or fully-unmetered
    slice still tells the reader which layers are known-missing (#345)."""
    if summary["status"] == "unmetered":
        md = (
            f"**Unmetered** — no cost records for {summary['repo']}#{summary['ticket']} "
            "in the factory ledger. That is absence of telemetry, not a $0 build: "
            "run `factory_log.py ingest-claude-transcripts --repo <owner/repo> "
            "--ticket <n>` (and set `CW_TELEMETRY=1` before consults) to meter the "
            "next one."
        )
        if cov is not None:
            md += "\n\n" + render_coverage_markdown(cov)
        return md
    rows = [
        "| Layer | Calls | Tokens in | Cache | Tokens out | Cost |",
        "|---|---|---|---|---|---|",
    ]
    labels = {"orchestrator": "Orchestrator", "subagents": "Subagents", "consults": "Consults"}
    for key, label in labels.items():
        layer = summary["layers"][key]
        if not layer["calls"]:
            continue
        if key == "consults" and summary["consult_providers"]:
            label = f"Consults ({', '.join(summary['consult_providers'])})"
        cost = f"${layer['cost_usd']:.2f}" + (" *" if layer["unpriced_calls"] else "")
        rows.append(f"| {label} | {layer['calls']} | {_fmt_tokens(layer['tokens_in'])} | "
                    f"{_fmt_tokens(layer['cache_tokens'])} | {_fmt_tokens(layer['tokens_out'])} | {cost} |")
    total = f"**${summary['total_cost_usd']:.2f}**"
    rows.append(f"| **Total** | | | | | {total} |")
    lines = ["\n".join(rows), ""]
    if estimate_usd is not None:
        lines.append(_variance_line(estimate_usd, summary["total_cost_usd"]))
        lines.append("")
    if cov is not None:
        lines.append(render_coverage_markdown(cov))
        lines.append("")
    note = ("Nominal model spend (tokens × `config/model_pricing.json`, cache-aware "
            "for Claude Code layers). Human time and CI excluded.")
    if summary["cost_partial"]:
        unpriced = sum(layer["unpriced_calls"] for layer in summary["layers"].values())
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


def _print_summary(summary: dict, fmt: str, estimate_usd: float | None,
                   cov: dict | None = None) -> None:
    if fmt == "json":
        out = dict(summary)
        if cov is not None:
            out["coverage"] = cov
        print(json.dumps(out, indent=2))
    elif fmt == "markdown":
        print(render_actual_markdown(summary, estimate_usd, cov=cov))
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
        if cov is not None:
            for key, label in (("orchestrator", "orchestrator"), ("subagents", "subagents"),
                               ("consults", "consults")):
                layer = cov["layers"][key]
                if layer["status"] not in ("captured", "captured-partial"):
                    print(f"ticket_cost: coverage {cov['captured_layers']}/"
                          f"{cov['total_layers']} layers — {label} "
                          f"{layer['status'].upper()} ({layer['basis']})")


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
    a.add_argument("--cwd-prefix", help="Also attribute claude_code turns whose cwd sits under "
                                        "this path (worktree-precise read-time slicing)")
    a.add_argument("--since-ts", type=float, help="Window start (epoch) for read-time slicing "
                                                  "and for the coverage evidence scan")
    a.add_argument("--until-ts", type=float, help="Window end (epoch)")
    a.add_argument("--transcript-root", help="Transcript root for the coverage evidence scan "
                                             "(default: ~/.claude/projects)")
    a.add_argument("--exit-code-on-gap", action="store_true",
                   help="Exit 3 when coverage is not complete (opt-in; the default stays "
                        "report-only, exit 0)")

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
        summary = summarize_ticket(records, args.repo, args.ticket,
                                   cwd_prefix=args.cwd_prefix, since=args.since_ts,
                                   until=args.until_ts)
        transcript_root = Path(args.transcript_root) if args.transcript_root else None
        cov = coverage(summary, records, transcript_root=transcript_root,
                       since=args.since_ts, until=args.until_ts, cwd_prefix=args.cwd_prefix)
        _print_summary(summary, args.format, args.estimate, cov=cov)
        return 3 if (args.exit_code_on_gap and cov["status"] != "complete") else 0
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
