#!/usr/bin/env python3
"""Bet ledger — the business-factory portfolio spine (chief-wiggum#235).

A **bet** is a bounded venture experiment: under ~$1k to prove, ~3 months to a
kill decision. The empirical warrant (Staw 1976; Boulding et al. 1997): founders
escalate on failing bets and self-enforced kill rules do NOT stop them — only
**binding predetermined rules** do. CW already owns the enforcement idioms
(ratchet hash chain, goalpost hashing, gate scripts); this script applies them
to the operator's own future self. See docs/business-factory.md §1–2, §8.

State lives in a dedicated private portfolio repo (default
``~/.chief-wiggum/portfolio``; override with ``--portfolio-dir`` or the
``CHIEF_WIGGUM_PORTFOLIO`` env var), git-initialized on first use::

    <portfolio>/
    ├── journal.jsonl          # append-only hash chain (ratchet.py format — never hand-edit)
    ├── means.json             # means inventory (templates/means-schema.json), optional
    └── bets/<bet-id>/
        ├── bet.json           # templates/bet-schema.json (envelope embedded — a goalpost)
        ├── kill-criteria.json # templates/kill-criteria-schema.json (a goalpost)
        ├── ledger.jsonl       # append-only spend/time/rep entries
        ├── channels.json      # optional channel-experiment records (#241)
        ├── assumptions.json   # optional assumption ledger (#236, assumption.py)
        ├── test-cards.json    # optional pre-registered test cards (#236)
        └── retrospective.md   # required non-trivially before `killed`

The journal REUSES ``ratchet.py``'s hash-chain format via ``ratchet.load_journal``
and ``chief_wiggum.hashing.stable_hash`` (imported, not copied): each record's
hash covers its body plus the previous record's hash; every read verifies the
chain from genesis and FAILS CLOSED (exit 4) on a break. The envelope and kill
criteria are **goalpost artifacts**: content-hashed into the journal at create;
``rebaseline`` is the only sanctioned mutation path and journals old hash → new
hash with a required ``--reason`` (Adner & Levinthal 2004 — the abandonment
condition must not be retroactively redefined).

Bet state machine (docs/business-factory.md §2.3)::

    proposed → probing → validating → building → scaling
    kill_pending reachable from any non-terminal state
    terminals: killed | parked | lifestyle | sold | wound_down

``lifestyle`` is the LIVE terminal — a revenue-producing, support-consuming
product that earns zero in-flight slots forever (#274, §9.6.3/9.6.5's
zombie-fleet gap). ``wound_down`` (#274) is a distinct terminal for a
PLANNED graceful hold-then-shutdown, never conflated with ``killed``'s
harvest-check discipline.

Verdicts at gates use Cooper's vocabulary — ``go | kill | hold | recycle`` —
never binary pass/fail. **Pivot is a transition, not an edit**: it closes the
bet (criteria evaluated against the old thesis) and opens a successor bet with
fresh criteria + envelope. ``killed`` is blocked until ``retrospective.md``
exists non-trivially AND a harvest check ran: if est sale value (3.9 × TTM
seller-discretionary profit — Acquire.com micro-SaaS median) exceeds wind-down
cost, the proposed terminal is ``sold``, not ``killed``; absent inputs → the
check reports ``skipped``, never blocks silently, never crashes.

Gate checks (ALL report-only by default per docs/gate-rollout.md — findings
printed, exit 0; blocking only with ``--gate``; ships without any workflow
passing ``--gate`` until a ``validation/bet-gates.json`` record exists):

- **states-and-dates soundness lint** (create/rebaseline): a criterion missing
  the measurable state (metric/comparator/threshold) OR the date is malformed.
- **tranche gate** (spend): cumulative spend ≤ cumulative unlocked tranches.
- **dated-criterion evaluation** (evaluate): a triggered criterion journals a
  ``kill-proposed`` event; further spend entries are findings pending the human
  accept (``transition <id> kill_pending``) or override
  (``transition <id> --override-kill --reason ...``) — both journaled acts.
- **bets-in-flight cap** (transition/portfolio): bets in probing|validating|
  building, default 2 (``--max-in-flight``) — attention is the binding resource.
- **capacity-based cap, addition rule, attention kill criterion** (#274,
  §9.6.3 — transition/portfolio): the count-based cap above cannot see a
  ``lifestyle`` bet, which consumes zero in-flight slots while consuming
  operator hours forever (the zombie-fleet gap). Remaining capacity =
  ``means.hours_per_week − Σ(measured ongoing_load of every lifestyle bet) −
  reserve_hours_per_week`` — a SECOND, independent bound alongside
  ``--max-in-flight``; whichever binds is visible. ``ongoing_load`` is always
  MEASURED from the ledger's trailing hours (never a typed-in guess). The
  addition rule flags starting a new bet before the most recently added live
  product has run below its target load for two consecutive weekly periods.
  The attention kill criterion flags a live bet costing more than 2h/wk while
  earning (operator-entered) MRR under $2k. All three are brand-new
  reinterpretations of the existing cap and so NEVER gate, even under
  ``--gate``, until validated against a real portfolio (docs/gate-rollout.md).
- **bet-selection lint** (create, #235 amendment): while means.json says
  sales AND marketing are novice and no channel across the portfolio has
  reached ``focused``, a bet whose acquisition plan has no ecosystem channel
  and no owned audience is flagged. means.json absent → ``skipped``.
- **goalpost integrity** (every read): envelope/criteria content hash vs the
  last journaled baseline — a hand edit outside ``rebaseline`` is flagged.
- **rep cadence** (evaluate, #241): while probing|validating, ≥N Mom-Test
  conversations in the trailing week counted from the ledger's rep entries
  (default 3; per-bet ``create --cadence``, journaled). Missed cadence feeds
  the kill review as distribution-not-attempted evidence.
- ***Traction* 50% rule** (evaluate, #241): hours may carry a
  ``--tag product|traction``; traction share below 0.5 while
  probing|validating is a finding — untagged hours never are (a finding must
  come from data, not its absence).

- **evidence-strength floor** (transition → building, #236): ≥1 validated
  assumption at strength ≥4 (reputation/money — Blank: purchase orders, not
  enthusiasm), computed by ``assumption.py``. No assumptions.json →
  ``skipped``. A pivot's ``--changed-elements`` re-opens (validated →
  untested) dependent assumptions in the successor (Bland's dependency rule).
- **liability-exposure soundness lint** (create/rebaseline, #277):
  ``envelope.liability_exposure`` (docs/business-factory.md §2.1 addendum) is
  enumerated — ``capped_at``/``insured``/``uncapped_entity``/
  ``uncapped_personal``. Its total ABSENCE is a finding; a STATED value —
  including an uncapped one — is NEVER itself a finding anywhere in this
  script: the operator's risk appetite is a deliberate, sized, counted choice,
  not a defect. ``insured.responds`` defaults to ``unverified`` (insurability
  is a separate fact from insurance — a policy may not respond to a
  contractually-assumed liability) and only a human answer moves it.
- **portfolio-level uncapped-liability concurrency count** (portfolio, #277):
  always visible, never a finding/never gates — one uncapped exposure is a
  considered bet, several concurrently is a portfolio that cannot survive one
  bad event.
- **low-cap distribution-divergence screens** (create, #275, §9.6.5 screens
  8-13): six candidates from the low-cap distribution-divergence sweep (#272)
  — enumerable buyers (500-5,000 named prospects), support-obligation hazard
  (reject real-time/critical-path/regulatory-deadline functionality),
  structural retention (>=5y record retention or a weekly-recurring
  workflow), channel existence (>=1 of a cheap newsletter/marketplace/small
  trade show/large member group), dark-matter demand (a keyword cluster with
  nonzero CPC, or >=3 "what software do you use for X" threads), and the
  opportunity-cost benchmark (projected $/hr vs the operator's contracting
  rate — means.json ``contracting_rate_usd_per_hour``). ALL brand-new
  reinterpretations and so NEVER gate, even under ``--gate``
  (``NEVER_GATES_PREFIXES``'s ``"screen:"`` entry), until validated against a
  real candidate set. An absent screen input is UNRESOLVED (cannot run),
  never a silent pass — three of the six (buyers, channels, demand) need
  genuinely external data CW cannot produce itself.

Channel-experiment records themselves (Bullseye states, completeness,
exactly-one-focused, CAC join, headcount filter) are ``scripts/channel.py``'s
job — it imports this module's helpers and journals into the same chain; the
assumption ledger + pre-registered test cards (#236) are
``scripts/assumption.py``'s job in the same importing-sibling shape.

Missing OPTIONAL inputs never crash and are never silently omitted: no
means.json → the selection lint reports ``skipped``; no channels.json and no
rep ledger entries → ``evaluate`` reports distribution ``unattempted`` (feeds
the #237 verdict rule: no-demand evidence without attempted distribution is
evidence of no marketing, not no demand).

Two hard guards are state-machine integrity, not precision-risk gates, and
block regardless of ``--gate``: an invalid state transition (exit 2), and
``killed`` without a non-trivial retrospective (exit 1 — harvest discipline,
``/close-epic``'s shape).

Kill-review quorum (#237, docs/business-factory.md §2.4): at a kill checkpoint
the continue case is argued to a FRESH-CONTEXT quorum (``consult_ai.py --role
kill-review`` — codex + opus required, claude-interactive optional, bounded
charters per the lenses-not-personas convention) that sees only a GENERATED
brief: hashed kill criteria verbatim, measured values with journal sources,
envelope status, open-assumption evidence, material findings (#252), and the
distribution-attempt table. The evaluator having no sunk context is the feature
(Boulding et al. 1997) — ``kill-brief`` renders only journal-backed artifacts
and REFUSES (exit 1, a hard self-check, not a ``--gate``) if the brief would
carry an unsourced value or thesis prose. Verdict schema: ``{verdict:
go|kill|hold|recycle, confidence, reasons[], cheapest_disconfirming_test?}``.
**Distribution-fairness rule** (#241 amendment): a demand-shaped criterion
(direction=has, or an explicit ``demand_shaped`` flag) that fired while
distribution is unattempted cannot ground a ``kill`` — a parsed kill verdict is
mechanically downgraded to ``recycle`` with a finding naming the cheapest
untried exposure; this holds under EITHER convening trigger below, no
exceptions. The human sees the quorum verdicts BEFORE the accept/override
instructions (the fresh verdict anchors the decision), and the verdicts +
brief hash are journaled as a ``kill-review`` event. Nothing convenes the
quorum automatically: a fired criterion RECOMMENDS ``kill-review``; the human
runs it.

**Two legitimate convening triggers** (#252): a dated criterion firing
(``criterion``, the original shape above), or a **material premise change** — a
journaled ``finding`` (``bet.py finding``) bearing on the bet's premise, e.g. a
competitor twin discovered post-creation. ``kill-brief``/``kill-review`` accept
an explicit ``--trigger`` and otherwise auto-detect one from the journal; the
brief states which trigger convened it up front, so evaluators judge the right
question — under a premise-change trigger, "insufficient evidence gathered
yet" is NOT by itself grounds for ``hold``; the question is whether the
premise still supports continuing to spend. A ``finding`` is refused (exit 2)
without a non-empty ``--source-url`` — an external fact with no citable source
cannot enter a brief that must stay journal-backed.

Subcommands:
    create      register a bet: envelope + kill criteria hashed into the journal
    spend       append a ledger entry (spend/time or a distribution rep)
    evaluate    evaluate dated criteria + report distribution-attempted status
    finding     record a material external finding bearing on premise/assumption/
                criterion (#252) — journaled, cited in kill-brief, refused without
                a --source-url
    kill-brief  render the fresh-context kill brief (journal-backed values only)
    kill-review generate the brief, run the kill-review quorum, journal verdicts
    transition  move the state machine (incl. pivot, kill accept/override,
                tranche unlock — every one a journaled act)
    rebaseline  the ONLY mutation path for envelope/kill criteria (--reason required)
    portfolio   per-bet + portfolio summary: spend vs envelope, days to next
                dated criterion, in-flight cap, loss distribution + kill hygiene
                per dead bet — never per-bet win/lose ranking

Exit codes: 0 = ok / report-only findings, 1 = gate violation (--gate) or hard
guard, 2 = usage/config error, 4 = journal tamper detected (fail closed).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Chain-format reuse, not a fork: ratchet.py stays the format's single home
# (its source is frozen by its live gate-validation record — see
# docs/quality/validation/ratchet.json), so bet.py imports its verified reader
# and the shared stable_hash primitive instead of refactoring it.
import ratchet  # noqa: E402
from chief_wiggum.hashing import stable_hash  # noqa: E402
from ratchet import TamperError  # noqa: E402  (maps to exit 4 — fail closed)

JOURNAL_NAME = "journal.jsonl"
MEANS_NAME = "means.json"

ACTIVE_ORDER = ["proposed", "probing", "validating", "building", "scaling"]
IN_FLIGHT = {"probing", "validating", "building"}
# `wound_down` (#274, §9.6.5): a planned, graceful hold-then-shutdown (or
# handing the product to a loyal customer) is the normal ending for a small
# vertical vendor — distinct from `killed`'s harvest-check discipline, which
# assumes the ending is a loss to grade, not a deliberate wind-down.
TERMINALS = {"killed", "parked", "lifestyle", "sold", "wound_down"}
ALL_STATES = set(ACTIVE_ORDER) | {"kill_pending"} | TERMINALS
VERDICTS = ("go", "kill", "hold", "recycle")
COMPARATORS = {"<", "<=", ">", ">=", "==", "!="}
DEFAULT_MAX_IN_FLIGHT = 2

# Findings prefixed with any of these never gate — printed always, tagged
# report-only even under --gate. "skipped:" is a missing OPTIONAL input
# (pre-existing convention). "capacity:" is the #274 zombie-fleet arithmetic
# (capacity-based cap, addition rule, attention kill criterion): brand-new
# checks that reinterpret what the existing --max-in-flight cap reports, so
# per docs/gate-rollout.md they ship report-only until validated against a
# real portfolio — they must never harden into a blocker on their own.
# "screen:" is the #275 low-cap distribution-divergence screens (§9.6.5
# screens 8-13) — same posture: brand-new bet-selection lints, never gated
# until validated against a real candidate set.
# "buildcost:" (#257) is the same shape: plan-share accounting is inherently
# estimate-shaped (the harness rarely exposes true plan consumption), so the
# envelope's nominal_build_cap_usd/plan_share_cap_pct checks are report-only
# until validated, never a blocker on their own.
NEVER_GATES_PREFIXES = ("skipped:", "capacity:", "screen:", "buildcost:")

# Low-cap distribution-divergence screens (#275, docs/business-factory.md
# §9.6.5 screens 8-13) — six candidate additions to the §9.5 standing
# screens, mined from the same distribution-divergence sweep as §9.6 (#272).
ENUMERABLE_BUYERS_MIN = 500
ENUMERABLE_BUYERS_MAX = 5000
CHANNEL_NEWSLETTER_MAX_COST_USD = 500
CHANNEL_TRADE_SHOW_MAX_ATTENDEES = 5000
CHANNEL_MEMBER_GROUP_MIN_MEMBERS = 2000
DARK_MATTER_MIN_CLUSTER = 10
DARK_MATTER_MAX_CLUSTER = 20
DARK_MATTER_MIN_SEARCHES = 10
DARK_MATTER_MAX_SEARCHES = 300
DARK_MATTER_MIN_THREADS = 3
STRUCTURAL_RETENTION_MIN_YEARS = 5
# Standard month-average weeks/month (52/12), used to turn a weekly hours
# projection into a monthly one for screen 13's hourly-rate comparison.
WEEKS_PER_MONTH = 4.345

# Live states whose products consume operator attention indefinitely despite
# being a TERMINAL — the zombie-fleet gap (#274, docs/business-factory.md
# §9.6.3/§9.6.5): `lifestyle` is a live, revenue-producing, support-consuming
# product that earns zero in-flight slots forever. This is the set the
# capacity accounting, addition rule and attention kill criterion all read.
LIVE_STATES = {"lifestyle"}

# Ongoing load is MEASURED from the ledger's trailing hours entries, never a
# typed-in guess (#274 item 1) — averaged over this many trailing weeks.
LOAD_WINDOW_WEEKS = 4

# §9.6.3 proposed thresholds: the attention kill criterion (#274 item 4) flags
# a live product whose measured steady-state load is above
# ATTENTION_LOAD_THRESHOLD_HOURS while its (operator-entered) MRR is below
# ATTENTION_REVENUE_THRESHOLD_USD — attention spent without being paid for.
# The same load threshold doubles as the addition rule's (#274 item 3)
# default "target load" a live product must run under for two consecutive
# weekly periods before another bet may start, absent a per-bet override.
ATTENTION_LOAD_THRESHOLD_HOURS = 2.0
ATTENTION_REVENUE_THRESHOLD_USD = 2000.0

# Liability-exposure enumeration (#277) — Dew et al.'s 2009 affordable-loss
# field list omits it, so an uncapped indemnity records identically to no
# exposure at all. Enumerated, never free text (docs/business-factory.md §2.1
# addendum). A STATED value — including uncapped_entity/uncapped_personal —
# is NEVER itself a finding: the operator's risk appetite is a deliberate,
# sized, counted choice, not a defect. Only its total ABSENCE is a finding.
LIABILITY_TYPES = {"capped_at", "insured", "uncapped_entity", "uncapped_personal"}
# Insurability is a separate fact from insurance (#277 decision 2): a policy
# does not establish that it responds to a contractually-assumed liability (a
# common PI exclusion) — `responds` defaults to `unverified` and only a human
# answer moves it.
RESPONDS_VALUES = {"yes", "unverified", "no"}
UNCAPPED_LIABILITY_TYPES = {"uncapped_entity", "uncapped_personal"}
# A bet's contractual liability position is considered EXITED (no longer
# counted in the portfolio-level concurrency count) only once it is killed,
# sold (liability moves with the buyer/new entity) or wound down. Every other
# state — including `parked` — may still carry an unresolved, live exposure.
LIABILITY_EXITED_STATES = {"killed", "sold", "wound_down"}

# Channel-engine ledger checks (#241 leg 3). While a bet is probing|validating,
# ≥N Mom-Test conversations/week (default 3, per-bet `create --cadence`,
# journaled) and — *Traction* 50% rule — traction-tagged hours ≥ half of all
# tagged hours. Both report-only; the doing-gap must be legible, not blocked.
DEFAULT_REP_CADENCE = 3
CADENCE_STATES = {"probing", "validating"}
TRACTION_SHARE_MIN = 0.5
LEDGER_TAGS = ("product", "traction")

# A channel record counts as ATTEMPTED distribution only once it reaches the
# Bullseye inner rings — brainstormed/ranked is consideration, not an attempt.
EXPERIMENT_STATES = {"testing", "focused", "rejected"}

# Kill-review quorum (#237): the consult role + on-disk names for the generated
# brief and the per-provider verdict files consult_ai.py --role writes.
KILL_REVIEW_ROLE = "kill-review"
BRIEF_NAME = "kill-brief.md"
KILL_REVIEW_DIR = "kill-review"

# Exposure-bearing numeric fields a channel-experiment record may carry — the
# brief's "exposure delivered" column reads only these; absent → UNRESOLVED.
EXPOSURE_KEYS = ("exposure", "traffic", "visitors", "impressions", "listings", "launches")

# Material findings (#252): an external, citable fact bearing on the bet — distinct
# from an assumption ledger entry (a claim under test) and a measurement (a
# criterion's value). `bearing_on` names what it bears on: the bet's premise as a
# whole, a specific assumption, or a specific kill criterion.
FINDING_BEARING_ON_RE = re.compile(r"^(premise|ASM-[A-Za-z0-9_-]+|KC-[A-Za-z0-9_-]+)$")
EVIDENCE_GRADES = ("verified", "reported")
KILL_REVIEW_TRIGGERS = ("criterion", "premise-change")

# Brief-purity lint: every [source: ...] citation must be a journal record id
# or an artifact file inside this bet's directory — nothing else exists to a
# fresh-context evaluator.
_SOURCE_RE = re.compile(r"\[source: ([^\]]+)\]")
_REC_ID_RE = re.compile(r"^rec-\d{5,}$")
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# Acquire.com micro-SaaS median multiple on TTM seller-discretionary earnings
# (docs/business-factory.md §2.3) — the harvest check's est-sale-value factor.
HARVEST_MULTIPLE = 3.9

# retrospective.md is "non-trivial" when, with headings stripped, at least this
# much prose remains — an empty file or a bare title must not unlock `killed`.
RETRO_MIN_CHARS = 40


class BetError(Exception):
    """Usage/config problem. Maps to exit 2."""


# ---- portfolio repo ------------------------------------------------------------


def portfolio_root(arg: str | None) -> Path:
    """Resolve the portfolio repo (flag > env > default) and git-init on first use."""
    if arg:
        root = Path(arg).expanduser()
    elif os.environ.get("CHIEF_WIGGUM_PORTFOLIO"):
        root = Path(os.environ["CHIEF_WIGGUM_PORTFOLIO"]).expanduser()
    else:
        root = Path.home() / ".chief-wiggum" / "portfolio"
    root = root.resolve()
    (root / "bets").mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        proc = subprocess.run(
            ["git", "init", "-q"], cwd=root, capture_output=True, text=True
        )
        if proc.returncode != 0:
            sys.stderr.write(f"bet: warning — git init failed: {proc.stderr.strip()}\n")
    return root


def bet_dir(root: Path, bet_id: str) -> Path:
    return root / "bets" / bet_id


def load_bet(root: Path, bet_id: str) -> dict:
    path = bet_dir(root, bet_id) / "bet.json"
    if not path.is_file():
        raise BetError(f"no such bet: {bet_id} (no {path})")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise BetError(f"cannot parse {path}: {e}") from e


def save_bet(root: Path, bet: dict) -> None:
    path = bet_dir(root, bet["id"]) / "bet.json"
    path.write_text(json.dumps(bet, indent=2, sort_keys=True) + "\n")


def all_bets(root: Path) -> list[dict]:
    out = []
    for p in sorted((root / "bets").glob("*/bet.json")):
        try:
            out.append(json.loads(p.read_text()))
        except json.JSONDecodeError:
            sys.stderr.write(f"bet: warning — cannot parse {p}, skipping\n")
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- hash-chained journal (ratchet.py format, reused) --------------------------


def load_journal(root: Path) -> list[dict]:
    """Verified read of the portfolio journal — ratchet.py's chain reader reused
    via a minimal shim (``load_journal`` only touches ``cfg.journal``). Raises
    ``TamperError`` (exit 4) on a broken chain — fail closed."""
    return ratchet.load_journal(SimpleNamespace(journal=root / JOURNAL_NAME))


def append_event(root: Path, event: str, ref: str, details: dict) -> dict:
    """Append a hash-chained record (same body+prev-hash scheme as ratchet.py).
    The verified read first means a tampered journal is never silently extended."""
    records = load_journal(root)
    body = {
        "record_id": f"rec-{len(records) + 1:05d}",
        "event": event,
        "ref": ref,
        "ts": now_iso(),
        "details": details,
    }
    prev = records[-1]["record_hash"] if records else "genesis"
    body["record_hash"] = stable_hash(prev, json.dumps(body, sort_keys=True))
    with (root / JOURNAL_NAME).open("a") as f:
        f.write(json.dumps(body, sort_keys=True) + "\n")
    return body


def bet_events(records: list[dict], bet_id: str) -> list[dict]:
    return [r for r in records if r.get("ref") == bet_id]


def load_findings(records: list[dict], bet_id: str) -> list[dict]:
    """Every journaled `finding` event for this bet (chief-wiggum#252) — external,
    citable facts distinct from an assumption (a claim under test) or a measurement
    (a criterion's value). Every finding was refused at record time without a
    non-empty --source-url, so this list is journal-backed by construction."""
    return [r for r in bet_events(records, bet_id) if r.get("event") == "finding"]


# ---- goalpost hashing ----------------------------------------------------------


def content_hash(obj) -> str:
    """Hash the parsed JSON canonical form — reformatting is not a goalpost edit."""
    return stable_hash(json.dumps(obj, sort_keys=True))


def load_criteria(root: Path, bet_id: str) -> list[dict]:
    path = bet_dir(root, bet_id) / "kill-criteria.json"
    if not path.is_file():
        raise BetError(f"missing {path}")
    return parse_criteria_file(path)


def parse_criteria_file(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise BetError(f"cannot parse {path}: {e}") from e
    crit = data.get("criteria") if isinstance(data, dict) else data
    if not isinstance(crit, list):
        raise BetError(f"{path}: expected a list or {{'criteria': [...]}}")
    return crit


def goalpost_baseline(records: list[dict], bet_id: str) -> tuple[str | None, str | None]:
    """(envelope_hash, criteria_hash) last journaled for this bet (create/rebaseline)."""
    env_h = crit_h = None
    for rec in bet_events(records, bet_id):
        d = rec.get("details", {}) or {}
        if rec.get("event") == "bet-create":
            env_h = d.get("envelope_hash", env_h)
            crit_h = d.get("criteria_hash", crit_h)
        elif rec.get("event") == "rebaseline":
            env_h = d.get("new_envelope_hash", env_h)
            crit_h = d.get("new_criteria_hash", crit_h)
    return env_h, crit_h


def goalpost_findings(root: Path, bet: dict, records: list[dict]) -> list[str]:
    """Envelope/criteria drift vs the journaled baseline — an edit outside
    `rebaseline` is a goalpost move and must be visible, never silently absorbed."""
    out = []
    env_h, crit_h = goalpost_baseline(records, bet["id"])
    if env_h and content_hash(bet.get("envelope", {})) != env_h:
        out.append(
            f"{bet['id']}: envelope hash does not match the journaled baseline — "
            "edited outside `bet.py rebaseline` (goalposts moved)"
        )
    if crit_h:
        try:
            cur = content_hash(load_criteria(root, bet["id"]))
        except BetError as e:
            out.append(f"{bet['id']}: kill criteria unreadable ({e})")
        else:
            if cur != crit_h:
                out.append(
                    f"{bet['id']}: kill-criteria hash does not match the journaled "
                    "baseline — edited outside `bet.py rebaseline` (goalposts moved)"
                )
    return out


# ---- states-and-dates soundness lint -------------------------------------------


def _valid_date(s) -> bool:
    try:
        date.fromisoformat(str(s))
        return True
    except ValueError:
        return False


def criteria_soundness(criteria: list[dict]) -> list[str]:
    """A criterion missing the measurable state OR the date is malformed (Duke:
    every kill criterion is a state AND a date — no timeout-free criteria)."""
    out = []
    for i, c in enumerate(criteria):
        if not isinstance(c, dict):
            out.append(f"criteria[{i}]: not an object")
            continue
        cid = c.get("id") or f"criteria[{i}]"
        if not c.get("metric"):
            out.append(f"{cid}: malformed — no measurable state (missing metric)")
        if c.get("comparator") not in COMPARATORS:
            out.append(
                f"{cid}: malformed — no measurable state (comparator must be one of "
                f"{sorted(COMPARATORS)}, got {c.get('comparator')!r})"
            )
        if not isinstance(c.get("threshold"), (int, float)):
            out.append(f"{cid}: malformed — no measurable state (missing numeric threshold)")
        if "by_date" not in c or not _valid_date(c.get("by_date")):
            out.append(f"{cid}: malformed — no date (by_date missing or not ISO)")
        if c.get("direction", "has") not in ("has", "has_not"):
            out.append(f"{cid}: malformed — direction must be has|has_not")
    return out


def envelope_soundness(envelope: dict) -> list[str]:
    out = []
    tranches = envelope.get("tranches") or []
    for i, t in enumerate(tranches):
        if not isinstance(t, dict) or not isinstance(t.get("amount_usd"), (int, float)):
            out.append(f"tranches[{i}]: malformed — missing numeric amount_usd")
    amounts = [t.get("amount_usd", 0) for t in tranches if isinstance(t, dict)]
    cap = envelope.get("cash_cap_usd", 0)
    if amounts and sum(amounts) > cap:
        out.append(
            f"tranches sum to ${sum(amounts):g} > cash_cap_usd ${cap:g} — "
            "the tranche ladder exceeds the envelope"
        )
    return out


def liability_soundness(envelope: dict) -> list[str]:
    """Liability-exposure soundness lint (#277): every envelope must record an
    explicit `liability_exposure` — its total ABSENCE is a finding (an
    uncapped indemnity taken by oversight is indistinguishable from one taken
    on purpose unless *something* is always recorded). A STATED value,
    whatever it is — including uncapped_entity/uncapped_personal — is NEVER
    itself a finding here or anywhere else: recording the operator's
    deliberate risk appetite is the point, not a defect to flag. `insured`'s
    `responds` defaults to `unverified` when absent (only a human answer
    moves it) — that default is complete, not malformed."""
    exposure = envelope.get("liability_exposure")
    if exposure is None:
        return [
            "liability_exposure unset — the affordable-loss envelope's liability "
            "dimension must be recorded explicitly (capped_at/insured/"
            "uncapped_entity/uncapped_personal); a STATED value is never itself "
            "a finding, only an unset one is (#277)"
        ]
    if not isinstance(exposure, dict) or exposure.get("type") not in LIABILITY_TYPES:
        return [
            f"liability_exposure malformed — type must be one of "
            f"{sorted(LIABILITY_TYPES)}, got {exposure!r}"
        ]
    t = exposure["type"]
    if t == "capped_at" and not isinstance(exposure.get("amount_usd"), (int, float)):
        return ["liability_exposure malformed — capped_at requires a numeric amount_usd"]
    if t == "insured":
        if not exposure.get("policy"):
            return ["liability_exposure malformed — insured requires a policy name/id"]
        responds = exposure.get("responds", "unverified")
        if responds not in RESPONDS_VALUES:
            return [
                f"liability_exposure malformed — insured.responds must be one of "
                f"{sorted(RESPONDS_VALUES)}, got {responds!r}"
            ]
    return []


def liability_concurrency(root: Path) -> dict:
    """Portfolio-level concurrency count (#277 item 3) — the check that
    actually matters: one unbounded exposure is a considered bet; several
    concurrently is a portfolio that cannot survive one bad event. Counts
    every bet whose contractual position has not definitively exited
    (LIABILITY_EXITED_STATES); reported the way the in-flight cap reports
    attention — always visible, never a gate."""
    carriers = [
        b["id"] for b in all_bets(root)
        if b.get("state") not in LIABILITY_EXITED_STATES
        and ((b.get("envelope") or {}).get("liability_exposure") or {}).get("type")
        in UNCAPPED_LIABILITY_TYPES
    ]
    return {"count": len(carriers), "bets": sorted(carriers)}


# ---- low-cap distribution-divergence screens (#275, §9.6.5 screens 8-13) -------
#
# Six candidate additions to the §9.5 standing screens, mechanized as
# report-only bet.json lints (docs/gate-rollout.md — a new blocking signal
# needs a precision dry-run first; NEVER_GATES_PREFIXES's "screen:" entry is
# how they stay report-only even under --gate, same posture as #274's
# "capacity:" checks). Every screen's operator-entered facts live under
# `bet["low_cap_screens"]` (templates/bet-schema.json) — an ABSENT field means
# that screen is UNRESOLVED (cannot run), never a silent pass. Screens 8, 11,
# and 12 need genuinely external data (a public-source headcount, channel
# research, keyword/CPC or forum data) that CW cannot produce itself; 9, 10,
# and 13 are answerable directly from the operator's own description of the
# product, but are held to the SAME unresolved-not-silent-pass discipline for
# one consistent convention rather than a special case per screen.


def _screen(msg: str) -> str:
    return f"screen: {msg}"


def enumerable_buyers_findings(bet: dict) -> list[str]:
    """Screen 8: >=500 named prospects producible from public sources in an
    afternoon; reject above 5,000 — the dead zone too big for personal
    outbound and too small for paid."""
    n = (bet.get("low_cap_screens") or {}).get("enumerable_buyers_count")
    if n is None:
        return [_screen(
            "enumerable buyers unresolved — needs a headcount of named prospects "
            "producible from public sources in an afternoon (§9.6.5 screen 8)"
        )]
    if n < ENUMERABLE_BUYERS_MIN:
        return [_screen(
            f"enumerable buyers count {n} is below {ENUMERABLE_BUYERS_MIN} — no "
            "list, no distribution, no business (§9.6.5 screen 8)"
        )]
    if n > ENUMERABLE_BUYERS_MAX:
        return [_screen(
            f"enumerable buyers count {n} exceeds {ENUMERABLE_BUYERS_MAX} — the "
            "dead zone too big for personal outbound and too small for paid "
            "(§9.6.5 screen 8)"
        )]
    return []


def support_hazard_findings(bet: dict) -> list[str]:
    """Screen 9: reject real-time/critical-path/regulatory-deadline
    functionality — a solo operator cannot carry an on-call obligation.
    Reaches the same conclusion as #260's regulated-calculation-liability
    rabbitry kill from the attention direction rather than liability — the
    convergence is corroboration, so this is ONE check with both rationales
    named, not two overlapping ones."""
    hazard = (bet.get("low_cap_screens") or {}).get("support_hazard")
    flag = hazard.get("has_realtime_or_regulatory_hazard") if isinstance(hazard, dict) else None
    if flag is None:
        return [_screen(
            "support-obligation hazard unresolved — needs an explicit answer: does "
            "this bet involve real-time, critical-path, or regulatory-deadline "
            "functionality? (§9.6.5 screen 9)"
        )]
    if flag:
        return [_screen(
            "support-obligation hazard — bet declares real-time/critical-path/"
            "regulatory-deadline functionality a solo operator cannot carry "
            "on-call (§9.6.5 screen 9; converges with #260's liability-direction "
            "rabbitry kill)"
        )]
    return []


def structural_retention_findings(bet: dict) -> list[str]:
    """Screen 10: passes if the product stores records the buyer must retain
    >=5 years, or runs a weekly-recurring workflow. Satisfaction is not
    retention."""
    sr = (bet.get("low_cap_screens") or {}).get("structural_retention")
    years = sr.get("retention_years") if isinstance(sr, dict) else None
    weekly = sr.get("weekly_recurring_workflow") if isinstance(sr, dict) else None
    if years is None and weekly is None:
        return [_screen(
            "structural retention unresolved — needs retention_years and/or "
            "weekly_recurring_workflow (§9.6.5 screen 10)"
        )]
    if weekly or (years is not None and years >= STRUCTURAL_RETENTION_MIN_YEARS):
        return []
    return [_screen(
        "no structural retention — product neither retains records "
        f">= {STRUCTURAL_RETENTION_MIN_YEARS} years nor runs a weekly-recurring "
        "workflow; satisfaction is not retention (§9.6.5 screen 10)"
    )]


def _channel_passes(entry: dict) -> bool:
    t = entry.get("type")
    if t == "association_newsletter":
        cost = entry.get("cost_usd")
        return isinstance(cost, (int, float)) and cost < CHANNEL_NEWSLETTER_MAX_COST_USD
    if t == "app_marketplace":
        return True  # existence alone is the bar — a browsed marketplace
    if t == "trade_show":
        attendees = entry.get("attendees")
        return isinstance(attendees, (int, float)) and attendees < CHANNEL_TRADE_SHOW_MAX_ATTENDEES
    if t == "member_group":
        members = entry.get("members")
        return isinstance(members, (int, float)) and members > CHANNEL_MEMBER_GROUP_MIN_MEMBERS
    return False


def channel_existence_findings(bet: dict) -> list[str]:
    """Screen 11: >=1 of: sponsorable association newsletter <$500, a browsed
    app marketplace, a trade show <5,000 attendees, a >2k-member group. Needs
    external channel research the operator supplies; absent -> UNRESOLVED,
    never a silent pass (#275 — one of the three screens needing external
    data sources)."""
    channels = (bet.get("low_cap_screens") or {}).get("channels")
    if not channels:
        return [_screen(
            "channel existence unresolved — needs >=1 researched channel: "
            "sponsorable association newsletter <$500, a browsed app marketplace, "
            "a trade show <5,000 attendees, or a >2k-member group (§9.6.5 screen 11)"
        )]
    if any(_channel_passes(c) for c in channels if isinstance(c, dict)):
        return []
    return [_screen(
        f"channel existence — none of {len(channels)} entered channel(s) meet the "
        "size/cost bar (§9.6.5 screen 11)"
    )]


def dark_matter_demand_findings(bet: dict) -> list[str]:
    """Screen 12: 10-300 searches/mo across a 10-20 keyword cluster with
    nonzero CPC, OR >=3 findable 'what software do you use for X' threads.
    Supersedes the cruder 'reject if search volume is high' heuristic two
    models proposed independently: volume alone is ambiguous,
    volume-with-commercial-intent is the signal. Needs external keyword/
    thread research; absent -> UNRESOLVED, never a silent pass (#275 — one of
    the three screens needing external data sources)."""
    dm = (bet.get("low_cap_screens") or {}).get("dark_matter_demand")
    if not isinstance(dm, dict):
        dm = {}
    cluster = dm.get("keyword_cluster_size")
    searches = dm.get("monthly_searches")
    cpc = dm.get("nonzero_cpc")
    threads = dm.get("so_threads_count")
    if cluster is None and searches is None and cpc is None and threads is None:
        return [_screen(
            "dark-matter demand unresolved — needs keyword-cluster search-volume/"
            "CPC data or a count of 'what software do you use for X' threads "
            "(§9.6.5 screen 12)"
        )]
    keyword_pass = (
        cluster is not None and DARK_MATTER_MIN_CLUSTER <= cluster <= DARK_MATTER_MAX_CLUSTER
        and searches is not None and DARK_MATTER_MIN_SEARCHES <= searches <= DARK_MATTER_MAX_SEARCHES
        and bool(cpc)
    )
    thread_pass = threads is not None and threads >= DARK_MATTER_MIN_THREADS
    if keyword_pass or thread_pass:
        return []
    return [_screen(
        "dark-matter demand — keyword/CPC/thread data present but below every "
        "pass threshold (§9.6.5 screen 12)"
    )]


def opportunity_cost_findings(bet: dict, means: dict | None) -> list[str]:
    """Screen 13: every low-cap bet is judged against the operator's
    contracting rate for the same hours — never against zero, never against
    venture outcomes. This is the counterfactual the startup corpus never
    forces, and the ledger had no field for it before #275. Needs
    means.json's contracting_rate_usd_per_hour AND this bet's
    projected_mrr_usd/projected_hours_per_week; either absent -> UNRESOLVED,
    never a silent pass."""
    rate = (means or {}).get("contracting_rate_usd_per_hour")
    oc = (bet.get("low_cap_screens") or {}).get("opportunity_cost") or {}
    projected_mrr = oc.get("projected_mrr_usd")
    projected_hours = oc.get("projected_hours_per_week")
    if rate is None or projected_mrr is None or not projected_hours:
        return [_screen(
            "opportunity-cost benchmark unresolved — needs means.json "
            "contracting_rate_usd_per_hour plus this bet's projected_mrr_usd/"
            "projected_hours_per_week (§9.6.5 screen 13)"
        )]
    hourly = projected_mrr / (projected_hours * WEEKS_PER_MONTH)
    if hourly >= rate:
        return []
    return [_screen(
        f"opportunity-cost benchmark — projected ${hourly:,.2f}/hr is below the "
        f"operator's ${rate:,.2f}/hr contracting rate (§9.6.5 screen 13)"
    )]


def low_cap_screen_findings(root: Path, bet: dict) -> list[str]:
    """All six low-cap screens (#275, §9.6.5 screens 8-13), combined. Every
    finding is already prefixed 'screen: ' (NEVER_GATES_PREFIXES) — this whole
    dimension is a brand-new reinterpretation of the §9.5 standing screens and
    so never gates, even under --gate, until validated against a real
    candidate set (docs/gate-rollout.md), same posture as #274's 'capacity:'
    checks."""
    means = load_means(root)
    return (
        enumerable_buyers_findings(bet)
        + support_hazard_findings(bet)
        + structural_retention_findings(bet)
        + channel_existence_findings(bet)
        + dark_matter_demand_findings(bet)
        + opportunity_cost_findings(bet, means)
    )


# ---- signal-source tiering + competitor sweep (#254) ---------------------------

SIGNAL_TIERS = ("A", "B", "C")
# Convergence-risk staleness bar (chief-wiggum#254): a Tier-C (public) signal's
# competitor sweep older than this is stale — public boards move, and the whole
# point is that someone else may be reading the same page today.
STALE_SWEEP_DAYS = 30


def competitor_sweep_soundness(sweep) -> list[str]:
    """states-and-dates-style soundness for a `competitor_sweep` block: every
    required field present and well-typed. Malformed input is a finding, never a
    crash — mirrors `criteria_soundness`'s shape for the same reason."""
    if not isinstance(sweep, dict):
        return ["competitor_sweep must be an object"]
    out = []
    if not _valid_date(sweep.get("date")):
        out.append("competitor_sweep.date missing or not ISO (YYYY-MM-DD)")
    if not isinstance(sweep.get("sources"), list):
        out.append("competitor_sweep.sources must be a list")
    competitors = sweep.get("competitors")
    if not isinstance(competitors, list):
        out.append("competitor_sweep.competitors must be a list")
    elif any(not isinstance(c, dict) or not c.get("name") for c in competitors):
        out.append("competitor_sweep.competitors entries each need a non-empty `name`")
    if not isinstance(sweep.get("unresolved"), list):
        out.append("competitor_sweep.unresolved must be a list")
    return out


def competitor_sweep_findings(bet: dict, as_of: date) -> list[str]:
    """Tier-C signal grounding must be legible (chief-wiggum#254): a bet whose
    thesis rests solely on a public (Tier C) signal is contested by construction —
    the competitor sweep for it must exist and be current, checked at CREATE time,
    not at name-pick time (the sequencing that missed a real direct-twin collision).
    Report-only always: contested markets are frequently the correct call (§9.4 —
    neglect arbitrage, judo strategy); the mandate is that contestedness is KNOWN
    and STATED, never discovered after the domain is bought. Tier A/B or an
    undeclared tier never produce a finding here beyond the `skipped:` note."""
    tier = bet.get("signal_tier")
    if tier is None:
        return ["skipped: signal_tier not declared (chief-wiggum#254)"]
    if tier != "C":
        return []
    sweep = bet.get("competitor_sweep")
    if not sweep:
        return [
            "Tier-C signal (public, contested by construction) with no "
            "competitor_sweep recorded — run one now, not at name-pick time "
            "(chief-wiggum#254)"
        ]
    try:
        age = (as_of - date.fromisoformat(sweep["date"])).days
    except (KeyError, TypeError, ValueError):
        return ["competitor_sweep.date is missing/malformed — cannot assess staleness"]
    if age > STALE_SWEEP_DAYS:
        return [
            f"competitor_sweep is {age}d old (>{STALE_SWEEP_DAYS}d) — stale for a "
            "Tier-C bet; someone else may be reading the same public signal today"
        ]
    return []


# ---- standing screen 15: regulated-calculation liability (#260) ---------------

# Keyword sweep over the thesis text — deliberately loose (a false positive just
# means an operator answers a screen that turns out irrelevant; a false negative
# means a real compliance-calculation bet ships unscreened, the worse failure).
REGULATED_CALCULATION_KEYWORDS = (
    "wage", "wages", "payroll", "tax", "taxes", "superannuation", "super",
    "benefit", "benefits", "dosing", "dosage", "clinical", "prescri",
    "safety threshold", "award rate", "penalty rate", "entitlement",
    "compliance calculat", "regulatory calculat",
)
REGULATED_SCREEN_FIELDS = (
    "who_bears_error", "correctness_winnable", "insurable",
    "paid_configuration", "interpretation_surface",
)


def regulated_calculation_soundness(screen) -> list[str]:
    """Every sub-question of standing screen 15 must be present and non-empty when
    a screen is recorded at all — a half-answered screen is a malformed screen, the
    same soundness discipline as `criteria_soundness`."""
    if not isinstance(screen, dict):
        return ["regulated_calculation_screen must be an object"]
    return [
        f"regulated_calculation_screen.{f} missing or empty"
        for f in REGULATED_SCREEN_FIELDS
        if not str(screen.get(f, "")).strip()
    ]


def regulated_calculation_findings(bet: dict) -> list[str]:
    """Standing screen 15 (chief-wiggum#260, §9.5): a bet whose thesis names a
    regulated-calculation domain (wages, tax, super, benefits, clinical dosing,
    safety thresholds) but carries no recorded screen is a report-only finding —
    never a block. Regulated calculation is a real market; the mandate is only that
    the screen is ANSWERED, not that the answer be favorable."""
    thesis = (bet.get("thesis") or "").lower()
    if not any(k in thesis for k in REGULATED_CALCULATION_KEYWORDS):
        return []
    if bet.get("regulated_calculation_screen"):
        return []
    return [
        "thesis mentions a regulated-calculation domain with no "
        "regulated_calculation_screen recorded — answer standing screen 15 "
        "(who bears the error, is correctness winnable, is it insurable, will "
        "the operator do paid configuration, what is the interpretation surface — "
        "chief-wiggum#260, `bet.py create --regulated-calculation-screen JSON`)"
    ]


def build_cost_findings(bet: dict, summary: dict) -> list[str]:
    """Envelope build-cost caps (#257) — declared, not silently enforced. Both caps
    are optional; absent → silent for that cap (never guessed). Report-only always
    (NEVER_GATES_PREFIXES "buildcost:"): plan-share accounting is estimate-shaped and
    this reinterprets an existing cap the same way #274's capacity checks do."""
    env = bet.get("envelope", {}) or {}
    out = []
    nom_cap = env.get("nominal_build_cap_usd")
    if isinstance(nom_cap, (int, float)) and summary.get("nominal_usd") is not None \
            and summary["nominal_usd"] > nom_cap:
        out.append(
            f"buildcost: nominal build cost ${summary['nominal_usd']:g} exceeds "
            f"nominal_build_cap_usd ${nom_cap:g}"
        )
    share_cap = env.get("plan_share_cap_pct")
    if isinstance(share_cap, (int, float)) and summary.get("plan_share_pct") is not None \
            and summary["plan_share_pct"] > share_cap:
        out.append(
            f"buildcost: plan-share {summary['plan_share_pct']:g}% exceeds "
            f"plan_share_cap_pct {share_cap:g}%"
        )
    return out


# ---- ledger --------------------------------------------------------------------


def ledger_path(root: Path, bet_id: str) -> Path:
    return bet_dir(root, bet_id) / "ledger.jsonl"


def load_ledger(root: Path, bet_id: str) -> list[dict]:
    p = ledger_path(root, bet_id)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                sys.stderr.write(f"bet: warning — unparsable ledger line in {p}, skipping\n")
    return out


def spend_totals(entries: list[dict]) -> tuple[float, float]:
    cash = sum(e.get("amount_usd") or 0 for e in entries)
    hours = sum(e.get("hours") or 0 for e in entries)
    return cash, hours


def unlocked_cap(bet: dict) -> float:
    """Cumulative unlocked tranche amount. No tranches → the whole cash cap is
    available; with tranches, null-milestone tranches plus journaled unlocks."""
    env = bet.get("envelope", {})
    tranches = env.get("tranches") or []
    if not tranches:
        return env.get("cash_cap_usd", 0)
    unlocked = set(bet.get("unlocked_milestones") or [])
    return sum(
        t.get("amount_usd", 0)
        for t in tranches
        if isinstance(t, dict)
        and (t.get("unlock_milestone_id") is None or t.get("unlock_milestone_id") in unlocked)
    )


# ---- pending kill proposals ----------------------------------------------------


def pending_kill(records: list[dict], bet_id: str) -> dict | None:
    """The unresolved kill-proposed event for this bet, if any. Resolved by a
    journaled human act: a kill-override, or a transition into kill_pending or a
    terminal state."""
    pending = None
    for rec in bet_events(records, bet_id):
        ev = rec.get("event")
        if ev == "kill-proposed":
            pending = rec
        elif ev == "kill-override":
            pending = None
        elif ev == "transition":
            to = (rec.get("details", {}) or {}).get("to")
            if to == "kill_pending" or to in TERMINALS:
                pending = None
    return pending


# ---- distribution status (#241 seam) -------------------------------------------


def _channel_records(root: Path, bet_id: str) -> list[dict]:
    p = bet_dir(root, bet_id) / "channels.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        sys.stderr.write(f"bet: warning — cannot parse {p}; treating as no channel records\n")
        return []
    if isinstance(data, dict):
        data = data.get("channels") or data.get("experiments") or []
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []


def distribution_status(root: Path, bet_id: str) -> dict:
    """Attempted-distribution evidence: channel-experiment records that reached
    the Bullseye inner rings (testing|focused|rejected — brainstormed/ranked is
    consideration, not an attempt) plus rep ledger entries. Absent both →
    `unattempted` — reported, never silently omitted (the #237 rule: no-demand
    evidence without attempted distribution is evidence of no marketing, not
    no demand)."""
    channels = _channel_records(root, bet_id)
    by_status: dict[str, int] = {}
    for c in channels:
        st = c.get("status") or c.get("state") or "brainstormed"
        by_status[st] = by_status.get(st, 0) + 1
    experiments = [
        c for c in channels if (c.get("status") or c.get("state")) in EXPERIMENT_STATES
    ]
    reps = [e for e in load_ledger(root, bet_id) if e.get("type") == "rep"]
    attempted = bool(experiments or reps)
    return {
        "status": "attempted" if attempted else "unattempted",
        "channel_experiments": len(experiments),
        "rep_entries": len(reps),
        "channels_by_status": by_status,
    }


def rep_cadence_status(bet: dict, entries: list[dict], as_of: date | None = None) -> dict | None:
    """Rep-cadence check (#241 leg 3): while probing|validating, ≥N Mom-Test
    conversations in the trailing 7 days, counted from the ledger's rep entries
    (default N=3; per-bet override `create --cadence`, journaled). Outside
    those states → None: no check, never a finding. Missed cadence is
    *distribution-not-attempted* evidence for the #237 kill review — a
    demand-kill with skipped reps downgrades to `recycle`."""
    if bet.get("state") not in CADENCE_STATES:
        return None
    as_of = as_of or date.today()
    need = bet.get("rep_cadence_per_week")
    if not isinstance(need, (int, float)) or need <= 0:
        need = DEFAULT_REP_CADENCE
    count = 0
    for e in entries:
        if e.get("type") != "rep":
            continue
        try:
            d = datetime.fromisoformat(str(e.get("ts"))).date()
        except ValueError:
            continue
        if 0 <= (as_of - d).days < 7:
            count += 1
    missed = count < need
    line = (
        f"{count}/{need:g} Mom-Test reps in the 7 days to {as_of.isoformat()} "
        f"[{bet.get('state')}] — {'MISSED' if missed else 'ok'}"
    )
    findings = []
    if missed:
        findings.append(
            f"rep cadence missed: {count}/{need:g} Mom-Test conversations in the "
            f"7 days to {as_of.isoformat()} while {bet.get('state')} — the reps "
            "are the irreducible core the tooling cannot do; this feeds the kill "
            "review as distribution-not-attempted evidence"
        )
    return {"count": count, "required": need, "line": line, "findings": findings}


def traction_findings(bet: dict, entries: list[dict]) -> list[str]:
    """*Traction* 50% rule (#241 leg 3): while probing|validating, traction-
    tagged hours must be ≥ half of all tagged hours. No tagged hours at all →
    silent — a finding must come from data, never from its absence."""
    if bet.get("state") not in CADENCE_STATES:
        return []
    tagged = [
        e for e in entries
        if e.get("tag") in LEDGER_TAGS and isinstance(e.get("hours"), (int, float))
    ]
    total = sum(e["hours"] for e in tagged)
    if total <= 0:
        return []
    share = sum(e["hours"] for e in tagged if e["tag"] == "traction") / total
    if share < TRACTION_SHARE_MIN:
        return [
            f"traction 50% rule: traction share {share:.0%} of {total:g} tagged "
            f"hours while {bet.get('state')} — at least half the effort belongs "
            "on distribution, not product (Weinberg & Mares)"
        ]
    return []


def any_channel_focused(root: Path) -> bool:
    for bet in all_bets(root):
        for c in _channel_records(root, bet["id"]):
            if (c.get("status") or c.get("state")) == "focused":
                return True
    return False


# ---- kill-review quorum (#237) ---------------------------------------------------


def criterion_demand_shaped(c: dict) -> bool:
    """A demand-shaped criterion is one whose firing the operator's own
    distribution gap could explain: direction=has (a reach-the-demand-state
    milestone). A has_not criterion fires on evidence that OCCURRED (refund
    spike, churn), which no marketing gap explains. Explicit override via the
    optional ``demand_shaped`` boolean (templates/kill-criteria-schema.json)."""
    v = c.get("demand_shaped")
    if isinstance(v, bool):
        return v
    return c.get("direction", "has") == "has"


def latest_kill_proposed(records: list[dict], bet_id: str) -> dict | None:
    return next(
        (r for r in reversed(bet_events(records, bet_id)) if r.get("event") == "kill-proposed"),
        None,
    )


def _criteria_baseline_record(records: list[dict], bet_id: str) -> str | None:
    """Record id of the journal event that established the current criteria hash."""
    rec_id = None
    for r in bet_events(records, bet_id):
        d = r.get("details", {}) or {}
        if r.get("event") == "bet-create" and d.get("criteria_hash"):
            rec_id = r.get("record_id", rec_id)
        elif r.get("event") == "rebaseline" and d.get("new_criteria_hash"):
            rec_id = r.get("record_id", rec_id)
    return rec_id


def cheapest_untried_exposure(root: Path, bet: dict) -> str:
    """Name the cheapest exposure not yet attempted — the distribution finding a
    fairness-downgraded kill verdict must carry. Deterministic, artifact-backed:
    founder reps are always the cheapest ($0) when none exist; else the highest-
    ranked untried Bullseye channel; else the first untried enum channel."""
    reps = [e for e in load_ledger(root, bet["id"]) if e.get("type") == "rep"]
    if not reps:
        need = bet.get("rep_cadence_per_week")
        if not isinstance(need, (int, float)) or need <= 0:
            need = DEFAULT_REP_CADENCE
        return (
            f"founder reps — {need:g} Mom-Test conversations/week "
            "(`bet.py spend --rep`), $0; the rep ledger shows none"
        )
    channels = _channel_records(root, bet["id"])
    if not channels:
        return "no channel records at all — `channel.py brainstorm` (the 19-channel enum), $0"
    untried = [
        c for c in channels
        if (c.get("status") or c.get("state") or "brainstormed") not in EXPERIMENT_STATES
    ]
    ranked = sorted(
        (c for c in untried if isinstance(c.get("rank"), (int, float))),
        key=lambda c: c["rank"],
    )
    if ranked:
        return (
            f"channel `{ranked[0].get('channel')}` (rank {ranked[0]['rank']:g}, "
            "never tested — `channel.py test`)"
        )
    if untried:
        return (
            f"channel `{untried[0].get('channel')}` (brainstormed, never tested — "
            "`channel.py rank` then `test`)"
        )
    return "every recorded channel reached testing — the gap is exposure volume, not channel count"


def brief_purity_findings(text: str, bet: dict, facts: list[dict]) -> list[str]:
    """The lintable invariant behind the generated brief (#237 decision 2):
    every measured value cites a journal record id or an artifact file in this
    bet's directory, or is explicitly ``UNRESOLVED:`` — and the bet's narrative
    (thesis prose) never reaches the fresh-context evaluator."""
    out = []
    for f in facts:
        val = str(f.get("value", ""))
        if not f.get("source") and not val.startswith("UNRESOLVED:"):
            out.append(
                f"unsourced value in brief: {f.get('label')} = {val} — every "
                "measured value must cite a journal record id or artifact file"
            )
    thesis = (bet.get("thesis") or "").strip()
    if thesis and thesis in text:
        out.append(
            "thesis prose leaked into the brief — the fresh-context evaluator "
            "must not inherit the bet's narrative (the missing context is the feature)"
        )
    for m in _SOURCE_RE.finditer(text):
        for src in m.group(1).split(";"):
            src = src.strip()
            if not (_REC_ID_RE.match(src) or src.startswith(f"bets/{bet.get('id')}/")):
                out.append(
                    f"non-journal, non-artifact source cited: {src!r} — the brief "
                    "generator reads only journal-backed artifacts"
                )
    return out


def build_kill_brief(
    root: Path, bet_id: str, as_of: date | None = None, trigger: str | None = None,
) -> tuple[str, list[str], dict]:
    """Render the fresh-context kill brief from journal-backed artifacts ONLY.

    Returns ``(markdown, findings, meta)``. ``findings`` (goalpost drift +
    purity-lint hits) non-empty ⇒ the caller must refuse to emit the brief —
    exit 1, a hard self-check, never a ``--gate``. ``meta`` carries what the
    fairness rule needs: fired criteria, demand-shaped fired criteria, the
    live distribution status, and the cheapest untried exposure.

    ``trigger`` (chief-wiggum#252) states which of the two legitimate kill-review
    triggers convened this brief — a dated criterion firing, or a material premise
    change (an external `finding` bearing on the bet's premise). Explicit callers
    (``--trigger``) win; otherwise it is auto-detected: a fired criterion always
    means ``criterion`` (it already recommends review); absent that, any
    premise-bearing finding means ``premise-change``; absent both, ``criterion``
    is the ad-hoc default for a human-convened review with no specific finding."""
    bet = load_bet(root, bet_id)
    records = load_journal(root)
    as_of = as_of or date.today()
    # A brief rendered from hand-moved goalposts is not journal-backed.
    findings = list(goalpost_findings(root, bet, records))
    criteria = load_criteria(root, bet_id)
    facts: list[dict] = []

    def fact(label: str, value, source: str | None) -> str:
        facts.append({"label": label, "value": value, "source": source})
        return f"- {label}: {value}" + (f" [source: {source}]" if source else "")

    ledger_src = f"bets/{bet_id}/ledger.jsonl"
    bet_src = f"bets/{bet_id}/bet.json"
    chan_src = f"bets/{bet_id}/channels.json"
    asm_src = f"bets/{bet_id}/assumptions.json"

    kp_for_trigger = latest_kill_proposed(records, bet_id)
    fired_for_trigger = (kp_for_trigger.get("details", {}) or {}).get("criteria") or [] if kp_for_trigger else []
    material_findings = load_findings(records, bet_id)
    premise_findings = [f for f in material_findings if (f.get("details", {}) or {}).get("bearing_on") == "premise"]
    if trigger is None:
        trigger = "criterion" if fired_for_trigger else ("premise-change" if premise_findings else "criterion")

    trigger_note = (
        "a material external finding bears on the bet's premise — "
        "\"insufficient evidence gathered yet\" is NOT by itself grounds for `hold` "
        "here; the question is whether the premise still supports continuing to spend"
        if trigger == "premise-change" else
        "a pre-registered dated kill criterion fired"
    )

    lines = [
        f"# Kill brief: {bet_id} — {bet.get('title', '')}",
        "",
        f"State: `{bet.get('state')}` as of {as_of.isoformat()}. Convening trigger: "
        f"**{trigger}** ({trigger_note}). This brief contains ONLY "
        "journal-backed artifacts: pre-registered kill criteria, measured values with "
        "sources, envelope status, open-assumption evidence, material findings, and "
        "distribution attempts. It deliberately contains no history, no working context, "
        "and no thesis prose — you are the fresh-context evaluator, and the missing "
        "context is the feature (Boulding et al. 1997).",
        "",
    ]

    # -- material findings (#252): external, citable facts bearing on the bet —
    #    distinct from an assumption (a claim under test) and a measurement (a
    #    criterion's value). Journal-backed by construction (every `finding` event
    #    was refused at record time without a --source-url).
    lines.append("## Material findings")
    lines.append("")
    if not material_findings:
        lines.append("- none recorded")
    for f in material_findings:
        d = f.get("details", {}) or {}
        lines.append(fact(
            f"finding bearing on {d.get('bearing_on', '?')} "
            f"[{d.get('evidence_grade', 'reported')}]",
            f"{d.get('statement', '')!r} — {d.get('source_url', '?')}",
            f["record_id"],
        ))
    lines.append("")

    # -- criteria, verbatim, hash-cited
    crit_hash = content_hash(criteria)
    baseline_rec = _criteria_baseline_record(records, bet_id)
    cite = f" [source: {baseline_rec}]" if baseline_rec else ""
    lines += [
        f"## Kill criteria (pre-registered goalposts, verbatim; hash `{crit_hash[:12]}…`"
        f" journaled at create/rebaseline{cite})",
        "",
        "```json",
        json.dumps(criteria, indent=2, sort_keys=True),
        "```",
        "",
    ]

    # -- measured values: ONLY from the journaled kill-proposed evaluation rows
    kp = latest_kill_proposed(records, bet_id)
    rows_by_id: dict[str, dict] = {}
    fired: list[str] = []
    if kp:
        d = kp.get("details", {}) or {}
        fired = [c for c in d.get("criteria") or [] if isinstance(c, str)]
        for row in d.get("rows") or []:
            if isinstance(row, dict) and row.get("id"):
                rows_by_id[row["id"]] = row
    lines.append("## Measured values")
    lines.append("")
    for c in criteria:
        cid = c.get("id") or "?"
        metric = c.get("metric") or "?"
        shape = "demand-shaped" if criterion_demand_shaped(c) else "not demand-shaped"
        row = rows_by_id.get(cid)
        if row is not None and isinstance(row.get("measured"), (int, float)):
            lines.append(fact(
                f"{cid} {metric} ({shape})",
                f"{row['measured']:g} — status {row.get('status', '?')}",
                kp["record_id"],
            ))
        elif row is not None:
            lines.append(fact(
                f"{cid} {metric} ({shape})",
                f"unmeasured at the journaled evaluation — status {row.get('status', '?')} "
                "(no evidence counts as not achieved)",
                kp["record_id"],
            ))
        else:
            lines.append(fact(
                f"{cid} {metric} ({shape})",
                "UNRESOLVED: no journaled evaluation covers this criterion "
                "(run `bet.py evaluate --results ...`)",
                None,
            ))
    lines.append("")

    # -- envelope status
    entries = load_ledger(root, bet_id)
    cash, hours = spend_totals(entries)
    cap = unlocked_cap(bet)
    env = bet.get("envelope", {})
    lines.append("## Envelope status (spend vs tranches)")
    lines.append("")
    lines.append(fact("cumulative cash spend", f"${cash:g}", ledger_src))
    lines.append(fact(
        "unlocked tranches",
        f"${cap:g} of ${env.get('cash_cap_usd', 0):g} cash cap"
        f" — spend {'EXCEEDS unlocked' if cash > cap else 'within envelope'}",
        bet_src,
    ))
    lines.append(fact("cumulative hours", f"{hours:g}", ledger_src))
    time_cap = env.get("time_cap_hours")
    if isinstance(time_cap, (int, float)):
        lines.append(fact("time cap", f"{time_cap:g}h", bet_src))
    lines.append("")

    # -- build cost (#257): the dominant input to most bets, invisible until now.
    # An expensive bet under review deserves to have that visible to a
    # fresh-context evaluator, same as cash spend.
    import build_cost
    bc_records = build_cost.load_build_costs(root, bet_id)
    bc_src = "; ".join(r["record_id"] for r in bc_records) or None
    bc = build_cost.summarize(bc_records)
    lines.append("## Build cost (nominal + plan-share)")
    lines.append("")
    if bc["records"]:
        nominal = f"${bc['nominal_usd']:g}" if bc["nominal_usd"] is not None else "UNRESOLVED (unpriced model)"
        if bc["nominal_partial"]:
            nominal += " (partial — some entries unpriced)"
        share = f"{bc['plan_share_pct']:g}%" if bc["plan_share_pct"] is not None else "UNRESOLVED (not supplied)"
        if bc["plan_share_partial"]:
            share += " (partial — some entries unresolved)"
        lines.append(fact("nominal build cost", nominal, bc_src))
        lines.append(fact("plan-share consumed", share, bc_src))
        nom_cap = env.get("nominal_build_cap_usd")
        if isinstance(nom_cap, (int, float)):
            lines.append(fact("nominal_build_cap_usd", f"${nom_cap:g}", bet_src))
        share_cap = env.get("plan_share_cap_pct")
        if isinstance(share_cap, (int, float)):
            lines.append(fact("plan_share_cap_pct", f"{share_cap:g}%", bet_src))
    else:
        lines.append("- none recorded")
    lines.append("")

    # -- signal-source grounding + competitor sweep (#254): a Tier-C bet's
    #    convergence risk must be legible to the fresh-context evaluator, not just
    #    to the operator at create time.
    lines.append("## Signal grounding (convergence risk)")
    lines.append("")
    tier = bet.get("signal_tier")
    lines.append(fact("signal tier", tier if tier else "UNRESOLVED: not declared", bet_src))
    sweep = bet.get("competitor_sweep")
    if sweep and isinstance(sweep, dict):
        names = ", ".join(c.get("name", "?") for c in sweep.get("competitors") or []) or "none found"
        lines.append(fact(
            "competitor sweep",
            f"run {sweep.get('date', '?')} via {', '.join(sweep.get('sources') or []) or '?'} "
            f"— competitors: {names}",
            bet_src,
        ))
        if sweep.get("unresolved"):
            lines.append(fact("sweep unresolved items", "; ".join(sweep["unresolved"]), bet_src))
    else:
        lines.append(fact(
            "competitor sweep",
            "UNRESOLVED: no competitor_sweep recorded"
            + (" — REQUIRED for a Tier-C bet" if tier == "C" else ""),
            None,
        ))
    for f in competitor_sweep_findings(bet, as_of):
        if not f.startswith("skipped:"):
            findings_note = f"- **finding**: {f}"
            lines.append(findings_note)
    lines.append("")

    # -- open-assumption evidence table (assumption.py owns the ledger; the
    #    import is deferred to avoid the module cycle, same as transition)
    lines.append("## Open assumptions (evidence table)")
    lines.append("")
    import assumption as asmlib
    if not asmlib.assumptions_path(root, bet_id).is_file():
        lines.append(
            "- none recorded — no assumptions.json for this bet "
            "(no assumption ledger was registered)"
        )
    else:
        assumptions = asmlib.load_assumptions(root, bet_id)
        cards = asmlib.load_cards(root, bet_id)
        open_asms = [a for a in assumptions if a.get("status") in ("untested", "testing")]
        settled = len(assumptions) - len(open_asms)
        lines.append(fact(
            "assumption ledger",
            f"{len(assumptions)} assumption(s): {len(open_asms)} open, {settled} settled",
            asm_src,
        ))
        for a in open_asms:
            aid = a.get("id", "?")
            eff, _notes = asmlib.asm_effective_strength(aid, cards)
            strength = (
                f"validated evidence strength {eff} ({asmlib.STRENGTH_LABELS.get(eff, '?')})"
                if eff else "no validated evidence"
            )
            lines.append(fact(
                f"{aid} [{a.get('status')}]",
                f"{strength} — {a.get('statement', '')!r}",
                asm_src,
            ))
    lines.append("")

    # -- distribution-attempt table (#241 amendment)
    dist = distribution_status(root, bet_id)
    exposure = cheapest_untried_exposure(root, bet)
    lines.append("## Distribution attempts")
    lines.append("")
    if dist["status"] == "unattempted":
        lines.append(fact(
            "distribution",
            "unattempted — no channel experiment reached testing and no rep entries exist",
            ledger_src,
        ))
    else:
        lines.append(fact(
            "distribution",
            f"attempted — {dist['channel_experiments']} channel experiment(s), "
            f"{dist['rep_entries']} rep entr{'y' if dist['rep_entries'] == 1 else 'ies'}",
            ledger_src,
        ))
    channels = _channel_records(root, bet_id)
    experiments = [
        c for c in channels if (c.get("status") or c.get("state")) in EXPERIMENT_STATES
    ]
    if experiments:
        ran = ", ".join(
            f"{c.get('channel', '?')} ({c.get('status') or c.get('state')})"
            for c in experiments
        )
        exp_cell = fact("channel experiments run", ran, chan_src)
        acquired = [
            f"{c.get('channel', '?')}: {c['customers_acquired']:g}"
            for c in experiments if isinstance(c.get("customers_acquired"), (int, float))
        ]
        acq_cell = (
            fact("customers acquired", "; ".join(acquired), chan_src)
            if acquired else None
        )
        seen = [
            f"{c.get('channel', '?')}: {c[k]:g} {k}"
            for c in experiments for k in EXPOSURE_KEYS
            if isinstance(c.get(k), (int, float))
        ]
        expo_cell = (
            fact("exposure delivered", "; ".join(seen), chan_src)
            if seen else fact(
                "exposure delivered",
                "UNRESOLVED: no exposure (traffic/listings/launches) recorded on any "
                "channel experiment",
                None,
            )
        )
    else:
        exp_cell = fact("channel experiments run", "none (unattempted)", ledger_src)
        acq_cell = None
        expo_cell = fact(
            "exposure delivered",
            "UNRESOLVED: no channel experiments — no exposure was delivered",
            None,
        )
    reps = [e for e in entries if e.get("type") == "rep"]
    cad = rep_cadence_status(bet, entries, as_of)
    if cad is not None:
        rep_cell = fact(
            "rep-cadence adherence",
            f"{cad['count']}/{cad['required']:g} Mom-Test reps in the trailing week "
            f"({'MISSED' if cad['count'] < cad['required'] else 'met'}); "
            f"{len(reps)} rep entr{'y' if len(reps) == 1 else 'ies'} total",
            ledger_src,
        )
    else:
        rep_cell = fact(
            "rep-cadence adherence",
            f"n/a while {bet.get('state')} (cadence applies probing|validating); "
            f"{len(reps)} rep entr{'y' if len(reps) == 1 else 'ies'} total",
            ledger_src,
        )
    fired_demand = []
    by_id = {c.get("id"): c for c in criteria if isinstance(c, dict)}
    for cid in fired:
        c = by_id.get(cid)
        if c is not None and criterion_demand_shaped(c):
            fired_demand.append(cid)
    demand_criteria = [c for c in criteria if isinstance(c, dict) and criterion_demand_shaped(c)]
    lines.append("")
    lines.append("Per demand-shaped criterion (the fairness rule's evidence):")
    lines.append("")
    if not demand_criteria:
        lines.append("- no demand-shaped criteria on this bet")
    for c in demand_criteria:
        cid = c.get("id") or "?"
        fired_note = " — FIRED" if cid in fired else ""
        lines.append(f"- {cid} ({c.get('metric')}){fired_note}:")
        for cell in (exp_cell, expo_cell, acq_cell, rep_cell):
            if cell:
                lines.append(f"  {cell}")
    lines += [
        "",
        "## Verdict required",
        "",
        "Reply with your reasoning, then EXACTLY ONE fenced JSON block:",
        "",
        "```json",
        '{"verdict": "go|kill|hold|recycle", "confidence": 0.0,',
        ' "reasons": ["..."], "cheapest_disconfirming_test": "required for hold"}',
        "```",
        "",
        "Constraints:",
        "- Judge ONLY the evidence above against the pre-registered criteria.",
        "- A `hold` must name the `cheapest_disconfirming_test` that would settle it.",
        "- Distribution-fairness rule (#241 amendment): a demand-shaped criterion that "
        "fired while the distribution-attempt table shows `unattempted` may NOT ground "
        "a `kill` — the verdict space for that criterion is `recycle` (naming the "
        "distribution gap) or `hold`. Zero exposure producing zero signups is evidence "
        "of no marketing, not no demand. A `kill` returned anyway is mechanically "
        "downgraded to `recycle`.",
        "",
    ]

    text = "\n".join(lines)
    findings += brief_purity_findings(text, bet, facts)
    meta = {
        "fired": fired,
        "fired_demand": fired_demand,
        "distribution": dist,
        "exposure": exposure,
        "as_of": as_of.isoformat(),
    }
    return text, findings, meta


def parse_verdict(text: str) -> dict | None:
    """Extract the provider's verdict from its response: the LAST fenced JSON
    block carrying a valid verdict wins; a bare-JSON body is tolerated. Returns
    None when nothing parseable exists — flagged by the caller, never a crash."""
    candidates = [b for b in _FENCE_RE.findall(text or "")]
    candidates.append((text or "").strip())
    for raw in reversed(candidates):
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("verdict") not in VERDICTS:
            continue
        conf = data.get("confidence")
        out = {
            "verdict": data["verdict"],
            "confidence": conf if isinstance(conf, (int, float)) and not isinstance(conf, bool) else None,
            "reasons": [str(r) for r in (data.get("reasons") or []) if str(r).strip()],
        }
        test = data.get("cheapest_disconfirming_test")
        if isinstance(test, str) and test.strip():
            out["cheapest_disconfirming_test"] = test.strip()
        return out
    return None


def collect_verdicts(out_dir: Path) -> tuple[list[dict], list[str]]:
    """Read the quorum's per-provider files (manifest-driven when present) into
    parsed verdict entries. Malformed output is flagged and carried as a
    ``malformed`` entry — tolerated, never fatal (#237 decision 3)."""
    verdicts: list[dict] = []
    findings: list[str] = []
    files: list[tuple[str, Path]] = []
    manifest_path = out_dir / f"{KILL_REVIEW_ROLE}-manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            manifest = {}
        for r in manifest.get("results") or []:
            if r.get("status") == "ok" and r.get("path"):
                files.append((r.get("name", "?"), Path(r["path"])))
            else:
                findings.append(
                    f"provider {r.get('name', '?')}: no response "
                    f"({r.get('error') or 'failed'}) — optional voice missing"
                )
    else:
        for p in sorted(out_dir.glob(f"{KILL_REVIEW_ROLE}-*.md")):
            if p.name.endswith(".error.md"):
                continue
            files.append((p.stem.removeprefix(f"{KILL_REVIEW_ROLE}-"), p))
    for name, path in files:
        if not path.is_file():
            continue
        parsed = parse_verdict(path.read_text())
        if parsed is None:
            findings.append(
                f"provider {name}: malformed verdict output — no parseable fenced "
                "JSON with a go|kill|hold|recycle verdict; flagged, not counted"
            )
            verdicts.append({"provider": name, "verdict": None, "malformed": True})
            continue
        if parsed["verdict"] == "hold" and not parsed.get("cheapest_disconfirming_test"):
            findings.append(
                f"provider {name}: hold verdict without cheapest_disconfirming_test "
                "— a hold must name what evidence would settle it"
            )
        verdicts.append({"provider": name, **parsed})
    return verdicts, findings


def apply_fairness(
    verdicts: list[dict], fired_demand: list[str], dist_status: str, exposure: str,
) -> list[str]:
    """The distribution-fairness verdict rule (#241 amendment): a demand-shaped
    criterion fired with distribution unattempted may not produce `kill` —
    downgrade to `recycle` with a distribution finding naming the cheapest
    untried exposure. Returns the providers downgraded."""
    if not fired_demand or dist_status != "unattempted":
        return []
    downgraded = []
    for v in verdicts:
        if v.get("verdict") != "kill":
            continue
        v["verdict"] = "recycle"
        v["downgraded_from"] = "kill"
        v["distribution_finding"] = (
            f"demand-shaped criterion(s) {', '.join(fired_demand)} fired with "
            "distribution unattempted — evidence of no marketing, not no demand; "
            f"cheapest untried exposure: {exposure}"
        )
        downgraded.append(v.get("provider", "?"))
    return downgraded


# ---- bet-selection lint (#235 amendment) ---------------------------------------


def load_means(root: Path) -> dict | None:
    p = root / MEANS_NAME
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise BetError(f"cannot parse {p}: {e}") from e


def selection_lint(root: Path, bet: dict) -> list[str]:
    """While means.json says sales AND marketing are novice and no channel has
    ever reached `focused`, a bet whose acquisition plan has no built-in-
    distribution ecosystem channel and no owned audience is flagged.
    means.json absent → skipped (reported, exit 0, never a crash)."""
    means = load_means(root)
    if means is None:
        return [f"skipped: no {MEANS_NAME} in the portfolio — bet-selection lint needs it"]
    skills = means.get("skills", {}) or {}
    novice = (
        skills.get("sales", "novice") == "novice"
        and skills.get("marketing", "novice") == "novice"
    )
    if not novice or any_channel_focused(root):
        return []
    acq = bet.get("acquisition", {}) or {}
    if not acq.get("ecosystem_channel") and not acq.get("owned_audience"):
        return [
            f"{bet['id']}: acquisition plan has no ecosystem channel and no owned "
            "audience while means.json says sales/marketing novice and no channel "
            "has reached `focused` — distribution is the binding constraint (#241)"
        ]
    return []


# ---- criteria evaluation -------------------------------------------------------


def _compare(value: float, comparator: str, threshold: float) -> bool:
    return {
        "<": value < threshold, "<=": value <= threshold,
        ">": value > threshold, ">=": value >= threshold,
        "==": value == threshold, "!=": value != threshold,
    }[comparator]


def evaluate_criteria(criteria: list[dict], results: dict, as_of: date) -> list[dict]:
    """States-and-dates evaluation. direction=has: past by_date without the state
    → TRIGGERED (no evidence of the state counts as not achieved — the honesty
    discipline); before the date → pending/met. direction=has_not: the state
    occurring triggers immediately; surviving past the date → held."""
    out = []
    for i, c in enumerate(criteria):
        if not isinstance(c, dict):
            out.append({"id": f"criteria[{i}]", "metric": None,
                        "status": "malformed", "measured": None})
            continue
        cid = c.get("id") or f"criteria[{i}]"
        entry = {"id": cid, "metric": c.get("metric"), "status": "pending", "measured": None}
        try:
            by = date.fromisoformat(str(c.get("by_date")))
        except ValueError:
            entry["status"] = "malformed"
            out.append(entry)
            continue
        direction = c.get("direction", "has")
        value = results.get(c.get("metric"))
        met = None
        if isinstance(value, (int, float)) and c.get("comparator") in COMPARATORS \
                and isinstance(c.get("threshold"), (int, float)):
            entry["measured"] = value
            met = _compare(value, c["comparator"], c["threshold"])
        due = as_of > by
        if direction == "has":
            if met:
                entry["status"] = "met"
            elif due:
                entry["status"] = "triggered"
                entry["reason"] = (
                    f"by_date {by.isoformat()} passed without the state "
                    f"({c.get('metric')} {c.get('comparator')} {c.get('threshold')})"
                    + ("" if met is False else " — unmeasured; no evidence counts as not achieved")
                )
            else:
                entry["status"] = "pending" if met is False else "unmeasured"
        else:  # has_not
            if met:
                entry["status"] = "triggered"
                entry["reason"] = (
                    f"state occurred ({c.get('metric')} {c.get('comparator')} "
                    f"{c.get('threshold')}) — must-not-hold criterion"
                )
            elif due:
                entry["status"] = "held"
            else:
                entry["status"] = "pending" if met is False else "unmeasured"
        out.append(entry)
    return out


# ---- harvest check -------------------------------------------------------------


def harvest_check(bet: dict) -> dict:
    """Est sale value (3.9 × TTM seller-discretionary profit) vs wind-down cost.
    Absent inputs → `skipped` — reported and journaled, never a silent block,
    never a crash (the complexity-snapshot contract)."""
    inputs = bet.get("harvest_inputs") or {}
    ttm = inputs.get("ttm_sdp_usd")
    wind = inputs.get("wind_down_cost_usd")
    if not isinstance(ttm, (int, float)) or not isinstance(wind, (int, float)):
        return {"skipped": "harvest inputs absent (harvest_inputs.ttm_sdp_usd / "
                           ".wind_down_cost_usd) — est sale value unknown"}
    est = round(HARVEST_MULTIPLE * ttm, 2)
    return {
        "est_sale_value_usd": est,
        "wind_down_cost_usd": wind,
        "proposed_terminal": "sold" if est > wind else "killed",
    }


def retrospective_nontrivial(root: Path, bet_id: str) -> bool:
    p = bet_dir(root, bet_id) / "retrospective.md"
    if not p.is_file():
        return False
    prose = "\n".join(
        ln for ln in p.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    return len("".join(prose.split())) >= RETRO_MIN_CHARS


# ---- in-flight cap -------------------------------------------------------------


def in_flight_bets(root: Path) -> list[str]:
    return sorted(b["id"] for b in all_bets(root) if b.get("state") in IN_FLIGHT)


def cap_findings(root: Path, max_in_flight: int, entering: str | None = None) -> list[str]:
    """Bets-in-flight cap (probing|validating|building). `entering` names a bet
    about to enter an in-flight state, counted as if it already had."""
    flight = set(in_flight_bets(root))
    if entering:
        flight.add(entering)
    if len(flight) > max_in_flight:
        return [
            f"bets in flight ({len(flight)}: {', '.join(sorted(flight))}) exceed the "
            f"cap of {max_in_flight} — attention is the binding resource "
            "(park or kill before starting another)"
        ]
    return []


# ---- zombie-fleet capacity accounting (#274) ------------------------------------


def ongoing_load_hours_per_week(
    root: Path, bet_id: str, as_of: date | None = None, window_weeks: int = LOAD_WINDOW_WEEKS,
) -> float:
    """MEASURED, not guessed (#274 item 1): average weekly hours logged on this
    bet's ledger over the trailing `window_weeks` — an optimistic operator
    cannot simply type in a smaller number, because the field is never
    settable at all. Untagged and tagged hours entries both count; rep entries
    (which carry no `hours`) do not."""
    as_of = as_of or date.today()
    cutoff_days = window_weeks * 7
    total = 0.0
    for e in load_ledger(root, bet_id):
        if not isinstance(e.get("hours"), (int, float)):
            continue
        try:
            d = datetime.fromisoformat(str(e.get("ts"))).date()
        except ValueError:
            continue
        if 0 <= (as_of - d).days < cutoff_days:
            total += e["hours"]
    return round(total / window_weeks, 2)


def attention_capacity(root: Path, as_of: date | None = None) -> dict | None:
    """The zombie-fleet arithmetic (#274 item 2, §9.6.3): remaining capacity =
    means.hours_per_week − Σ(measured ongoing load of every LIVE_STATES bet) −
    reserve_hours_per_week. `lifestyle` bets earn zero in-flight slots (by
    design — they are a terminal) while consuming operator hours forever;
    this is the accounting that makes that visible. None (never guessed) when
    means.json is absent or has no numeric hours_per_week."""
    means = load_means(root)
    if means is None or not isinstance(means.get("hours_per_week"), (int, float)):
        return None
    reserve = means.get("reserve_hours_per_week", 0) or 0
    loads = {
        b["id"]: ongoing_load_hours_per_week(root, b["id"], as_of)
        for b in all_bets(root) if b.get("state") in LIVE_STATES
    }
    total_load = sum(loads.values())
    return {
        "hours_per_week": means["hours_per_week"],
        "reserve_hours_per_week": reserve,
        "live_loads": loads,
        "total_load_hours_per_week": total_load,
        "remaining_hours_per_week": means["hours_per_week"] - total_load - reserve,
    }


def capacity_findings(root: Path, as_of: date | None = None) -> list[str]:
    """Capacity-based cap (#274 item 2): fires when the live fleet alone has
    exhausted (or exceeded) the attention budget — independent of, and a
    second bound alongside, the integer --max-in-flight cap (whichever binds
    first is visible; this one counts the fleet the other cannot see). Never
    gates (NEVER_GATES_PREFIXES: "capacity:") — a brand-new reinterpretation
    of what the existing cap reports, unvalidated until run against a real
    portfolio (docs/gate-rollout.md)."""
    cap = attention_capacity(root, as_of)
    if cap is None:
        return ["skipped: no means.json hours_per_week — capacity-based cap needs it"]
    if cap["remaining_hours_per_week"] <= 0:
        detail = ", ".join(
            f"{bid} {h:g}h/wk" for bid, h in sorted(cap["live_loads"].items())
        ) or "none"
        return [
            "capacity: attention exhausted — "
            f"{cap['hours_per_week']:g}h/wk available − {cap['total_load_hours_per_week']:g}h/wk "
            f"live-product load ({detail}) − {cap['reserve_hours_per_week']:g}h/wk reserve = "
            f"{cap['remaining_hours_per_week']:g}h/wk remaining (§9.6.3) — the live fleet "
            "alone consumes the attention budget; starting another bet has no slack"
        ]
    return []


def addition_rule_findings(
    root: Path, entering_id: str, as_of: date | None = None,
) -> list[str]:
    """Addition rule (#274 item 3, §9.6.3): only start product n+1 once
    product n — the most recently added live (lifestyle) product — has run
    below its target load for two consecutive trailing weekly measurement
    periods. No live products yet → nothing to check. Never gates
    ("capacity:" — unvalidated new check, docs/gate-rollout.md)."""
    as_of = as_of or date.today()
    live = [
        b for b in all_bets(root)
        if b.get("state") in LIVE_STATES and b["id"] != entering_id
    ]
    if not live:
        return []

    def _created(b: dict) -> datetime:
        try:
            return datetime.fromisoformat(str(b.get("created")))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    latest = max(live, key=_created)
    target = latest.get("target_load_hours_per_week")
    if not isinstance(target, (int, float)):
        target = ATTENTION_LOAD_THRESHOLD_HOURS
    entries = load_ledger(root, latest["id"])
    periods = []
    for k in range(2):
        start = as_of - timedelta(days=7 * (k + 1))
        end = as_of - timedelta(days=7 * k)
        total = 0.0
        for e in entries:
            if not isinstance(e.get("hours"), (int, float)):
                continue
            try:
                d = datetime.fromisoformat(str(e.get("ts"))).date()
            except ValueError:
                continue
            if start <= d < end:
                total += e["hours"]
        periods.append(total)
    if any(p >= target for p in periods):
        return [
            f"capacity: addition rule (§9.6.3) — the most recently added live "
            f"product {latest['id']} has not run below its target load "
            f"({target:g}h/wk) for two consecutive weekly periods (measured "
            f"{periods[1]:g}h/wk then {periods[0]:g}h/wk, most recent last) — "
            f"starting {entering_id} before {latest['id']} stabilizes risks the "
            "zombie fleet"
        ]
    return []


def attention_kill_findings(root: Path, as_of: date | None = None) -> list[str]:
    """Attention kill criterion (#274 item 4, §9.6.3): a live product whose
    measured steady-state load is above ATTENTION_LOAD_THRESHOLD_HOURS while
    its (operator-entered) MRR is below ATTENTION_REVENUE_THRESHOLD_USD is a
    kill-or-redesign candidate — attention spent without being paid for. No
    `mrr_usd` recorded on a bet → silent for that bet (never guessed; a
    finding must come from data, not its absence). Never gates."""
    out = []
    for bet in all_bets(root):
        if bet.get("state") not in LIVE_STATES:
            continue
        mrr = bet.get("mrr_usd")
        if not isinstance(mrr, (int, float)):
            continue
        load = ongoing_load_hours_per_week(root, bet["id"], as_of)
        if load > ATTENTION_LOAD_THRESHOLD_HOURS and mrr < ATTENTION_REVENUE_THRESHOLD_USD:
            out.append(
                f"capacity: attention kill criterion (§9.6.3) — {bet['id']} costs "
                f"{load:g}h/wk (> {ATTENTION_LOAD_THRESHOLD_HOURS:g}h/wk) while earning "
                f"${mrr:g} MRR (< ${ATTENTION_REVENUE_THRESHOLD_USD:g}) — a kill-or-"
                "redesign candidate, attention spent without being paid for"
            )
    return out


# ---- reporting -----------------------------------------------------------------


def report(findings: list[str], gate: bool, label: str = "bet") -> int:
    """docs/gate-rollout.md discipline: findings print always; exit 1 only under
    --gate. Skipped checks (and unvalidated new #274 capacity findings — see
    NEVER_GATES_PREFIXES) are reported, never silently omitted, and never gate."""
    real = [f for f in findings if not f.startswith(NEVER_GATES_PREFIXES)]
    for f in findings:
        tag = "gated" if gate and not f.startswith(NEVER_GATES_PREFIXES) else "report-only"
        print(f"{label}: [{tag}] {f}")
    return 1 if gate and real else 0


# ---- subcommands ---------------------------------------------------------------


def cmd_create(args) -> int:
    root = portfolio_root(args.portfolio_dir)
    if not args.bet_id.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise BetError(f"bet id {args.bet_id!r} must be alphanumeric with ._- separators")
    bdir = bet_dir(root, args.bet_id)
    if (bdir / "bet.json").exists():
        raise BetError(f"bet {args.bet_id} already exists at {bdir}")

    env_path = Path(args.envelope)
    if not env_path.is_file():
        raise BetError(f"envelope file not found: {env_path}")
    try:
        envelope = json.loads(env_path.read_text())
    except json.JSONDecodeError as e:
        raise BetError(f"cannot parse {env_path}: {e}") from e
    if not isinstance(envelope, dict) or not isinstance(envelope.get("cash_cap_usd"), (int, float)):
        raise BetError(f"{env_path}: envelope needs a numeric cash_cap_usd (templates/bet-schema.json)")

    criteria = parse_criteria_file(Path(args.criteria))

    competitor_sweep = None
    if args.competitor_sweep:
        csp = Path(args.competitor_sweep)
        if not csp.is_file():
            raise BetError(f"competitor sweep file not found: {csp}")
        try:
            competitor_sweep = json.loads(csp.read_text())
        except json.JSONDecodeError as e:
            raise BetError(f"cannot parse {csp}: {e}") from e

    regulated_calculation_screen = None
    if args.regulated_calculation_screen:
        rcp = Path(args.regulated_calculation_screen)
        if not rcp.is_file():
            raise BetError(f"regulated-calculation screen file not found: {rcp}")
        try:
            regulated_calculation_screen = json.loads(rcp.read_text())
        except json.JSONDecodeError as e:
            raise BetError(f"cannot parse {rcp}: {e}") from e

    bet = {
        "id": args.bet_id,
        "title": args.title,
        "thesis": args.thesis or "",
        "created": now_iso(),
        "state": "proposed",
        "envelope": envelope,
        "acquisition": {
            "ecosystem_channel": args.ecosystem_channel,
            "owned_audience": args.owned_audience,
        },
        "means_refs": args.means_ref or [],
        "unlocked_milestones": [],
        "predecessor": args.predecessor,
        "successor": None,
    }
    if args.cadence is not None:
        if args.cadence <= 0:
            raise BetError("--cadence must be a positive reps-per-week count")
        bet["rep_cadence_per_week"] = args.cadence
    if args.target_cac is not None:
        bet["target_cac_usd"] = args.target_cac
    # getattr with a default (house precedent, e.g. _resolve_retire_cases in
    # ratchet.py): a hand-built Namespace predating this flag (the pivot path
    # in cmd_transition, above) must degrade gracefully, not AttributeError.
    if getattr(args, "low_cap_screens", None):
        lcs_path = Path(args.low_cap_screens)
        if not lcs_path.is_file():
            raise BetError(f"--low-cap-screens file not found: {lcs_path}")
        try:
            low_cap_screens = json.loads(lcs_path.read_text())
        except json.JSONDecodeError as e:
            raise BetError(f"cannot parse {lcs_path}: {e}") from e
        if not isinstance(low_cap_screens, dict):
            raise BetError(f"{lcs_path}: low-cap screens data must be a JSON object")
        bet["low_cap_screens"] = low_cap_screens
    if args.signal_tier is not None:
        bet["signal_tier"] = args.signal_tier
    if competitor_sweep is not None:
        bet["competitor_sweep"] = competitor_sweep
    if regulated_calculation_screen is not None:
        bet["regulated_calculation_screen"] = regulated_calculation_screen

    findings = [f"soundness: {f}" for f in criteria_soundness(criteria)]
    findings += [f"soundness: {f}" for f in envelope_soundness(envelope)]
    findings += [f"soundness: {f}" for f in liability_soundness(envelope)]
    if competitor_sweep is not None:
        findings += [f"soundness: {f}" for f in competitor_sweep_soundness(competitor_sweep)]
    if regulated_calculation_screen is not None:
        findings += [
            f"soundness: {f}" for f in regulated_calculation_soundness(regulated_calculation_screen)
        ]
    findings += [
        f if f.startswith("skipped:") else f"selection: {f}"
        for f in selection_lint(root, bet)
    ]
    # Low-cap distribution-divergence screens (#275, §9.6.5 screens 8-13) —
    # already self-prefixed 'screen: ' (NEVER_GATES_PREFIXES), no re-tagging.
    findings += low_cap_screen_findings(root, bet)
    findings += [
        f if f.startswith("skipped:") else f"signal: {f}"
        for f in competitor_sweep_findings(bet, date.today())
    ]
    findings += [f"screen: {f}" for f in regulated_calculation_findings(bet)]

    rc = report(findings, args.gate)
    if rc:
        print(f"bet: create {args.bet_id} REFUSED (--gate)")
        return rc

    bdir.mkdir(parents=True, exist_ok=True)
    save_bet(root, bet)
    (bdir / "kill-criteria.json").write_text(
        json.dumps({"criteria": criteria}, indent=2, sort_keys=True) + "\n"
    )
    ledger_path(root, args.bet_id).touch()
    append_event(root, "bet-create", args.bet_id, {
        "title": args.title,
        "envelope_hash": content_hash(envelope),
        "criteria_hash": content_hash(criteria),
        "predecessor": args.predecessor,
        # The effective cadence is journaled at create (#241 leg 3) — the
        # per-bet override is a decision, not a mutable knob.
        "rep_cadence_per_week": args.cadence if args.cadence is not None else DEFAULT_REP_CADENCE,
        "target_cac_usd": args.target_cac,
        "signal_tier": args.signal_tier,
    })
    print(
        f"bet: created {args.bet_id} [proposed] — envelope+criteria hashed into the "
        f"journal (goalposts; `rebaseline` is the only mutation path)"
    )
    return 0


def cmd_spend(args) -> int:
    root = portfolio_root(args.portfolio_dir)
    bet = load_bet(root, args.bet_id)
    if bet["state"] in TERMINALS:
        raise BetError(f"{args.bet_id} is terminal ({bet['state']}) — no further ledger entries")
    if args.amount_usd is None and args.hours is None and not args.rep:
        raise BetError("nothing to record — pass --amount-usd, --hours, and/or --rep")

    records = load_journal(root)
    findings = goalpost_findings(root, bet, records)

    pend = pending_kill(records, args.bet_id)
    if pend:
        crit = ", ".join((pend.get("details", {}) or {}).get("criteria", [])) or "?"
        findings.append(
            f"spend blocked pending kill decision — criterion(s) {crit} triggered "
            f"({pend['record_id']}); accept with `transition {args.bet_id} kill_pending` "
            f"or override with `transition {args.bet_id} --override-kill --reason ...`"
        )
    if bet["state"] == "kill_pending":
        findings.append(f"{args.bet_id} is kill_pending — spend awaits the kill decision")

    cash, hours = spend_totals(load_ledger(root, args.bet_id))
    new_cash = cash + (args.amount_usd or 0)
    new_hours = hours + (args.hours or 0)
    cap = unlocked_cap(bet)
    if new_cash > cap:
        findings.append(
            f"cumulative spend ${new_cash:g} exceeds cumulative unlocked tranches "
            f"${cap:g} — unlock a tranche (`transition --unlock-milestone`) or "
            "rebaseline the envelope (journaled)"
        )
    time_cap = bet.get("envelope", {}).get("time_cap_hours")
    if isinstance(time_cap, (int, float)) and new_hours > time_cap:
        findings.append(
            f"cumulative hours {new_hours:g} exceed time_cap_hours {time_cap:g}"
        )

    rc = report(findings, args.gate)
    if rc:
        print(f"bet: spend on {args.bet_id} REFUSED (--gate)")
        return rc

    entry = {
        "ts": now_iso(),
        "type": "rep" if args.rep else "spend",
        "amount_usd": args.amount_usd,
        "hours": args.hours,
        "note": args.note or "",
    }
    if args.tag:
        entry["tag"] = args.tag  # product|traction — feeds the 50% rule (#241)
    with ledger_path(root, args.bet_id).open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    print(
        f"bet: {entry['type']} recorded for {args.bet_id} — cash ${new_cash:g}/"
        f"${cap:g} unlocked (cap ${bet['envelope'].get('cash_cap_usd', 0):g}), "
        f"hours {new_hours:g}"
    )
    return 0


def cmd_evaluate(args) -> int:
    root = portfolio_root(args.portfolio_dir)
    bet = load_bet(root, args.bet_id)
    records = load_journal(root)
    findings = goalpost_findings(root, bet, records)
    criteria = load_criteria(root, args.bet_id)

    results = {}
    if args.results:
        rp = Path(args.results)
        if not rp.is_file():
            raise BetError(f"results file not found: {rp}")
        try:
            results = json.loads(rp.read_text())
        except json.JSONDecodeError as e:
            raise BetError(f"cannot parse {rp}: {e}") from e
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()

    rows = evaluate_criteria(criteria, results, as_of)
    print(f"bet: evaluate {args.bet_id} [{bet['state']}] as of {as_of.isoformat()}")
    for r in rows:
        measured = "unmeasured" if r["measured"] is None else f"measured={r['measured']:g}"
        line = f"  {r['id']} {r['metric']}: {r['status'].upper()} ({measured})"
        if r.get("reason"):
            line += f" — {r['reason']}"
        print(line)

    dist = distribution_status(root, args.bet_id)
    by = dist.get("channels_by_status") or {}
    chan_note = ""
    if by:
        order = ("brainstormed", "ranked", "testing", "focused", "rejected")
        chan_note = "; channels: " + ", ".join(
            f"{by[st]} {st}" for st in order if by.get(st)
        )
    print(
        f"  distribution: {dist['status']} "
        f"({dist['channel_experiments']} channel experiment(s), "
        f"{dist['rep_entries']} rep entr{'y' if dist['rep_entries'] == 1 else 'ies'}"
        f"{chan_note})"
    )
    # #241 leg 3: the doing-gap checks surface inside the distribution block —
    # rep cadence while probing|validating, and the *Traction* 50% rule.
    entries = load_ledger(root, args.bet_id)
    cad = rep_cadence_status(bet, entries, as_of)
    if cad is not None:
        print(f"  rep cadence: {cad['line']}")
        findings += cad["findings"]
    findings += traction_findings(bet, entries)

    triggered = [r["id"] for r in rows if r["status"] == "triggered"]
    if triggered:
        findings.append(
            f"criterion(s) triggered: {', '.join(triggered)} — kill_pending proposed; "
            "further spend is blocked pending the journaled human accept/override"
        )
        if dist["status"] == "unattempted":
            print(
                "  note: triggered with distribution UNATTEMPTED — evidence of no "
                "marketing, not no demand; the #237 verdict rule downgrades kill "
                "to recycle"
            )
        if pending_kill(records, args.bet_id) is None and bet["state"] not in TERMINALS:
            # The full evaluation rows ride along so the #237 kill brief can
            # cite measured values to this record id — journal-backed, never prose.
            rec = append_event(root, "kill-proposed", args.bet_id, {
                "criteria": triggered,
                "as_of": as_of.isoformat(),
                "distribution": dist,
                "rows": rows,
            })
            print(f"  journaled kill-proposed ({rec['record_id']})")
        else:
            print("  kill proposal already pending — not re-journaled")
        # Trigger point (#237 decision 5): a proposed kill RECOMMENDS the
        # fresh-context quorum; nothing runs it automatically.
        print(
            f"  next: convene the fresh-context kill review — "
            f"`bet.py kill-review {args.bet_id}` (#237)"
        )
    return report(findings, args.gate)


def cmd_finding(args) -> int:
    """Record a material external finding (chief-wiggum#252) — a citable fact
    bearing on the bet, distinct from an assumption (a claim under test) or a
    measurement (a criterion's value). Malformed (no statement, no --source-url,
    or an unrecognized --bearing-on) is a hard usage error (exit 2), the same
    discipline as a states-and-dates criterion missing its date: a finding without
    a source is not a finding, it is unsourced prose, and the whole point of this
    record type is that `kill-brief` can cite it without weakening brief purity."""
    root = portfolio_root(args.portfolio_dir)
    load_bet(root, args.bet_id)  # existence check; raises BetError if unknown
    if not args.statement or not args.statement.strip():
        raise BetError("finding needs a non-empty --statement")
    if not args.source_url or not args.source_url.strip():
        raise BetError(
            "finding is malformed without a --source-url — an external finding "
            "with no citable source cannot enter the record (brief-purity discipline)"
        )
    if not args.bearing_on or not FINDING_BEARING_ON_RE.match(args.bearing_on):
        raise BetError(
            f"--bearing-on must be 'premise', an ASM-<id>, or a KC-<id>, "
            f"got {args.bearing_on!r}"
        )
    grade = args.evidence_grade or "reported"
    rec = append_event(root, "finding", args.bet_id, {
        "statement": args.statement,
        "source_url": args.source_url,
        "bearing_on": args.bearing_on,
        "evidence_grade": grade,
    })
    print(
        f"bet: finding recorded for {args.bet_id} ({rec['record_id']}) — "
        f"bears on {args.bearing_on}, evidence_grade={grade}"
    )
    return 0


def cmd_kill_brief(args) -> int:
    root = portfolio_root(args.portfolio_dir)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    text, findings, _meta = build_kill_brief(root, args.bet_id, as_of, getattr(args, "trigger", None))
    if findings:
        for f in findings:
            print(f"bet: [purity] {f}")
        print(
            f"bet: kill-brief {args.bet_id} REFUSED — the brief generator emits "
            "only journal-backed values (hard self-check, not a --gate)"
        )
        return 1
    out_path = Path(args.output) if args.output else bet_dir(root, args.bet_id) / BRIEF_NAME
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    print(text)
    print(f"bet: kill brief written to {out_path}")
    return 0


def cmd_kill_review(args) -> int:
    root = portfolio_root(args.portfolio_dir)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    text, findings, meta = build_kill_brief(root, args.bet_id, as_of, getattr(args, "trigger", None))
    if findings:
        for f in findings:
            print(f"bet: [purity] {f}")
        print(
            f"bet: kill-review {args.bet_id} REFUSED — cannot convene a quorum on "
            "a brief that fails the purity self-check"
        )
        return 1
    brief_path = bet_dir(root, args.bet_id) / BRIEF_NAME
    brief_path.write_text(text)

    out_dir = Path(args.output_dir) if args.output_dir else bet_dir(root, args.bet_id) / KILL_REVIEW_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    # CW_CONSULT_AI overrides the consult entrypoint — the test seam (fixture
    # verdict files instead of real providers); the argv contract is identical.
    consult = os.environ.get("CW_CONSULT_AI") or str(
        Path(__file__).resolve().parent / "consult_ai.py"
    )
    proc = subprocess.run(
        [sys.executable, consult, "--role", KILL_REVIEW_ROLE, str(brief_path),
         "--output-dir", str(out_dir)],
        capture_output=True, text=True,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        print(
            f"bet: kill-review {args.bet_id} — quorum FAILED (a required provider "
            "did not answer); no verdicts journaled"
        )
        return 1

    verdicts, parse_findings = collect_verdicts(out_dir)
    if not verdicts:
        print(f"bet: kill-review {args.bet_id} — quorum produced no verdict files; nothing journaled")
        return 1
    downgraded = apply_fairness(
        verdicts, meta["fired_demand"], meta["distribution"]["status"], meta["exposure"]
    )
    rec = append_event(root, "kill-review", args.bet_id, {
        "brief_hash": stable_hash(text),
        "brief_path": f"bets/{args.bet_id}/{BRIEF_NAME}",
        "as_of": meta["as_of"],
        "fired_criteria": meta["fired"],
        "distribution_status": meta["distribution"]["status"],
        "verdicts": verdicts,
        "fairness_downgraded": downgraded,
    })

    # Ordering invariant (#237 decision 4): the human reads the fresh verdicts
    # BEFORE the accept/override instructions — the verdict anchors the decision.
    print(f"bet: kill-review {args.bet_id} — fresh-context quorum verdicts (read these first):")
    for v in verdicts:
        if v.get("malformed"):
            print(f"  {v['provider']}: MALFORMED verdict output (flagged, not counted)")
            continue
        conf = (
            f" (confidence {v['confidence']:g})"
            if isinstance(v.get("confidence"), (int, float)) else ""
        )
        note = (
            f" — downgraded from `{v['downgraded_from']}` by the distribution-fairness rule"
            if v.get("downgraded_from") else ""
        )
        print(f"  {v['provider']}: {v['verdict'].upper()}{conf}{note}")
        for r in v.get("reasons", []):
            print(f"    - {r}")
        if v.get("cheapest_disconfirming_test"):
            print(f"    cheapest disconfirming test: {v['cheapest_disconfirming_test']}")
        if v.get("distribution_finding"):
            print(f"    distribution finding: {v['distribution_finding']}")
    print(f"  verdicts + brief hash journaled ({rec['record_id']})")
    print("bet: your decision (a journaled act — decide AFTER reading the verdicts above):")
    print(f"  accept the kill:   `bet.py transition {args.bet_id} kill_pending --verdict kill`")
    print(f"  override the kill: `bet.py transition {args.bet_id} --override-kill --reason ...`")
    return report(parse_findings, args.gate)


def _guard_transition(bet: dict, to: str, override: bool) -> None:
    cur = bet["state"]
    if cur in TERMINALS:
        raise BetError(f"{bet['id']} is terminal ({cur}) — no further transitions")
    if to == "kill_pending":
        return
    if to in TERMINALS:
        return
    if cur == "kill_pending":
        if not override:
            raise BetError(
                f"{bet['id']} is kill_pending — resuming {to} requires "
                "--override-kill --reason (a journaled act)"
            )
        return
    if cur in ACTIVE_ORDER and to in ACTIVE_ORDER:
        if ACTIVE_ORDER.index(to) == ACTIVE_ORDER.index(cur) + 1:
            return
        raise BetError(
            f"invalid transition {cur} → {to}: forward moves are adjacent "
            f"({' → '.join(ACTIVE_ORDER)}); kill_pending and terminals "
            f"({'|'.join(sorted(TERMINALS))}) are reachable from anywhere"
        )
    raise BetError(f"invalid transition {cur} → {to}")


def cmd_transition(args) -> int:
    root = portfolio_root(args.portfolio_dir)
    bet = load_bet(root, args.bet_id)
    records = load_journal(root)
    findings = goalpost_findings(root, bet, records)
    to = args.new_state

    if args.verdict and args.verdict not in VERDICTS:
        raise BetError(f"--verdict must be one of {VERDICTS}")
    if args.successor and to != "killed":
        raise BetError("--successor (pivot) closes the bet: the target state must be `killed`")
    if args.successor and (not args.envelope or not args.criteria):
        raise BetError("a pivot successor needs fresh --envelope and --criteria files")
    if args.changed_elements and not args.successor:
        raise BetError("--changed-elements applies to a pivot — pass --successor")

    # A stateless journaled act: unlock a tranche and/or override a pending kill.
    if to is None:
        if not args.unlock_milestone and not args.override_kill:
            raise BetError(
                "pass a new state, or --unlock-milestone / --override-kill for a "
                "stateless journaled act"
            )
        rc = report(findings, args.gate)
        if rc:
            print(f"bet: stateless act on {args.bet_id} REFUSED (--gate)")
            return rc
        if args.unlock_milestone:
            milestones = {
                t.get("unlock_milestone_id")
                for t in bet.get("envelope", {}).get("tranches") or []
                if isinstance(t, dict)
            }
            if args.unlock_milestone not in milestones:
                raise BetError(
                    f"no tranche unlocks on milestone {args.unlock_milestone!r} "
                    f"(tranche milestones: {sorted(m for m in milestones if m)})"
                )
            unlocked = bet.setdefault("unlocked_milestones", [])
            if args.unlock_milestone not in unlocked:
                unlocked.append(args.unlock_milestone)
            save_bet(root, bet)
            append_event(root, "tranche-unlock", args.bet_id, {
                "milestone": args.unlock_milestone, "reason": args.reason or "",
            })
            print(
                f"bet: unlocked milestone {args.unlock_milestone} on {args.bet_id} — "
                f"${unlocked_cap(bet):g} now unlocked"
            )
        if args.override_kill:
            if not args.reason:
                raise BetError("--override-kill requires --reason (overrides are costly and journaled)")
            if pending_kill(records, args.bet_id) is None:
                raise BetError(f"no pending kill proposal on {args.bet_id} to override")
            rec = append_event(root, "kill-override", args.bet_id, {"reason": args.reason})
            print(
                f"bet: kill proposal on {args.bet_id} OVERRIDDEN ({rec['record_id']}) — "
                f"reason journaled: {args.reason}"
            )
        return 0

    if to not in ALL_STATES:
        raise BetError(f"unknown state {to!r} (states: {', '.join(sorted(ALL_STATES))})")
    if args.override_kill and not args.reason:
        raise BetError("--override-kill requires --reason (overrides are costly and journaled)")
    _guard_transition(bet, to, args.override_kill)

    harvest = None
    if to == "killed":
        # Hard guards — state-machine integrity, not precision-risk gates:
        # `killed` is blocked until the retrospective exists non-trivially and
        # the harvest check RAN (it runs right here; skipped ≠ blocked).
        if not retrospective_nontrivial(root, args.bet_id):
            sys.stderr.write(
                f"bet: BLOCKED — {args.bet_id} cannot transition to killed until "
                f"bets/{args.bet_id}/retrospective.md exists non-trivially "
                f"(≥{RETRO_MIN_CHARS} chars of prose; harvest discipline — grade the "
                "process, not the outcome)\n"
            )
            return 1
        harvest = harvest_check(bet)
        if "skipped" in harvest:
            print(f"bet: harvest check skipped — {harvest['skipped']}")
        elif harvest["proposed_terminal"] == "sold":
            findings.append(
                f"harvest check: est sale value ${harvest['est_sale_value_usd']:g} "
                f"(3.9 × TTM SDP) > wind-down cost ${harvest['wind_down_cost_usd']:g} "
                "— the proposed terminal is `sold`, not `killed`"
            )
        else:
            print(
                f"bet: harvest check — est sale value ${harvest['est_sale_value_usd']:g} "
                f"≤ wind-down cost ${harvest['wind_down_cost_usd']:g}; kill stands"
            )

    if to in IN_FLIGHT and bet["state"] not in IN_FLIGHT:
        findings += cap_findings(root, args.max_in_flight, entering=args.bet_id)
        # #274: a second, independent bound — the live fleet's measured
        # attention accounting and the §9.6.3 addition rule — visible
        # alongside the integer cap; whichever binds first is seen.
        findings += capacity_findings(root)
        findings += addition_rule_findings(root, args.bet_id)

    if to == "building":
        # Evidence-strength floor (#236): ≥1 validated assumption at strength ≥4
        # (reputation/money). assumption.py owns the logic; the import is
        # deferred to avoid a module cycle (assumption.py imports bet).
        import assumption as asmlib
        findings += asmlib.building_floor_findings(root, args.bet_id)

    rc = report(findings, args.gate)
    if rc:
        print(f"bet: transition {args.bet_id} → {to} REFUSED (--gate)")
        return rc

    if args.override_kill and pending_kill(records, args.bet_id):
        append_event(root, "kill-override", args.bet_id, {"reason": args.reason})

    prev = bet["state"]
    bet["state"] = to
    details = {
        "from": prev, "to": to,
        "verdict": args.verdict, "reason": args.reason or "",
    }
    if harvest is not None:
        details["harvest"] = harvest
    successor_note = ""
    if args.successor:
        # Pivot: close this bet honestly (criteria evaluated against the OLD
        # thesis belong in the retrospective + evaluate history) and open a
        # successor with FRESH envelope + criteria — never an edit.
        if not args.envelope or not args.criteria:
            raise BetError("a pivot successor needs fresh --envelope and --criteria files")
        details["successor"] = args.successor
        bet["successor"] = args.successor
        save_bet(root, bet)
        append_event(root, "transition", args.bet_id, details)
        sub = argparse.Namespace(
            portfolio_dir=args.portfolio_dir, bet_id=args.successor,
            title=args.successor_title or f"pivot of {args.bet_id}",
            thesis=args.successor_thesis,
            envelope=args.envelope, criteria=args.criteria,
            ecosystem_channel=None, owned_audience=None,
            means_ref=None, predecessor=args.bet_id, gate=args.gate,
            cadence=None, target_cac=None, low_cap_screens=None,
            # A pivot's successor re-derives its own signal grounding (#254) and
            # regulated-calculation screen (#260) — neither carries over
            # automatically from the closed bet.
            signal_tier=None, competitor_sweep=None,
            regulated_calculation_screen=None,
        )
        rc = cmd_create(sub)
        if rc:
            return rc
        successor_note = f"; successor {args.successor} opened with fresh envelope+criteria"
        if args.changed_elements:
            # Bland's dependency rule (#236): the successor inherits the
            # assumption ledger with every validated ASM tagged to a changed
            # canvas element re-opened (validated → untested), journaled.
            import assumption as asmlib
            changed = [e for arg in args.changed_elements for e in arg.split(",")]
            summary = asmlib.pivot_reopen(root, args.bet_id, args.successor, changed)
            if summary is None:
                print(f"bet: no assumptions.json on {args.bet_id} — nothing to re-open")
            else:
                names = ", ".join(summary["reopened"]) or "none"
                print(
                    f"bet: pivot re-opened {len(summary['reopened'])} assumption(s) "
                    f"({names}) in {args.successor} — changed element(s) invalidate "
                    f"dependent validation; {summary['carried']} carried "
                    f"({summary['record_id']})"
                )
    else:
        save_bet(root, bet)
        append_event(root, "transition", args.bet_id, details)
    verdict = f" verdict={args.verdict}" if args.verdict else ""
    print(f"bet: {args.bet_id} {prev} → {to}{verdict}{successor_note}")
    return 0


def cmd_rebaseline(args) -> int:
    root = portfolio_root(args.portfolio_dir)
    bet = load_bet(root, args.bet_id)
    if bet["state"] in TERMINALS:
        raise BetError(f"{args.bet_id} is terminal ({bet['state']}) — goalposts are frozen")
    if not args.envelope and not args.criteria:
        raise BetError("nothing to rebaseline — pass --envelope and/or --criteria")

    records = load_journal(root)
    for f in goalpost_findings(root, bet, records):
        # Surface a pre-existing hand edit; the rebaseline heals it FROM the
        # journaled baseline, so the tamper stays visible in the chain.
        print(f"bet: [report-only] {f} — rebaselining over it (journaled)")
    old_env_h, old_crit_h = goalpost_baseline(records, args.bet_id)

    details = {"reason": args.reason}
    findings = []
    if args.envelope:
        env_path = Path(args.envelope)
        if not env_path.is_file():
            raise BetError(f"envelope file not found: {env_path}")
        try:
            envelope = json.loads(env_path.read_text())
        except json.JSONDecodeError as e:
            raise BetError(f"cannot parse {env_path}: {e}") from e
        if not isinstance(envelope.get("cash_cap_usd"), (int, float)):
            raise BetError(f"{env_path}: envelope needs a numeric cash_cap_usd")
        findings += [f"soundness: {f}" for f in envelope_soundness(envelope)]
        findings += [f"soundness: {f}" for f in liability_soundness(envelope)]
        details["old_envelope_hash"] = old_env_h
        details["new_envelope_hash"] = content_hash(envelope)
    if args.criteria:
        criteria = parse_criteria_file(Path(args.criteria))
        findings += [f"soundness: {f}" for f in criteria_soundness(criteria)]
        details["old_criteria_hash"] = old_crit_h
        details["new_criteria_hash"] = content_hash(criteria)

    rc = report(findings, args.gate)
    if rc:
        print(f"bet: rebaseline {args.bet_id} REFUSED (--gate)")
        return rc

    if args.envelope:
        bet["envelope"] = envelope
        save_bet(root, bet)
    if args.criteria:
        (bet_dir(root, args.bet_id) / "kill-criteria.json").write_text(
            json.dumps({"criteria": criteria}, indent=2, sort_keys=True) + "\n"
        )
    rec = append_event(root, "rebaseline", args.bet_id, details)
    moved = " + ".join(
        n for n, present in (("envelope", args.envelope), ("kill criteria", args.criteria)) if present
    )
    print(
        f"bet: rebaselined {moved} for {args.bet_id} ({rec['record_id']}) — old→new "
        f"hashes journaled; reason: {args.reason}"
    )
    return 0


def _next_due(criteria: list[dict], today: date) -> str:
    dates = []
    for c in criteria:
        try:
            dates.append(date.fromisoformat(str(c.get("by_date"))))
        except ValueError:
            continue
    if not dates:
        return "no dated criteria"
    nxt = min(dates)
    days = (nxt - today).days
    return f"{days}d to {nxt.isoformat()}" if days >= 0 else f"OVERDUE {-days}d ({nxt.isoformat()})"


def cmd_portfolio(args) -> int:
    root = portfolio_root(args.portfolio_dir)
    records = load_journal(root)
    bets = all_bets(root)
    today = date.today()
    # Build-cost tracking (#257): deferred import, same module-cycle-avoidance
    # shape as the assumption.py imports elsewhere in this file.
    import build_cost

    findings: list[str] = []
    rows = []
    for bet in bets:
        bid = bet["id"]
        findings += goalpost_findings(root, bet, records)
        cash, hours = spend_totals(load_ledger(root, bid))
        cap = unlocked_cap(bet)
        env = bet.get("envelope", {})
        if cash > cap and bet["state"] not in TERMINALS:
            findings.append(
                f"{bid}: cumulative spend ${cash:g} exceeds unlocked tranches ${cap:g}"
            )
        try:
            criteria = load_criteria(root, bid)
        except BetError:
            criteria = []
        pend = pending_kill(records, bid)
        bc_summary = build_cost.summarize(build_cost.load_build_costs(root, bid))
        findings += build_cost_findings(bet, bc_summary)
        rows.append({
            "id": bid,
            "state": bet["state"],
            "spend_usd": cash,
            "unlocked_usd": cap,
            "cash_cap_usd": env.get("cash_cap_usd", 0),
            "hours": hours,
            "next_criterion": _next_due(criteria, today),
            "distribution": distribution_status(root, bid)["status"],
            "kill_pending_proposal": bool(pend),
            "build_cost": bc_summary,
            # #274: measured (never guessed) ongoing load — only meaningful
            # for a LIVE_STATES (lifestyle) bet, the zombie-fleet population.
            "ongoing_load_hours_per_week": (
                ongoing_load_hours_per_week(root, bid, today)
                if bet["state"] in LIVE_STATES else None
            ),
        })

    findings += cap_findings(root, args.max_in_flight)
    findings += capacity_findings(root, today)
    findings += attention_kill_findings(root, today)
    attention = attention_capacity(root, today)
    liab = liability_concurrency(root)

    # Loss distribution + kill hygiene per DEAD bet — process accountability
    # (Simonson & Staw), never a per-bet win/lose ranking.
    dead = []
    for bet in bets:
        if bet["state"] != "killed":
            continue
        bid = bet["id"]
        cash, _ = spend_totals(load_ledger(root, bid))
        env_cap = bet.get("envelope", {}).get("cash_cap_usd", 0)
        proposed = next((r for r in bet_events(records, bid) if r["event"] == "kill-proposed"), None)
        closed = next(
            (r for r in reversed(bet_events(records, bid))
             if r["event"] == "transition" and (r.get("details", {}) or {}).get("to") in TERMINALS),
            None,
        )
        latency = "self-initiated (no trigger)"
        if proposed and closed:
            try:
                d0 = datetime.fromisoformat(proposed["ts"])
                d1 = datetime.fromisoformat(closed["ts"])
                latency = f"{(d1 - d0).days}d from trigger to close"
            except ValueError:
                latency = "unknown"
        dead.append({
            "id": bid, "loss_usd": cash,
            "envelope_respected": cash <= env_cap,
            "kill_on_trigger": latency,
        })

    # Build-cost buckets outside any single bet — 'factory' (CW's own dev) and
    # 'unattributed' — reported explicitly, never dropped or spread pro-rata (#257).
    bc_portfolio = build_cost.portfolio_summary(root)
    bc_other = {
        k: v for k, v in bc_portfolio.items()
        if k in (build_cost.FACTORY, build_cost.UNATTRIBUTED)
    }

    if args.format == "json":
        print(json.dumps({
            "bets": rows,
            "in_flight": in_flight_bets(root),
            "max_in_flight": args.max_in_flight,
            "dead_bets": dead,
            "attention": attention,
            "liability_concurrency": liab,
            "build_cost_other": bc_other,
            "findings": findings,
        }, indent=2))
        real = [f for f in findings if not f.startswith(NEVER_GATES_PREFIXES)]
        return 1 if args.gate and real else 0

    print(f"portfolio: {root}")
    print(f"  bets: {len(bets)} — in flight: {len(in_flight_bets(root))}/{args.max_in_flight} "
          f"({', '.join(in_flight_bets(root)) or 'none'})")
    if attention is not None:
        # #274 zombie-fleet visibility: shown always, not only when exhausted —
        # the whole point is that the live fleet's attention draw is COUNTED.
        print(
            f"  attention: {attention['hours_per_week']:g}h/wk available − "
            f"{attention['total_load_hours_per_week']:g}h/wk live-product load "
            f"({len(attention['live_loads'])} lifestyle bet(s)) − "
            f"{attention['reserve_hours_per_week']:g}h/wk reserve = "
            f"{attention['remaining_hours_per_week']:g}h/wk remaining"
        )
    print(
        f"  uncapped liability: {liab['count']} bet(s) currently carrying an "
        f"uncapped exposure ({', '.join(liab['bets']) or 'none'}) — a considered "
        "bet alone, a portfolio-survival question concurrently (#277)"
    )
    for r in rows:
        pend = "  [KILL PROPOSAL PENDING]" if r["kill_pending_proposal"] else ""
        load_note = (
            f"; ongoing load: {r['ongoing_load_hours_per_week']:g}h/wk"
            if r["ongoing_load_hours_per_week"] is not None else ""
        )
        bc = r["build_cost"]
        nominal = f"${bc['nominal_usd']:g}" if bc["nominal_usd"] is not None else "{unresolved}"
        share = f"{bc['plan_share_pct']:g}%" if bc["plan_share_pct"] is not None else "{unresolved}"
        build_note = f"; build cost: nominal {nominal}, plan-share {share}" if bc["records"] else ""
        print(
            f"  {r['id']:24s} {r['state']:12s} spend ${r['spend_usd']:g}/"
            f"${r['unlocked_usd']:g} unlocked (cap ${r['cash_cap_usd']:g}), "
            f"{r['hours']:g}h; next criterion: {r['next_criterion']}; "
            f"distribution: {r['distribution']}{load_note}{build_note}{pend}"
        )
    if bc_other:
        for bucket, s in bc_other.items():
            nominal = f"${s['nominal_usd']:g}" if s["nominal_usd"] is not None else "{unresolved}"
            share = f"{s['plan_share_pct']:g}%" if s["plan_share_pct"] is not None else "{unresolved}"
            print(f"  build cost [{bucket}]: {s['records']} record(s), nominal {nominal}, "
                  f"plan-share {share} — never spread pro-rata across bets (#257)")
    if dead:
        losses = sorted(d["loss_usd"] for d in dead)
        median = losses[len(losses) // 2] if len(losses) % 2 else \
            (losses[len(losses) // 2 - 1] + losses[len(losses) // 2]) / 2
        print(f"  dead bets: {len(dead)} — loss median ${median:g}, max ${max(losses):g} "
              "(loss distribution + kill hygiene, never win/lose ranking)")
        for d in dead:
            resp = "respected" if d["envelope_respected"] else "EXCEEDED"
            print(f"    {d['id']}: loss ${d['loss_usd']:g}, envelope {resp}, "
                  f"kill hygiene: {d['kill_on_trigger']}")
    return report(findings, args.gate)


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

    sp = sub.add_parser("create", help="register a bet (envelope+criteria hashed as goalposts)")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("--title", required=True)
    sp.add_argument("--thesis", default="")
    sp.add_argument("--envelope", required=True, metavar="JSON",
                    help="affordable-loss envelope file (templates/bet-schema.json $defs.envelope)")
    sp.add_argument("--criteria", required=True, metavar="JSON",
                    help="states-and-dates kill criteria file (templates/kill-criteria-schema.json)")
    sp.add_argument("--ecosystem-channel", default=None,
                    help="built-in-distribution ecosystem channel the acquisition plan ships into")
    sp.add_argument("--owned-audience", default=None,
                    help="owned audience the acquisition plan draws on")
    sp.add_argument("--means-ref", action="append", metavar="REF",
                    help="means.json entry this bet draws on (repeatable — bird-in-hand)")
    sp.add_argument("--cadence", type=float, default=None, metavar="N",
                    help=f"Mom-Test conversations/week required while probing|validating "
                         f"(default {DEFAULT_REP_CADENCE}; journaled at create — #241)")
    sp.add_argument("--target-cac", type=float, default=None, metavar="USD",
                    help="target CAC the channel engine joins measured channel-CAC "
                         "against (absent → the join reports skipped — #241)")
    sp.add_argument("--low-cap-screens", default=None, metavar="JSON",
                    help="operator-entered facts for the six low-cap distribution-divergence "
                         "screens (templates/bet-schema.json low_cap_screens — #275, "
                         "docs/business-factory.md §9.6.5 screens 8-13); absent fields are "
                         "UNRESOLVED findings (report-only), never a silent pass")
    sp.add_argument("--signal-tier", default=None, choices=SIGNAL_TIERS,
                    help="how contestable this bet's grounding signal is (chief-wiggum#254): "
                         "A=private (inbound/network/observation), B=semi-public (paid "
                         "data/gated communities), C=public (feature boards/reviews/forums — "
                         "contested by construction; assume a competitor reads it too)")
    sp.add_argument("--competitor-sweep", default=None, metavar="JSON",
                    help="create-time competitor sweep file: {date, sources[], "
                         "competitors[{name,url}], unresolved[]} — required for a Tier-C "
                         "bet to avoid the not-run-until-name-pick-time failure (#254)")
    sp.add_argument("--regulated-calculation-screen", default=None, metavar="JSON",
                    help="standing screen 15 (chief-wiggum#260) file: {who_bears_error, "
                         "correctness_winnable, insurable, paid_configuration, "
                         "interpretation_surface} — flagged report-only when the thesis "
                         "names a regulated-calculation domain and this is absent")
    sp.add_argument("--predecessor", default=None, help=argparse.SUPPRESS)

    sp = sub.add_parser("spend", help="append a spend/time/rep ledger entry")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("--amount-usd", type=float, default=None)
    sp.add_argument("--hours", type=float, default=None)
    sp.add_argument("--rep", action="store_true",
                    help="a distribution rep (outreach/channel work) — counts toward "
                         "distribution-attempted, not cash spend")
    sp.add_argument("--tag", default=None, choices=LEDGER_TAGS,
                    help="time-allocation tag on hours — feeds the *Traction* 50% "
                         "rule (#241; untagged hours are never a finding)")
    sp.add_argument("--note", default="")

    sp = sub.add_parser("evaluate", help="evaluate dated kill criteria + distribution status")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("--results", default=None, metavar="JSON",
                    help="measured values file: {metric: value}; absent metrics are UNMEASURED")
    sp.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                    help="evaluation date (default: today)")

    sp = sub.add_parser(
        "finding",
        help="record a material external finding bearing on the bet's premise, an "
             "assumption, or a criterion (chief-wiggum#252) — journaled, cited in kill-brief",
    )
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("--statement", required=True, help="the external, citable fact")
    sp.add_argument("--source-url", required=True, metavar="URL",
                    help="citable source — required; a finding without one is refused")
    sp.add_argument("--bearing-on", required=True, metavar="ASM-id|premise|KC-id",
                    help="what this finding bears on: 'premise', an ASM-<id>, or a KC-<id>")
    sp.add_argument("--evidence-grade", default=None, choices=EVIDENCE_GRADES,
                    help="how well-sourced the finding itself is (default: reported)")

    sp = sub.add_parser(
        "kill-brief",
        help="render the fresh-context kill brief (journal-backed values only — #237)",
    )
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                    help="brief date for the rep-cadence window (default: today)")
    sp.add_argument("--output", default=None, metavar="FILE",
                    help=f"write the brief here (default: bets/<id>/{BRIEF_NAME})")
    sp.add_argument("--trigger", default=None, choices=KILL_REVIEW_TRIGGERS,
                    help="which legitimate kill-review trigger convenes this brief "
                         "(chief-wiggum#252); default: auto-detected from a fired "
                         "criterion or a premise-bearing finding, 'criterion' otherwise")

    sp = sub.add_parser(
        "kill-review",
        help="generate the brief, run the kill-review quorum via consult_ai.py "
             "--role, journal verdicts + brief hash (#237)",
    )
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("--as-of", default=None, metavar="YYYY-MM-DD")
    sp.add_argument("--output-dir", default=None, metavar="DIR",
                    help=f"provider verdict files land here (default: bets/<id>/{KILL_REVIEW_DIR}/)")
    sp.add_argument("--trigger", default=None, choices=KILL_REVIEW_TRIGGERS,
                    help="which legitimate kill-review trigger convenes this brief "
                         "(chief-wiggum#252); default: auto-detected")

    sp = sub.add_parser(
        "transition",
        help="move the state machine (pivot via --successor; kill accept/override; "
             "tranche unlock) — every one a journaled act",
    )
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("new_state", nargs="?", default=None,
                    help=f"target state ({', '.join(sorted(ALL_STATES))}); omit for a "
                         "stateless act (--unlock-milestone / --override-kill)")
    sp.add_argument("--verdict", default=None, choices=VERDICTS,
                    help="gate verdict recorded with the transition (Cooper vocabulary)")
    sp.add_argument("--reason", default=None)
    sp.add_argument("--override-kill", action="store_true",
                    help="override a pending kill proposal (journaled; requires --reason)")
    sp.add_argument("--unlock-milestone", default=None, metavar="ID",
                    help="record a tranche-unlocking milestone as reached (journaled)")
    sp.add_argument("--successor", default=None, metavar="BET_ID",
                    help="pivot: close this bet (state must be `killed`) and open a "
                         "successor with fresh --envelope/--criteria")
    sp.add_argument("--envelope", default=None, metavar="JSON",
                    help="successor's fresh envelope (pivot only)")
    sp.add_argument("--criteria", default=None, metavar="JSON",
                    help="successor's fresh kill criteria (pivot only)")
    sp.add_argument("--successor-title", default=None)
    sp.add_argument("--successor-thesis", default="")
    sp.add_argument("--changed-elements", action="append", default=None,
                    metavar="ELEMENT[,ELEMENT...]",
                    help="pivot only: canvas element(s) the pivot changes — every "
                         "validated assumption tagged depends_on_element to one of "
                         "them re-opens (validated → untested) in the successor "
                         "(Bland's dependency rule, #236)")
    sp.add_argument("--max-in-flight", type=int, default=DEFAULT_MAX_IN_FLIGHT,
                    help=f"bets-in-flight cap over probing|validating|building "
                         f"(default {DEFAULT_MAX_IN_FLIGHT})")

    sp = sub.add_parser("rebaseline",
                        help="the ONLY mutation path for envelope/kill criteria (journaled)")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("--envelope", default=None, metavar="JSON")
    sp.add_argument("--criteria", default=None, metavar="JSON")
    sp.add_argument("--reason", required=True,
                    help="why the goalposts move — journaled with old→new hashes")

    sp = sub.add_parser("portfolio", help="portfolio summary + invariants")
    common(sp)
    sp.add_argument("--max-in-flight", type=int, default=DEFAULT_MAX_IN_FLIGHT)
    sp.add_argument("--format", choices=["text", "json"], default="text")

    args = p.parse_args()
    dispatch = {
        "create": cmd_create, "spend": cmd_spend, "evaluate": cmd_evaluate,
        "finding": cmd_finding,
        "kill-brief": cmd_kill_brief, "kill-review": cmd_kill_review,
        "transition": cmd_transition, "rebaseline": cmd_rebaseline,
        "portfolio": cmd_portfolio,
    }
    try:
        return dispatch[args.cmd](args)
    except BetError as e:
        sys.stderr.write(f"bet: {e}\n")
        return 2
    except TamperError as e:
        sys.stderr.write(f"bet: {e}\n")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
