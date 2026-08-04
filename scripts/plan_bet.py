#!/usr/bin/env python3
"""Business-model authoring stage (chief-wiggum#238, Track B of docs/business-factory.md §8).

`/plan-bet`-shaped like `/design` (Decision 1): product-level, once per bet,
a divergence-then-human-choice checkpoint upstream (the `.claude/commands/
plan-bet.md` workflow), and mechanical extraction into a binding artifact
here — ``bets/<bet-id>/business-model.json`` (``templates/
business-model-schema.json``). Once `/architect` starts folding it into
epics, this file is a GOALPOST the same way `bet.json`'s envelope and
`kill-criteria.json` are: content-hashed into the portfolio journal at
``author``; ``rebaseline`` is the only sanctioned mutation path.

Ticket #238 was PARKED until Track A (#235) and Track C (#236) had run
against >=1 real bet — the schema below is derived from four real portfolio
bets (accrualflow, rabbitry, gov-tender-channel, confluence-doc-control),
not speculated; see the schema file's own description and the #238 PR for
which field came from which bet.

Three checks, per Decision 2/3/4/5:

- **Fermi viability gate** (``fermi_findings`` / ``fermi_required_leads``):
  Maurya's reverse-income-statement — ``{MSC, price, churn_assumption,
  funnel_assumptions, TAM}`` -> required top-of-funnel throughput. This is
  the ONE check in the whole authoring stage that is NOT behind the usual
  report-only-until-validated ramp (docs/gate-rollout.md): an arithmetic
  impossibility BLOCKS ``author``/``rebaseline``/``check`` unconditionally,
  ``--gate`` or not (Decision 3 — a deliberate, stated exception; it gates
  authoring, not built work, so precision risk is a non-issue: either the
  bet's own declared numbers reach TAM or they don't). Declaring no
  ``fermi_inputs`` at all is a normal ``skipped:`` finding, never a block —
  only a DECLARED, arithmetically-impossible set of inputs blocks.
- **Premortem coverage** (``premortem_findings``): >=N (default 5) failure
  modes, each mapped to an ``ASM-NNN`` in this bet's ``assumptions.json`` or
  explicitly waived with a non-empty reason (Klein 2007).
- **Pain<->reliever bipartite completeness** (``vpc_findings``): every VPC
  pain addressed by >=1 reliever and every reliever addressing >=1 real
  pain — no orphan pains, no solution-in-search-of-a-problem relievers.
- **e3-value per-actor viability** (``e3_value_findings``, Decision 5):
  ONLY evaluated when ``structure.actor_types`` declares MORE THAN 2 actors
  — a single/dual-actor model (the common solo-SaaS shape) is
  ``inapplicable`` by construction, never a silent pass on missing data.

All findings/gates follow docs/gate-rollout.md (report-only by default,
``--gate`` to block) EXCEPT the Fermi gate as noted above. ``check`` reports
the standard four-state outcome (chief-wiggum#289): ``pass | findings |
inapplicable | error`` — no ``business-model.json`` for the bet is
``inapplicable`` (nothing to measure); a file that fails schema validation
is ``error`` (a broken instrument, never a silent pass), same shape as
``check_traceability.py``.

Subcommands:
    author      validate a candidate business-model.json against the schema
                and every check, then hash + journal it as the bet's binding
                artifact (refuses on a Fermi arithmetic impossibility or, with
                --gate, on any other real finding)
    rebaseline  the ONLY sanctioned change to an authored business-model.json
                (--reason required, old->new hash journaled)
    check       re-validate the journaled business-model.json: schema +
                every check + goalpost integrity; reports the four-state
                outcome

Exit codes: 0 = ok / report-only findings, 1 = gate violation (--gate) or a
Fermi hard block (unconditional), 2 = usage/config error, 4 = journal tamper
detected (fail closed).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Same importing-sibling shape as assumption.py/channel.py: portfolio
# resolution, journal chain, content hashing, and the report discipline are
# imported, not copied.
import bet as betlib  # noqa: E402
from bet import BetError, TamperError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "templates" / "business-model-schema.json"
BUSINESS_MODEL_NAME = "business-model.json"

DEFAULT_MIN_FAILURE_MODES = 5
# e3-value per-actor viability triggers only when MORE THAN this many actor
# types are declared (Decision 5) — a single/dual-actor model skips it.
E3_ACTOR_THRESHOLD = 2

CANVAS_FIELD_IDS = (
    "problem", "customer-segment", "value-proposition", "solution", "channels",
    "revenue-streams", "cost-structure", "key-metrics", "unfair-advantage",
    "pricing", "competitive-position",
)
FIELD_STATUSES = ("hypothesis", "validated", "falsified")


# ---- schema -----------------------------------------------------------------


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def schema_validation_findings(business_model: dict) -> list[str]:
    """Draft2020-12 validation against templates/business-model-schema.json.
    A schema failure means the artifact itself is malformed — reported as
    `error:`-prefixed so callers can treat it as a broken instrument, never
    a silent pass (same discipline check_traceability.py uses)."""
    validator = jsonschema.Draft202012Validator(_schema())
    return [
        f"error: {'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in sorted(validator.iter_errors(business_model), key=lambda e: [str(p) for p in e.absolute_path])
    ]


# ---- files --------------------------------------------------------------------


def business_model_path(root: Path, bet_id: str) -> Path:
    return betlib.bet_dir(root, bet_id) / BUSINESS_MODEL_NAME


def load_business_model(root: Path, bet_id: str) -> dict | None:
    path = business_model_path(root, bet_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise BetError(f"cannot parse {path}: {e}") from e


def save_business_model(root: Path, bet_id: str, bm: dict) -> None:
    business_model_path(root, bet_id).write_text(json.dumps(bm, indent=2, sort_keys=True) + "\n")


# ---- Fermi viability gate (Decision 3 — hard block, day zero) -----------------


def fermi_required_leads(fermi_inputs: dict) -> dict:
    """Maurya's reverse-income-statement arithmetic. `churn_assumption` is
    treated as inflating the funnel throughput needed to net out at the
    required steady-state customer count over the same window the funnel is
    measured across — a standard Fermi-check simplification, not a
    multi-period simulation; documented here rather than left implicit."""
    msc = fermi_inputs["msc_usd"]
    price = fermi_inputs["price_usd"]
    churn = fermi_inputs["churn_assumption"]
    funnel = fermi_inputs["funnel_assumptions"]
    tam = fermi_inputs["tam"]
    required_customers = msc / price
    churn_adjusted_customers = required_customers / (1 - churn)
    overall_conversion = 1.0
    for r in funnel:
        overall_conversion *= r
    required_leads = (
        churn_adjusted_customers / overall_conversion if overall_conversion > 0 else float("inf")
    )
    return {
        "required_customers": required_customers,
        "churn_adjusted_customers": churn_adjusted_customers,
        "overall_conversion": overall_conversion,
        "required_leads": required_leads,
        "tam": tam,
        "viable": required_leads <= tam,
    }


def fermi_findings(business_model: dict) -> list[str]:
    """`fermi:`-prefixed findings are the Decision-3 hard block (callers must
    treat them as unconditional, never gated behind --gate). No fermi_inputs
    declared at all is a normal `skipped:` finding — only a DECLARED,
    arithmetically-impossible set of inputs blocks."""
    fi = business_model.get("fermi_inputs")
    if fi is None:
        return [
            "skipped: fermi_inputs not declared — the Fermi viability gate was not "
            "evaluated (chief-wiggum#238 Decision 3)"
        ]
    r = fermi_required_leads(fi)
    if r["viable"]:
        return []
    return [
        f"fermi: ARITHMETIC IMPOSSIBILITY — reaching ${fi['msc_usd']:,.0f}/period at "
        f"${fi['price_usd']:,.2f}/customer with {fi['churn_assumption']:.0%} churn and "
        f"{r['overall_conversion']:.2%} overall funnel conversion requires "
        f"{r['required_leads']:,.0f} top-of-funnel prospects, but the declared TAM is "
        f"only {fi['tam']:,.0f} — this bet's own numbers cannot reach its MSC "
        "(Maurya's Fermi/reverse-income-statement gate, day zero, before any spend)"
    ]


# ---- premortem coverage (Decision 4) -------------------------------------------


def premortem_findings(business_model: dict, min_n: int = DEFAULT_MIN_FAILURE_MODES) -> list[str]:
    pm = business_model.get("premortem") or []
    out = []
    if len(pm) < min_n:
        out.append(
            f"premortem: only {len(pm)} failure mode(s) recorded, need >= {min_n} "
            "(Klein 2007 — chief-wiggum#238 Decision 4)"
        )
    for i, fm in enumerate(pm):
        fid = fm.get("id") or f"premortem[{i}]"
        has_asm = bool(fm.get("asm_id"))
        waived = bool(fm.get("waived"))
        if not has_asm and not waived:
            out.append(f"{fid}: uncovered — neither mapped to an ASM nor explicitly waived")
        if waived and not str(fm.get("waiver_reason") or "").strip():
            out.append(
                f"{fid}: waived with no waiver_reason — an unexplained waiver is "
                "indistinguishable from a forgotten one"
            )
    return out


# ---- pain<->reliever bipartite completeness (VPC) ------------------------------


def vpc_findings(business_model: dict) -> list[str]:
    vpc = business_model.get("vpc")
    if not vpc:
        return ["skipped: no vpc block — pain<->reliever completeness not evaluated"]
    pains = vpc.get("pains") or []
    relievers = vpc.get("pain_relievers") or []
    pain_ids = {p.get("id") for p in pains}
    addressed: set[str] = set()
    out = []
    for r in relievers:
        rid = r.get("id", "?")
        edges = r.get("addresses_pain_ids") or []
        if not edges:
            out.append(f"{rid}: addresses no pain — a solution in search of a problem")
        for pid in edges:
            if pid not in pain_ids:
                out.append(
                    f"{rid}: dangling edge — addresses_pain_ids references {pid!r}, no such pain"
                )
            else:
                addressed.add(pid)
    for p in pains:
        pid = p.get("id", "?")
        if pid not in addressed:
            out.append(f"{pid}: uncovered — no pain reliever addresses this pain")
    return out


# ---- e3-value per-actor viability (Decision 5) ---------------------------------


def e3_value_findings(business_model: dict) -> tuple[str, list[str]]:
    """(outcome, findings) where outcome is 'inapplicable' (<=2 actor types —
    the common solo-SaaS shape, skipped by construction), 'pass', or
    'findings'. Missing per-actor flow data on a >2-actor model is a finding,
    never a silent pass — absence of data is not evidence of viability."""
    structure = business_model.get("structure") or {}
    actors = structure.get("actor_types") or []
    if len(actors) <= E3_ACTOR_THRESHOLD:
        return "inapplicable", []
    e3 = business_model.get("e3_value") or {}
    flows = {f.get("actor"): f for f in (e3.get("actor_flows") or [])}
    out = []
    for a in actors:
        name = a.get("name")
        flow = flows.get(name)
        if flow is None or flow.get("revenue_usd") is None or flow.get("cost_usd") is None:
            out.append(
                f"e3-value: actor {name!r} has no revenue/cost flow recorded — per-actor "
                "viability unresolved (>2 actor types declared, Decision 5)"
            )
            continue
        net = flow["revenue_usd"] - flow["cost_usd"]
        if net <= 0:
            out.append(
                f"e3-value: actor {name!r} nets ${net:,.2f} (revenue "
                f"${flow['revenue_usd']:,.2f} - cost ${flow['cost_usd']:,.2f}) — not >0; "
                "this multi-actor model fails per-actor viability for this actor"
            )
    return ("findings" if out else "pass"), out


# ---- canvas/vpc field status<->evidence consistency ----------------------------


def _field_status_findings(field: dict, ref: str) -> list[str]:
    if not isinstance(field, dict):
        return [f"{ref}: not an object"]
    out = []
    status = field.get("status")
    if status not in FIELD_STATUSES:
        out.append(f"{ref}: unknown status {status!r} (want {'|'.join(FIELD_STATUSES)})")
    if status in ("validated", "falsified") and not field.get("asm_ids"):
        out.append(
            f"{ref}: status {status!r} with no asm_ids — a status change must cite the "
            "evidence that produced it (the omission-evasion shape assumption.py already "
            "catches for the ASM ledger itself)"
        )
    return out


def canvas_status_findings(business_model: dict) -> list[str]:
    out = []
    canvas = business_model.get("canvas") or {}
    for fid in CANVAS_FIELD_IDS:
        if fid in canvas:
            out += _field_status_findings(canvas[fid], fid)
    vpc = business_model.get("vpc") or {}
    for p in vpc.get("pains") or []:
        out += _field_status_findings(p, p.get("id", "vpc.pains[?]"))
    for r in vpc.get("pain_relievers") or []:
        out += _field_status_findings(r, r.get("id", "vpc.pain_relievers[?]"))
    return out


# ---- ASM join (assumptions.json <-> business-model.json canvas/vpc ids) -------


def _valid_element_ids(business_model: dict) -> set[str]:
    ids = set(CANVAS_FIELD_IDS)
    vpc = business_model.get("vpc") or {}
    ids |= {p.get("id") for p in vpc.get("pains") or [] if p.get("id")}
    ids |= {r.get("id") for r in vpc.get("pain_relievers") or [] if r.get("id")}
    return ids


def _all_asm_ids(business_model: dict) -> set[str]:
    ids: set[str] = set()
    canvas = business_model.get("canvas") or {}
    for fid in CANVAS_FIELD_IDS:
        f = canvas.get(fid)
        if isinstance(f, dict):
            ids |= set(f.get("asm_ids") or [])
    vpc = business_model.get("vpc") or {}
    for p in vpc.get("pains") or []:
        ids |= set(p.get("asm_ids") or [])
    for r in vpc.get("pain_relievers") or []:
        ids |= set(r.get("asm_ids") or [])
    for fm in business_model.get("premortem") or []:
        if fm.get("asm_id"):
            ids.add(fm["asm_id"])
    return ids


def asm_join_findings(business_model: dict, assumptions: list[dict] | None) -> list[str]:
    """Cross-artifact traceability: an assumption's `depends_on_element`
    should resolve to a real canvas/VPC field id (the join key Decision 2
    grounds directly in real bets' assumptions.json usage), and every
    asm_id a business-model.json field cites should resolve to a real
    ledger entry. `assumptions` is None (no assumptions.json yet) ->
    skipped, never a crash, never a silent pass on the join itself."""
    if assumptions is None:
        return ["skipped: no assumptions.json for this bet — ASM<->canvas join not evaluated"]
    out = []
    valid_elements = _valid_element_ids(business_model)
    asm_ids = {a.get("id") for a in assumptions}
    for a in assumptions:
        elem = a.get("depends_on_element")
        if elem and elem not in valid_elements:
            out.append(
                f"{a.get('id', '?')}: depends_on_element {elem!r} does not match any "
                "business-model.json canvas/vpc field id"
            )
    for cited in _all_asm_ids(business_model):
        if cited not in asm_ids:
            out.append(f"business-model.json cites {cited!r}, no such assumption in assumptions.json")
    return out


# ---- goalpost hashing (author/rebaseline pattern, same as bet.json) ----------


def bm_baseline(records: list[dict], bet_id: str) -> str | None:
    h = None
    for rec in betlib.bet_events(records, bet_id):
        d = rec.get("details", {}) or {}
        if rec.get("event") == "business-model-authored":
            h = d.get("business_model_hash", h)
        elif rec.get("event") == "business-model-rebaseline":
            h = d.get("new_business_model_hash", h)
    return h


def goalpost_integrity_findings(root: Path, bet_id: str, bm: dict, records: list[dict]) -> list[str]:
    baseline = bm_baseline(records, bet_id)
    if baseline is None:
        return [
            f"{bet_id}: business-model.json exists with no journaled authoring record — "
            "created outside `plan_bet.py author`"
        ]
    if betlib.content_hash(bm) != baseline:
        return [
            f"{bet_id}: business-model.json hash does not match the journaled baseline — "
            "edited outside `plan_bet.py rebaseline` (goalposts moved)"
        ]
    return []


# ---- aggregate checks -----------------------------------------------------------


def semantic_findings(root: Path, bet_id: str, bm: dict, min_failure_modes: int) -> list[str]:
    """Every non-schema check, combined — used by both `author`/`rebaseline`
    (validating a candidate before commit) and `check` (validating what was
    committed). Does NOT include goalpost integrity — that only applies once
    something has been journaled."""
    from assumption import (
        load_assumptions,  # noqa: PLC0415 (sibling import, lazy to avoid a cycle at module load)
    )

    assumptions_path = betlib.bet_dir(root, bet_id) / "assumptions.json"
    assumptions = load_assumptions(root, bet_id) if assumptions_path.is_file() else None

    out: list[str] = []
    out += fermi_findings(bm)
    out += premortem_findings(bm, min_failure_modes)
    out += vpc_findings(bm)
    _e3_outcome, e3_out = e3_value_findings(bm)
    out += e3_out
    out += canvas_status_findings(bm)
    out += asm_join_findings(bm, assumptions)
    return out


def check_outcome(root: Path, bet_id: str, min_failure_modes: int = DEFAULT_MIN_FAILURE_MODES) -> tuple[str, list[str]]:
    """The standard four-state outcome (chief-wiggum#289): pass | findings |
    inapplicable | error. `error` is a broken instrument (schema-invalid or
    unparsable business-model.json) — never a silent pass. `inapplicable`
    is a legitimate not-yet-authored state — also never a silent pass, it
    is reported explicitly."""
    betlib.load_bet(root, bet_id)  # bet must exist — a usage error otherwise (exit 2)
    path = business_model_path(root, bet_id)
    if not path.is_file():
        return "inapplicable", ["inapplicable: no business-model.json for this bet — nothing to check yet"]
    try:
        bm = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return "error", [f"error: cannot parse {path}: {e}"]
    schema_errors = schema_validation_findings(bm)
    if schema_errors:
        return "error", schema_errors

    records = betlib.load_journal(root)
    findings = semantic_findings(root, bet_id, bm, min_failure_modes)
    findings += goalpost_integrity_findings(root, bet_id, bm, records)

    real = [f for f in findings if not f.startswith(betlib.NEVER_GATES_PREFIXES)]
    outcome = "pass" if not real else "findings"
    return outcome, findings


# ---- subcommand plumbing --------------------------------------------------------


def _print_findings(findings: list[str], gate: bool, label: str = "plan_bet") -> None:
    for f in findings:
        is_fermi = f.startswith("fermi:")
        if is_fermi:
            tag = "BLOCKED"
        elif gate and not f.startswith(betlib.NEVER_GATES_PREFIXES):
            tag = "gated"
        else:
            tag = "report-only"
        print(f"{label}: [{tag}] {f}")


def cmd_author(args) -> int:
    root = betlib.portfolio_root(args.portfolio_dir)
    betlib.load_bet(root, args.bet_id)  # bet must exist
    betlib.load_journal(root)  # verified read first — never journal atop a tampered chain
    try:
        bm = json.loads(Path(args.file).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise BetError(f"cannot read/parse {args.file}: {e}") from e

    schema_errors = schema_validation_findings(bm)
    if schema_errors:
        _print_findings(schema_errors, args.gate)
        print(f"plan_bet: author on {args.bet_id} REFUSED — business-model.json fails schema validation")
        return 1
    if bm.get("bet_id") != args.bet_id:
        raise BetError(
            f"business-model.json bet_id {bm.get('bet_id')!r} does not match {args.bet_id!r}"
        )

    findings = semantic_findings(root, args.bet_id, bm, args.min_failure_modes)
    _print_findings(findings, args.gate)
    if any(f.startswith("fermi:") for f in findings):
        print(
            f"plan_bet: author on {args.bet_id} REFUSED — Fermi viability gate failed "
            "(chief-wiggum#238 Decision 3: unconditional hard block, day zero)"
        )
        return 1
    real = [f for f in findings if not f.startswith(betlib.NEVER_GATES_PREFIXES)]
    if args.gate and real:
        print(f"plan_bet: author on {args.bet_id} REFUSED (--gate)")
        return 1

    save_business_model(root, args.bet_id, bm)
    h = betlib.content_hash(bm)
    rec = betlib.append_event(root, "business-model-authored", args.bet_id, {"business_model_hash": h})
    print(
        f"plan_bet: business-model.json authored for {args.bet_id} ({rec['record_id']}) — "
        "content hashed into the journal"
    )
    return 0


def cmd_rebaseline(args) -> int:
    root = betlib.portfolio_root(args.portfolio_dir)
    betlib.load_bet(root, args.bet_id)
    existing = load_business_model(root, args.bet_id)
    if existing is None:
        raise BetError(f"no business-model.json authored yet for {args.bet_id} — run `author` first")
    records = betlib.load_journal(root)
    old_hash = bm_baseline(records, args.bet_id)

    try:
        bm = json.loads(Path(args.file).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise BetError(f"cannot read/parse {args.file}: {e}") from e
    schema_errors = schema_validation_findings(bm)
    if schema_errors:
        _print_findings(schema_errors, args.gate)
        print(f"plan_bet: rebaseline on {args.bet_id} REFUSED — business-model.json fails schema validation")
        return 1
    if bm.get("bet_id") != args.bet_id:
        raise BetError(
            f"business-model.json bet_id {bm.get('bet_id')!r} does not match {args.bet_id!r}"
        )

    findings = semantic_findings(root, args.bet_id, bm, args.min_failure_modes)
    _print_findings(findings, args.gate)
    if any(f.startswith("fermi:") for f in findings):
        print(
            f"plan_bet: rebaseline on {args.bet_id} REFUSED — Fermi viability gate failed "
            "(chief-wiggum#238 Decision 3: unconditional hard block, day zero)"
        )
        return 1
    real = [f for f in findings if not f.startswith(betlib.NEVER_GATES_PREFIXES)]
    if args.gate and real:
        print(f"plan_bet: rebaseline on {args.bet_id} REFUSED (--gate)")
        return 1

    save_business_model(root, args.bet_id, bm)
    new_hash = betlib.content_hash(bm)
    rec = betlib.append_event(root, "business-model-rebaseline", args.bet_id, {
        "reason": args.reason,
        "old_business_model_hash": old_hash,
        "new_business_model_hash": new_hash,
    })
    print(
        f"plan_bet: rebaselined business-model.json for {args.bet_id} ({rec['record_id']}) — "
        f"old->new hashes journaled; reason: {args.reason}"
    )
    return 0


def cmd_check(args) -> int:
    root = betlib.portfolio_root(args.portfolio_dir)
    outcome, findings = check_outcome(root, args.bet_id, args.min_failure_modes)
    _print_findings(findings, args.gate)
    print(f"plan_bet: check {args.bet_id} — outcome={outcome}")

    if any(f.startswith("fermi:") for f in findings):
        return 1  # Decision 3 — unconditional, no --gate required
    if outcome == "error":
        return 1 if args.gate else 0
    if outcome == "inapplicable":
        return 0
    real = [f for f in findings if not f.startswith(betlib.NEVER_GATES_PREFIXES)]
    return 1 if (args.gate and real) else 0


# ---- CLI -----------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument(
            "--portfolio-dir", default=None,
            help="portfolio repo (default: $CHIEF_WIGGUM_PORTFOLIO or ~/.chief-wiggum/portfolio)",
        )
        sp.add_argument(
            "--gate", action="store_true",
            help="exit 1 on any other real finding too (docs/gate-rollout.md); the Fermi "
                 "check blocks regardless of this flag (Decision 3)",
        )
        sp.add_argument(
            "--min-failure-modes", type=int, default=DEFAULT_MIN_FAILURE_MODES,
            help=f"premortem coverage floor (default {DEFAULT_MIN_FAILURE_MODES})",
        )

    sp = sub.add_parser("author", help="validate + journal a candidate business-model.json")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("--file", required=True, help="path to the candidate business-model.json")

    sp = sub.add_parser("rebaseline", help="the ONLY sanctioned change to an authored business-model.json")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("--file", required=True, help="path to the replacement business-model.json")
    sp.add_argument("--reason", required=True, help="why the model changes — journaled with old->new hashes")

    sp = sub.add_parser("check", help="re-validate the journaled business-model.json (four-state outcome)")
    common(sp)
    sp.add_argument("bet_id")

    args = p.parse_args()
    dispatch = {"author": cmd_author, "rebaseline": cmd_rebaseline, "check": cmd_check}
    try:
        return dispatch[args.cmd](args)
    except BetError as e:
        sys.stderr.write(f"plan_bet: {e}\n")
        return 2
    except TamperError as e:
        sys.stderr.write(f"plan_bet: {e}\n")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
