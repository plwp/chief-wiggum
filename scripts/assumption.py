#!/usr/bin/env python3
"""Validation experiments — assumption ledger + pre-registered test cards
(chief-wiggum#236, Track C of docs/business-factory.md §2.2/§8).

The RCT-validated mechanism (Camuffo et al. 2020; SMJ 2024 replication):
pre-registered falsifiable hypotheses with quantitative decision thresholds
measurably improve killing bad ideas. This script transplants CW's existing
enforcement idioms onto business assumptions:

- **Assumption ledger** (``bets/<bet-id>/assumptions.json``,
  templates/assumptions-schema.json): stable ``ASM-NNN`` ids, status
  ``untested → testing → validated | falsified``, source
  ``premortem | financial_model | canvas``, and a ``depends_on_element`` tag
  that makes Bland's pivot dependency rule mechanical. This two-segment
  ``ASM-NNN`` is a DELIBERATELY separate namespace from the system-layer
  ``ASM`` stable-ID kind in ``chief_wiggum.trace_ids.ID_KINDS`` (three-segment
  ``ASM-slug-NNN``, e.g. architecture.json ``asm_refs``) — the two share the
  ``ASM-`` prefix but are structurally disjoint grammars (a two-segment
  ledger id never has the second hyphen the stable-ID grammar requires) and
  scoped to different artifacts: this ledger lives at
  ``bets/<bet-id>/assumptions.json``, never under ``docs/epics/``, so no
  trace_ids-based scanner ever walks it. See chief-wiggum#294;
  ``tests/test_trace_ids.py`` pins the disjointness.
- **Test cards** (``test-cards.json``, templates/test-cards-schema.json):
  ``{card_id, asm_id, method, metric, threshold, sample_min,
  cost_estimate_usd, evidence_strength, result, verdict}``. The **threshold
  block is content-hashed into the portfolio journal at card creation**
  (``bet.append_event`` — same ratchet-format chain, fail-closed on tamper);
  a verdict may only be recorded against the original hash. Changing a
  threshold is a journaled ``rebaseline`` (old→new hash, ``--reason``
  required) — no post-hoc success definitions, ever.
- **Falsifiability linter**: the hypothesis must parse to Savoia's XYZ
  grammar — "at least X% of Y will Z" with numeric X, a concrete population Y
  (not "people"/"users"), and a measurable behavior Z (not an opinion verb).
  The linter is a parser, so un-falsifiable phrasing is syntactically
  impossible, not merely discouraged.
- **Vanity-metric lint**: cumulative/gross counters (``total_*``,
  ``cumulative_*``, ``lifetime_*``) are rejected as success criteria — they
  falsify nothing (Ries); the required form is a per-cohort rate.
- **Evidence-strength enum is FIXED** (Fitzpatrick/Bland/Savoia converge):
  1 opinion, 2 click/engagement, 3 time (scheduled session, real trial),
  4 reputation (intro, public commitment), 5 money (pre-order, deposit, paid
  conversion). Interview-class methods cap an assumption's EFFECTIVE strength
  at 1 regardless of the declared value or the interview count (the Mom
  Test/Bland floor: interviews alone can never exceed weak confidence).
  ``bet.py transition <id> building`` calls this module's
  ``building_floor_findings``: ≥1 validated ASM at strength ≥4 — Blank's
  "purchase orders, not enthusiasm". No assumptions.json → the floor reports
  ``skipped``, never blocks silently.
- **Traceability gate** (``check`` — check_traceability.py's shape with new
  node types): every ASM ↔ ≥1 test card. Uncovered assumptions, dangling
  cards, a ``validated`` status with no supporting card verdict (the
  evasion-omission seed), verdicts contradicting their pre-registered
  comparator, and cards with no journaled pre-registration are all findings.
- **Pivot dependency rule** (Bland; wired in ``bet.py transition
  --changed-elements``): a pivot copies the ledger to the successor bet and
  re-opens (``validated → untested``) every ASM whose ``depends_on_element``
  matches a changed element. Test cards do NOT carry over — evidence was
  registered against the old thesis, so coverage must be re-established.

All lints/gates are report-only by default per docs/gate-rollout.md (findings
print, exit 0; blocking only with ``--gate``); no workflow passes ``--gate``
until a validation record exists — dogfood one real bet through card → run →
verdict first (§8 grounding discipline). The stamped experiment patterns
(``patterns/landing-page-smoke-test/``, ``patterns/presale/``) provide the
scaffold + instrumentation half: the factory stamps the experiment, not just
tracks it.

Subcommands:
    add         register an assumption (XYZ falsifiability lint)
    card        pre-register a test card (threshold block hashed into the journal)
    verdict     record a result against the ORIGINAL journaled threshold hash
    rebaseline  the ONLY sanctioned threshold change (journaled, --reason)
    status      per-assumption effective evidence strength + cards
    check       ASM↔card traceability + every lint (the gate sweep)

Exit codes: 0 = ok / report-only findings, 1 = gate violation (--gate), 2 =
usage/config error, 4 = journal tamper detected (fail closed).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The ledger stays bet.py's object: portfolio resolution, journal chain,
# content hashing, and the report discipline are imported, not copied —
# the same placement precedent channel.py (#241) set.
import bet as betlib  # noqa: E402
from bet import BetError, TamperError  # noqa: E402

ASSUMPTIONS_NAME = "assumptions.json"
CARDS_NAME = "test-cards.json"

ASM_STATUSES = ("untested", "testing", "validated", "falsified")
ASM_SOURCES = ("premortem", "financial_model", "canvas")
CARD_VERDICTS = ("validated", "falsified", "inconclusive")

# Fixed evidence-strength enum (docs/business-factory.md §2.4) — the ladder is
# closed on purpose; there is no 6 and no reweighting.
STRENGTH_LABELS = {1: "opinion", 2: "click", 3: "time", 4: "reputation", 5: "money"}
BUILDING_FLOOR = 4

# Interview-class methods: opinion evidence no matter what strength the card
# declares — compliments and hypotheticals never graduate past 1 (Mom Test).
INTERVIEW_METHODS = {
    "interview", "interviews", "customer-interview", "user-interview",
    "mom-test-interview", "survey", "questionnaire", "focus-group",
}
INTERVIEW_CAP = 1

# ---- falsifiability (XYZ) lint --------------------------------------------------

XYZ_RE = re.compile(
    r"^\s*at\s+least\s+(\d+(?:\.\d+)?)\s*%\s+of\s+(.+?)\s+will\s+(.+?)\s*\.?\s*$",
    re.IGNORECASE,
)
# Populations that name everyone and therefore no one — Y must be concrete.
GENERIC_POPULATIONS = {
    "people", "users", "customers", "consumers", "visitors", "everyone",
    "anyone", "the market", "businesses", "companies", "the public",
}
# Opinion verbs in Z: unmeasurable by definition — a behavior is observable.
OPINION_Z_RE = re.compile(
    r"\b(like|love|enjoy|appreciate|want|prefer|think|feel|"
    r"be\s+interested|find\s+it\s+(useful|valuable)|say\s+they)\b",
    re.IGNORECASE,
)


def xyz_findings(statement: str, ref: str = "hypothesis") -> list[str]:
    """Savoia's XYZ grammar as a parser: 'at least X% of Y will Z'."""
    m = XYZ_RE.match(statement or "")
    if not m:
        return [
            f"{ref}: not in XYZ form — a falsifiable hypothesis reads "
            f"'at least X% of Y will Z' (X numeric, Y a concrete population, "
            f"Z a measurable behavior); got: {statement!r}"
        ]
    out = []
    y = m.group(2).strip().lower()
    if y in GENERIC_POPULATIONS or (y.startswith("the ") and y[4:] in GENERIC_POPULATIONS):
        out.append(
            f"{ref}: population {m.group(2).strip()!r} is not concrete — "
            "'everyone' is 'no one'; name the specific segment"
        )
    z = m.group(3).strip()
    if OPINION_Z_RE.search(z):
        out.append(
            f"{ref}: behavior {z!r} is an opinion, not a measurable behavior — "
            "opinions are strength-1 evidence at best; state what they will DO"
        )
    return out


# ---- vanity-metric lint ---------------------------------------------------------

VANITY_RE = re.compile(r"\b(total|cumulative|gross|all[_\s-]?time|lifetime|running)[_\s-]?", re.IGNORECASE)
RATE_RE = re.compile(r"(rate|pct|percent|share|ratio|conversion|per[_\s-])", re.IGNORECASE)


def vanity_metric_findings(metric: str, ref: str = "metric") -> list[str]:
    """Cumulative/gross counters falsify nothing (Ries): they only ever go up.
    A success criterion must be a per-cohort rate."""
    if VANITY_RE.search(metric or ""):
        return [
            f"{ref}: {metric!r} is a cumulative/gross counter — a vanity metric "
            "falsifies nothing; state a per-cohort rate (…_rate_pct, conversion, share)"
        ]
    if not RATE_RE.search(metric or ""):
        return [
            f"{ref}: {metric!r} does not read as a per-cohort rate — success "
            "criteria must be rates (rate/pct/share/ratio/conversion/per-*), "
            "not raw counters"
        ]
    return []


# ---- files ----------------------------------------------------------------------


def assumptions_path(root: Path, bet_id: str) -> Path:
    return betlib.bet_dir(root, bet_id) / ASSUMPTIONS_NAME


def cards_path(root: Path, bet_id: str) -> Path:
    return betlib.bet_dir(root, bet_id) / CARDS_NAME


def _load_list(path: Path, key: str) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise BetError(f"cannot parse {path}: {e}") from e
    items = data.get(key) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise BetError(f"{path}: expected {{'{key}': [...]}}")
    return [i for i in items if isinstance(i, dict)]


def load_assumptions(root: Path, bet_id: str) -> list[dict]:
    return _load_list(assumptions_path(root, bet_id), "assumptions")


def save_assumptions(root: Path, bet_id: str, assumptions: list[dict]) -> None:
    assumptions_path(root, bet_id).write_text(
        json.dumps({"assumptions": assumptions}, indent=2, sort_keys=True) + "\n"
    )


def load_cards(root: Path, bet_id: str) -> list[dict]:
    return _load_list(cards_path(root, bet_id), "cards")


def save_cards(root: Path, bet_id: str, cards: list[dict]) -> None:
    cards_path(root, bet_id).write_text(
        json.dumps({"cards": cards}, indent=2, sort_keys=True) + "\n"
    )


def _next_id(items: list[dict], key: str, prefix: str) -> str:
    ns = []
    for i in items:
        m = re.match(rf"^{prefix}-(\d+)$", str(i.get(key, "")))
        if m:
            ns.append(int(m.group(1)))
    return f"{prefix}-{(max(ns) + 1) if ns else 1:03d}"


def get_asm(assumptions: list[dict], asm_id: str) -> dict | None:
    return next((a for a in assumptions if a.get("id") == asm_id), None)


def get_card(cards: list[dict], card_id: str) -> dict | None:
    return next((c for c in cards if c.get("card_id") == card_id), None)


# ---- pre-registration hashing ---------------------------------------------------


def threshold_block(card: dict) -> dict:
    """The pre-registered success definition. Hashed at card creation; a
    verdict is only valid against this exact block. Numbers are normalized to
    float so 5 and 5.0 hash identically — reformatting is not a goalpost edit."""
    thr = card.get("threshold") or {}
    value = thr.get("value")
    sample_min = card.get("sample_min")
    return {
        "asm_id": card.get("asm_id"),
        "metric": card.get("metric"),
        "comparator": thr.get("comparator"),
        "value": float(value) if isinstance(value, (int, float)) else value,
        "sample_min": float(sample_min) if isinstance(sample_min, (int, float)) else sample_min,
    }


def threshold_baseline(records: list[dict], bet_id: str, card_id: str) -> str | None:
    """Last journaled threshold hash for this card (card-create / card-rebaseline)."""
    h = None
    for rec in betlib.bet_events(records, bet_id):
        d = rec.get("details", {}) or {}
        if d.get("card_id") != card_id:
            continue
        if rec.get("event") == "card-create":
            h = d.get("threshold_hash", h)
        elif rec.get("event") == "card-rebaseline":
            h = d.get("new_threshold_hash", h)
    return h


def preregistration_findings(records: list[dict], bet_id: str, card: dict) -> list[str]:
    cid = card.get("card_id", "?")
    baseline = threshold_baseline(records, bet_id, cid)
    if baseline is None:
        return [
            f"{cid}: no journaled pre-registration for this card — it was created "
            "outside `assumption.py card` (a success definition that was never "
            "pre-registered proves nothing)"
        ]
    if betlib.content_hash(threshold_block(card)) != baseline:
        return [
            f"{cid}: threshold block does not match the journaled pre-registration "
            "— post-hoc success definition (goalposts moved); `assumption.py "
            "rebaseline` is the only sanctioned change"
        ]
    return []


# ---- evidence strength ----------------------------------------------------------


def card_effective_strength(card: dict) -> tuple[int | None, str | None]:
    """(effective strength, cap note). Interview-class methods cap at 1
    regardless of the declared value — the Mom Test floor."""
    declared = card.get("evidence_strength")
    if not isinstance(declared, int) or not 1 <= declared <= 5:
        return None, None
    method = str(card.get("method", "")).strip().lower()
    if method in INTERVIEW_METHODS and declared > INTERVIEW_CAP:
        return INTERVIEW_CAP, (
            f"{card.get('card_id', '?')}: interview-class method {method!r} declares "
            f"strength {declared} ({STRENGTH_LABELS[declared]}) — capped at "
            f"{INTERVIEW_CAP} (opinion); interviews alone never exceed weak evidence, "
            "regardless of count"
        )
    return declared, None


def asm_effective_strength(asm_id: str, cards: list[dict]) -> tuple[int | None, list[str]]:
    """Max effective strength over this ASM's VALIDATED cards (None if none)."""
    best: int | None = None
    notes: list[str] = []
    for c in cards:
        if c.get("asm_id") != asm_id or c.get("verdict") != "validated":
            continue
        eff, note = card_effective_strength(c)
        if note:
            notes.append(note)
        if eff is not None and (best is None or eff > best):
            best = eff
    return best, notes


def building_floor_findings(root: Path, bet_id: str) -> list[str]:
    """Evidence-strength floor on `transition <id> building` (called by bet.py):
    ≥1 validated ASM at strength ≥4 (reputation/money) — Blank's validation
    exit is purchase orders, not enthusiasm. No assumptions.json → skipped
    (reported, never a silent block, never a crash)."""
    if not assumptions_path(root, bet_id).is_file():
        return [
            f"skipped: no {ASSUMPTIONS_NAME} for {bet_id} — evidence-strength "
            "floor not evaluated (register assumptions with `assumption.py add`)"
        ]
    assumptions = load_assumptions(root, bet_id)
    cards = load_cards(root, bet_id)
    out: list[str] = []
    best: int | None = None
    for a in assumptions:
        if a.get("status") != "validated":
            continue
        eff, notes = asm_effective_strength(a.get("id", "?"), cards)
        out += notes
        if eff is not None and (best is None or eff > best):
            best = eff
    if best is None or best < BUILDING_FLOOR:
        have = (
            f"strongest validated evidence is {best} ({STRENGTH_LABELS[best]})"
            if best is not None else "no validated assumption has card evidence"
        )
        out.append(
            f"evidence floor: transition to building requires ≥1 validated "
            f"assumption at strength ≥{BUILDING_FLOOR} "
            f"({STRENGTH_LABELS[4]}/{STRENGTH_LABELS[5]}) — {have}; "
            "purchase orders, not enthusiasm (Blank)"
        )
    return out


# ---- traceability + gate sweep --------------------------------------------------


def trace_findings(root: Path, bet_id: str, records: list[dict]) -> list[str]:
    """check_traceability.py's shape with new node types: every ASM ↔ ≥1 card;
    orphans, uncovered assumptions, dangling links, and evidence-less verdicts
    are findings."""
    assumptions = load_assumptions(root, bet_id)
    cards = load_cards(root, bet_id)
    out: list[str] = []
    asm_ids = set()
    for a in assumptions:
        aid = a.get("id", "?")
        asm_ids.add(aid)
        if a.get("status") not in ASM_STATUSES:
            out.append(f"{aid}: unknown status {a.get('status')!r} (want {'|'.join(ASM_STATUSES)})")
        if a.get("source") not in ASM_SOURCES:
            out.append(f"{aid}: unknown source {a.get('source')!r} (want {'|'.join(ASM_SOURCES)})")
        out += xyz_findings(a.get("statement", ""), ref=aid)

    covered = {c.get("asm_id") for c in cards}
    for a in assumptions:
        aid = a.get("id", "?")
        if aid not in covered:
            out.append(
                f"{aid}: uncovered — no test card (the DDP golden rule: every "
                "assumption gets ≥1 pre-registered test)"
            )
        if a.get("status") in ("validated", "falsified"):
            verdicts = {c.get("verdict") for c in cards if c.get("asm_id") == aid}
            if a["status"] not in verdicts:
                out.append(
                    f"{aid}: status {a['status']!r} with no card verdict to back it "
                    "— a verdict recorded outside `assumption.py verdict` "
                    "(evidence-less status is the omission evasion)"
                )

    seen_cards: set[str] = set()
    for c in cards:
        cid = c.get("card_id", "?")
        if cid in seen_cards:
            out.append(f"{cid}: duplicate card id")
        seen_cards.add(cid)
        if c.get("asm_id") not in asm_ids:
            out.append(f"{cid}: dangling — asm_id {c.get('asm_id')!r} resolves to no ledger assumption")
        out += vanity_metric_findings(c.get("metric", ""), ref=cid)
        strength = c.get("evidence_strength")
        if not isinstance(strength, int) or not 1 <= strength <= 5:
            out.append(f"{cid}: evidence_strength must be 1..5 (the enum is fixed), got {strength!r}")
        out += preregistration_findings(records, bet_id, c)
        if c.get("verdict") is not None:
            if c.get("verdict") not in CARD_VERDICTS:
                out.append(f"{cid}: unknown verdict {c.get('verdict')!r}")
            if c.get("result") is None:
                out.append(f"{cid}: verdict without a recorded result — nothing was measured")
        out += verdict_consistency_findings(c)
    return out


def verdict_consistency_findings(card: dict) -> list[str]:
    """A verdict must agree with the pre-registered comparator over the result."""
    thr = card.get("threshold") or {}
    result, comp, value = card.get("result"), thr.get("comparator"), thr.get("value")
    if card.get("verdict") not in ("validated", "falsified") or not isinstance(result, (int, float)) \
            or comp not in betlib.COMPARATORS or not isinstance(value, (int, float)):
        return []
    met = betlib._compare(result, comp, value)
    expected = "validated" if met else "falsified"
    if card["verdict"] != expected:
        return [
            f"{card.get('card_id', '?')}: verdict {card['verdict']!r} contradicts the "
            f"pre-registered threshold (result {result:g} {comp} {value:g} is "
            f"{str(met).lower()}) — the honest verdict is {expected!r}"
        ]
    return []


# ---- pivot re-open (Bland's dependency rule; called by bet.py) ------------------


def pivot_reopen(root: Path, old_id: str, new_id: str,
                 changed_elements: list[str]) -> dict | None:
    """Carry the assumption ledger to the pivot successor, re-opening
    (validated → untested) every ASM whose depends_on_element matches a
    changed element. Test cards do NOT carry: their thresholds were
    pre-registered against the old thesis, so coverage (and evidence strength)
    must be re-established on the successor — carried validation can never
    unlock `building` by itself. Returns a summary dict, or None when the old
    bet has no ledger."""
    if not assumptions_path(root, old_id).is_file():
        return None
    changed = {e.strip() for e in changed_elements if e and e.strip()}
    assumptions = load_assumptions(root, old_id)
    reopened: list[str] = []
    carried: list[dict] = []
    for a in assumptions:
        na = dict(a)
        if na.get("status") == "validated" and na.get("depends_on_element") in changed:
            na["status"] = "untested"
            reopened.append(na.get("id", "?"))
        carried.append(na)
    betlib.bet_dir(root, new_id).mkdir(parents=True, exist_ok=True)
    save_assumptions(root, new_id, carried)
    rec = betlib.append_event(root, "asm-reopen", new_id, {
        "pivot_from": old_id,
        "changed_elements": sorted(changed),
        "reopened": reopened,
        "carried": len(carried),
    })
    for a in carried:
        if a.get("id") in reopened:
            a["reopened_by"] = rec["record_id"]
    save_assumptions(root, new_id, carried)
    return {"reopened": reopened, "carried": len(carried), "record_id": rec["record_id"]}


# ---- subcommand plumbing --------------------------------------------------------


def _load(args) -> tuple[Path, dict]:
    root = betlib.portfolio_root(args.portfolio_dir)
    bet = betlib.load_bet(root, args.bet_id)
    if bet["state"] in betlib.TERMINALS:
        raise BetError(
            f"{args.bet_id} is terminal ({bet['state']}) — no further validation work"
        )
    betlib.load_journal(root)  # verified read first — never mutate atop a tampered chain
    return root, bet


def cmd_add(args) -> int:
    root, _bet = _load(args)
    if args.source not in ASM_SOURCES:
        raise BetError(f"--source must be one of {ASM_SOURCES}")
    assumptions = load_assumptions(root, args.bet_id)

    findings = xyz_findings(args.statement)
    rc = betlib.report(findings, args.gate, label="assumption")
    if rc:
        print(f"assumption: add on {args.bet_id} REFUSED (--gate)")
        return rc

    asm = {
        "id": _next_id(assumptions, "id", "ASM"),
        "statement": args.statement,
        "status": "untested",
        "source": args.source,
        "depends_on_element": args.element,
        "created": betlib.now_iso(),
    }
    assumptions.append(asm)
    save_assumptions(root, args.bet_id, assumptions)
    betlib.append_event(root, "asm-add", args.bet_id, {
        "asm_id": asm["id"], "source": args.source,
        "depends_on_element": args.element, "statement": args.statement,
    })
    print(f"assumption: {asm['id']} [untested, {args.source}] added to {args.bet_id}")
    return 0


def cmd_card(args) -> int:
    root, _bet = _load(args)
    assumptions = load_assumptions(root, args.bet_id)
    if get_asm(assumptions, args.asm) is None:
        raise BetError(
            f"no assumption {args.asm} on {args.bet_id} — a test card must test "
            "a ledger assumption (`assumption.py add` first)"
        )
    if not 1 <= args.evidence_strength <= 5:
        raise BetError(
            "evidence_strength must be 1..5 — the enum is fixed: "
            + ", ".join(f"{k} {v}" for k, v in STRENGTH_LABELS.items())
        )
    if args.comparator not in betlib.COMPARATORS:
        raise BetError(f"--comparator must be one of {sorted(betlib.COMPARATORS)}")

    cards = load_cards(root, args.bet_id)
    card = {
        "card_id": _next_id(cards, "card_id", "TC"),
        "asm_id": args.asm,
        "method": args.method,
        "metric": args.metric,
        "threshold": {"comparator": args.comparator, "value": args.value},
        "sample_min": args.sample_min,
        "cost_estimate_usd": args.cost_estimate_usd,
        "evidence_strength": args.evidence_strength,
        "result": None,
        "sample_n": None,
        "verdict": None,
        "created": betlib.now_iso(),
        "verdict_ts": None,
    }

    findings = vanity_metric_findings(args.metric)
    _eff, note = card_effective_strength(card)
    if note:
        findings.append(note)
    rc = betlib.report(findings, args.gate, label="assumption")
    if rc:
        print(f"assumption: card on {args.bet_id} REFUSED (--gate)")
        return rc

    cards.append(card)
    save_cards(root, args.bet_id, cards)
    asm = get_asm(assumptions, args.asm)
    if asm.get("status") == "untested":
        asm["status"] = "testing"
        save_assumptions(root, args.bet_id, assumptions)
    h = betlib.content_hash(threshold_block(card))
    betlib.append_event(root, "card-create", args.bet_id, {
        "card_id": card["card_id"], "asm_id": args.asm, "method": args.method,
        "threshold_hash": h, "evidence_strength": args.evidence_strength,
        "cost_estimate_usd": args.cost_estimate_usd,
    })
    print(
        f"assumption: {card['card_id']} pre-registered for {args.asm} on "
        f"{args.bet_id} — threshold block hashed into the journal "
        f"({args.metric} {args.comparator} {args.value:g}, n≥{args.sample_min}, "
        f"strength {args.evidence_strength} {STRENGTH_LABELS[args.evidence_strength]})"
    )
    return 0


def cmd_verdict(args) -> int:
    root, _bet = _load(args)
    cards = load_cards(root, args.bet_id)
    card = get_card(cards, args.card_id)
    if card is None:
        raise BetError(
            f"no card {args.card_id} on {args.bet_id} — a verdict requires a "
            "pre-registered test card (no card, no verdict)"
        )
    if card.get("verdict") is not None:
        raise BetError(
            f"{args.card_id} already has a verdict ({card['verdict']}) — a card "
            "is decided once; run a new card for a re-test"
        )
    if args.verdict not in CARD_VERDICTS:
        raise BetError(f"--verdict must be one of {CARD_VERDICTS}")

    records = betlib.load_journal(root)
    findings = preregistration_findings(records, args.bet_id, card)
    trial = {**card, "result": args.result, "verdict": args.verdict}
    findings += verdict_consistency_findings(trial)
    if args.sample_n is not None and isinstance(card.get("sample_min"), int) \
            and args.sample_n < card["sample_min"]:
        findings.append(
            f"{args.card_id}: under-powered — sample n={args.sample_n} below the "
            f"pre-registered sample_min {card['sample_min']}"
        )
    rc = betlib.report(findings, args.gate, label="assumption")
    if rc:
        print(f"assumption: verdict on {args.card_id} ({args.bet_id}) REFUSED (--gate)")
        return rc

    card["result"] = args.result
    card["sample_n"] = args.sample_n
    card["verdict"] = args.verdict
    card["verdict_ts"] = betlib.now_iso()
    save_cards(root, args.bet_id, cards)

    assumptions = load_assumptions(root, args.bet_id)
    asm = get_asm(assumptions, card.get("asm_id", ""))
    asm_status = None
    if asm is not None:
        asm_status = {"validated": "validated", "falsified": "falsified",
                      "inconclusive": "testing"}[args.verdict]
        asm["status"] = asm_status
        save_assumptions(root, args.bet_id, assumptions)

    betlib.append_event(root, "card-verdict", args.bet_id, {
        "card_id": args.card_id, "asm_id": card.get("asm_id"),
        "result": args.result, "sample_n": args.sample_n,
        "verdict": args.verdict, "asm_status": asm_status,
        "threshold_hash": threshold_baseline(records, args.bet_id, args.card_id),
    })
    eff, _ = card_effective_strength(card)
    print(
        f"assumption: {args.card_id} verdict {args.verdict} (result "
        f"{args.result:g}) — {card.get('asm_id')} now {asm_status or 'unlinked'}; "
        f"effective evidence strength {eff} "
        f"({STRENGTH_LABELS.get(eff, '?')})"
    )
    return 0


def cmd_rebaseline(args) -> int:
    root, _bet = _load(args)
    cards = load_cards(root, args.bet_id)
    card = get_card(cards, args.card_id)
    if card is None:
        raise BetError(f"no card {args.card_id} on {args.bet_id}")
    if card.get("verdict") is not None:
        raise BetError(
            f"{args.card_id} already has a verdict — a decided card's threshold "
            "is frozen (run a new card instead)"
        )
    if args.metric is None and args.comparator is None and args.value is None \
            and args.sample_min is None:
        raise BetError("nothing to rebaseline — pass --metric/--comparator/--value/--sample-min")
    if args.comparator is not None and args.comparator not in betlib.COMPARATORS:
        raise BetError(f"--comparator must be one of {sorted(betlib.COMPARATORS)}")

    records = betlib.load_journal(root)
    old_hash = threshold_baseline(records, args.bet_id, args.card_id)
    if args.metric is not None:
        card["metric"] = args.metric
    if args.comparator is not None:
        card["threshold"]["comparator"] = args.comparator
    if args.value is not None:
        card["threshold"]["value"] = args.value
    if args.sample_min is not None:
        card["sample_min"] = args.sample_min

    findings = vanity_metric_findings(card.get("metric", ""))
    rc = betlib.report(findings, args.gate, label="assumption")
    if rc:
        print(f"assumption: rebaseline {args.card_id} REFUSED (--gate)")
        return rc

    save_cards(root, args.bet_id, cards)
    new_hash = betlib.content_hash(threshold_block(card))
    rec = betlib.append_event(root, "card-rebaseline", args.bet_id, {
        "card_id": args.card_id, "reason": args.reason,
        "old_threshold_hash": old_hash, "new_threshold_hash": new_hash,
    })
    print(
        f"assumption: rebaselined {args.card_id} on {args.bet_id} "
        f"({rec['record_id']}) — old→new threshold hashes journaled; "
        f"reason: {args.reason}"
    )
    return 0


def cmd_status(args) -> int:
    root = betlib.portfolio_root(args.portfolio_dir)
    betlib.load_bet(root, args.bet_id)  # status works on terminal bets too
    assumptions = load_assumptions(root, args.bet_id)
    cards = load_cards(root, args.bet_id)
    print(f"assumption: {args.bet_id} — {len(assumptions)} assumption(s), {len(cards)} card(s)")
    for a in assumptions:
        aid = a.get("id", "?")
        eff, _notes = asm_effective_strength(aid, cards)
        strength = f"strength {eff} ({STRENGTH_LABELS.get(eff, '?')})" if eff else "no validated evidence"
        elem = f", element {a['depends_on_element']}" if a.get("depends_on_element") else ""
        print(f"  {aid} [{a.get('status')}, {a.get('source')}{elem}] {strength}")
        print(f"      {a.get('statement', '')}")
        for c in cards:
            if c.get("asm_id") != aid:
                continue
            thr = c.get("threshold") or {}
            verdict = c.get("verdict") or "pending"
            result = f", result {c['result']:g}" if isinstance(c.get("result"), (int, float)) else ""
            print(
                f"      {c.get('card_id')}: {c.get('method')} — {c.get('metric')} "
                f"{thr.get('comparator')} {thr.get('value')}, n≥{c.get('sample_min')}, "
                f"declared strength {c.get('evidence_strength')} → {verdict}{result}"
            )
    return 0


def cmd_check(args) -> int:
    root = betlib.portfolio_root(args.portfolio_dir)
    betlib.load_bet(root, args.bet_id)
    records = betlib.load_journal(root)
    if not assumptions_path(root, args.bet_id).is_file() \
            and not cards_path(root, args.bet_id).is_file():
        findings = [
            f"skipped: no {ASSUMPTIONS_NAME}/{CARDS_NAME} for {args.bet_id} — "
            "nothing to check"
        ]
    else:
        findings = trace_findings(root, args.bet_id, records)
    if not findings:
        print(f"assumption: check {args.bet_id} OK — every ASM ↔ ≥1 card, "
              "pre-registrations intact")
    return betlib.report(findings, args.gate, label="assumption")


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
            help="exit 1 on findings (report-only by default — docs/gate-rollout.md)",
        )

    sp = sub.add_parser("add", help="register an assumption (XYZ falsifiability lint)")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("--statement", required=True,
                    help="the hypothesis in XYZ form: 'at least X%% of Y will Z'")
    sp.add_argument("--source", required=True, choices=ASM_SOURCES,
                    help="where the assumption was mined")
    sp.add_argument("--element", default=None, metavar="CANVAS_ELEMENT",
                    help="canvas element this assumption depends on (pivot dependency "
                         "rule: a pivot changing it re-opens the assumption)")

    sp = sub.add_parser("card", help="pre-register a test card (threshold hashed into the journal)")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("--asm", required=True, metavar="ASM-NNN")
    sp.add_argument("--method", required=True,
                    help="how the test runs (landing-page-smoke-test, presale, interview, …); "
                         "interview-class methods cap effective strength at 1")
    sp.add_argument("--metric", required=True,
                    help="per-cohort rate to measure (vanity counters are rejected)")
    sp.add_argument("--comparator", required=True)
    sp.add_argument("--value", type=float, required=True, help="threshold value")
    sp.add_argument("--sample-min", type=int, required=True,
                    help="minimum sample size for a powered verdict")
    sp.add_argument("--cost-estimate-usd", type=float, default=None)
    sp.add_argument("--evidence-strength", type=int, required=True,
                    help="fixed enum: 1 opinion, 2 click, 3 time, 4 reputation, 5 money")

    sp = sub.add_parser("verdict", help="record a result against the ORIGINAL journaled threshold")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("card_id", metavar="TC-NNN")
    sp.add_argument("--result", type=float, required=True,
                    help="measured value of the card's metric")
    sp.add_argument("--verdict", required=True, choices=CARD_VERDICTS)
    sp.add_argument("--sample-n", type=int, default=None,
                    help="actual sample size (below sample_min → under-powered finding)")

    sp = sub.add_parser("rebaseline",
                        help="the ONLY sanctioned threshold change (journaled, --reason)")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("card_id", metavar="TC-NNN")
    sp.add_argument("--metric", default=None)
    sp.add_argument("--comparator", default=None)
    sp.add_argument("--value", type=float, default=None)
    sp.add_argument("--sample-min", type=int, default=None)
    sp.add_argument("--reason", required=True,
                    help="why the success definition moves — journaled with old→new hashes")

    sp = sub.add_parser("status", help="per-assumption effective evidence strength + cards")
    common(sp)
    sp.add_argument("bet_id")

    sp = sub.add_parser("check", help="ASM↔card traceability + every lint (the gate sweep)")
    common(sp)
    sp.add_argument("bet_id")

    args = p.parse_args()
    dispatch = {
        "add": cmd_add, "card": cmd_card, "verdict": cmd_verdict,
        "rebaseline": cmd_rebaseline, "status": cmd_status, "check": cmd_check,
    }
    try:
        return dispatch[args.cmd](args)
    except BetError as e:
        sys.stderr.write(f"assumption: {e}\n")
        return 2
    except TamperError as e:
        sys.stderr.write(f"assumption: {e}\n")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
