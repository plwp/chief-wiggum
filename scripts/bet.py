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
    terminals: killed | parked | lifestyle | sold

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

Subcommands:
    create      register a bet: envelope + kill criteria hashed into the journal
    spend       append a ledger entry (spend/time or a distribution rep)
    evaluate    evaluate dated criteria + report distribution-attempted status
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
import subprocess
import sys
from datetime import date, datetime, timezone
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
TERMINALS = {"killed", "parked", "lifestyle", "sold"}
ALL_STATES = set(ACTIVE_ORDER) | {"kill_pending"} | TERMINALS
VERDICTS = ("go", "kill", "hold", "recycle")
COMPARATORS = {"<", "<=", ">", ">=", "==", "!="}
DEFAULT_MAX_IN_FLIGHT = 2

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


# ---- reporting -----------------------------------------------------------------


def report(findings: list[str], gate: bool, label: str = "bet") -> int:
    """docs/gate-rollout.md discipline: findings print always; exit 1 only under
    --gate. Skipped checks are reported, never silently omitted."""
    real = [f for f in findings if not f.startswith("skipped:")]
    for f in findings:
        tag = "gated" if gate and not f.startswith("skipped:") else "report-only"
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

    findings = [f"soundness: {f}" for f in criteria_soundness(criteria)]
    findings += [f"soundness: {f}" for f in envelope_soundness(envelope)]
    findings += [
        f if f.startswith("skipped:") else f"selection: {f}"
        for f in selection_lint(root, bet)
    ]

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
            rec = append_event(root, "kill-proposed", args.bet_id, {
                "criteria": triggered,
                "as_of": as_of.isoformat(),
                "distribution": dist,
            })
            print(f"  journaled kill-proposed ({rec['record_id']})")
        else:
            print("  kill proposal already pending — not re-journaled")
    return report(findings, args.gate)


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
            cadence=None, target_cac=None,
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
        })

    findings += cap_findings(root, args.max_in_flight)

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

    if args.format == "json":
        print(json.dumps({
            "bets": rows,
            "in_flight": in_flight_bets(root),
            "max_in_flight": args.max_in_flight,
            "dead_bets": dead,
            "findings": findings,
        }, indent=2))
        return 1 if args.gate and findings else 0

    print(f"portfolio: {root}")
    print(f"  bets: {len(bets)} — in flight: {len(in_flight_bets(root))}/{args.max_in_flight} "
          f"({', '.join(in_flight_bets(root)) or 'none'})")
    for r in rows:
        pend = "  [KILL PROPOSAL PENDING]" if r["kill_pending_proposal"] else ""
        print(
            f"  {r['id']:24s} {r['state']:12s} spend ${r['spend_usd']:g}/"
            f"${r['unlocked_usd']:g} unlocked (cap ${r['cash_cap_usd']:g}), "
            f"{r['hours']:g}h; next criterion: {r['next_criterion']}; "
            f"distribution: {r['distribution']}{pend}"
        )
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
