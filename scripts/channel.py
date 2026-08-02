#!/usr/bin/env python3
"""Channel engine — Bullseye mechanized over the bet ledger (chief-wiggum#241).

The distribution gap is a genuine operator skills gap, so the default loop for
every bet is *build product → no user movement → kill* (Startup Genome;
Weinberg & Mares: the #1 startup killer is no customers, not no product). This
script makes marketing attempts cheap, scheduled, and legible — it generates
everything up to the conversation, and NEVER substitutes for it. The honest
boundary: tooling cannot close a doing-gap; the rep-cadence invariant
(``bet.py`` leg, surfaced in ``evaluate`` and ``status`` here) exists to catch
the displacement failure mode of building marketing tools instead of doing
marketing.

Channel-experiment records live in ``bets/<bet-id>/channels.json``
(templates/channel-experiment-schema.json) in the same portfolio repo
``scripts/bet.py`` owns; every mutation is a journaled act in the portfolio's
ratchet-format hash chain (``bet.append_event`` — a tampered journal fails
closed, exit 4). Records range over the FIXED 19-channel enum (Weinberg &
Mares, *Traction* 2014); ``brainstorm`` seeds all 19 so the unfashionable
channels are considered, ranking is human.

Per-channel Bullseye state machine::

    brainstormed → ranked → testing (≤3 concurrent) → focused | rejected
    focused → testing        (re-entry on saturation — journaled, needs --reason)

Gate checks (ALL report-only by default per docs/gate-rollout.md; blocking only
with ``--gate``):

- **experiment completeness**: a record that claims results (a verdict, or any
  result field, or status focused|rejected) without a measured CAC AND
  customers-acquired count is invalid. A referral/WOM experiment additionally
  needs its recorded **baseline input flow** — referral is a multiplier on an
  existing acquisition stream, never a source (Balfour: sustained K > 1 is
  rare). A viral-channel record may carry the stamped ``referral-invite-loop``
  pattern's declared metrics (``k_factor``, ``invite_accept_rate``,
  ``reward_cost_per_attributed_signup`` — the pattern's CAC figure, accepted as
  the experiment's measured CAC); this is a validation rule on the record, not
  a platform integration.
- **exactly-one-focused-channel** (micro-scale invariant #7).
- **channel-CAC ≤ target CAC** join against the bet's declared
  ``target_cac_usd`` (``bet.py create --target-cac``); no target declared →
  the join reports ``skipped``, never a finding.
- **≤3 concurrent testing** (the Bullseye inner ring).
- **zero-headcount filter**: sales-led channels (GTM motion-fit: headcount 0 +
  sales-led = fail) are flagged while testing/focused with headcount 0
  (bet.json ``headcount``, falling back to means.json, defaulting to 0).

``status`` also re-surfaces the ledger-side rep-cadence and *Traction*-50%
findings computed by ``bet.py`` — the channel engine's report is where the
operator looks, so the doing-gap evidence shows up here too.

Subcommands:
    brainstorm  seed all 19 channels as `brainstormed` (forces the full enum)
    rank        record the human ranking (ordered channel list)
    test        move a channel into `testing` (≤3 concurrent; headcount filter)
    record      record experiment results onto a testing/focused channel
    focus       promote a testing channel to `focused` (exactly-one invariant)
    reject      close a testing channel as `rejected`
    status      list records + run every gate check (report-only default)

Exit codes: 0 = ok / report-only findings, 1 = gate violation (--gate), 2 =
usage/config error, 4 = journal tamper detected (fail closed).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The ledger stays bet.py's object: portfolio resolution, journal chain,
# report discipline, and the cadence/50%-rule checks are imported, not copied.
import bet as betlib  # noqa: E402
from bet import BetError, TamperError  # noqa: E402

# The fixed 19 traction channels (Weinberg & Mares, *Traction* 2014). The enum
# is closed on purpose: brainstorming must cover ALL of them, including the
# unfashionable ones.
CHANNELS = (
    "viral-marketing",
    "public-relations",
    "unconventional-pr",
    "search-engine-marketing",
    "social-and-display-ads",
    "offline-ads",
    "search-engine-optimization",
    "content-marketing",
    "email-marketing",
    "engineering-as-marketing",
    "targeting-blogs",
    "business-development",
    "sales",
    "affiliate-programs",
    "existing-platforms",
    "trade-shows",
    "offline-events",
    "speaking-engagements",
    "community-building",
)

STATES = ("brainstormed", "ranked", "testing", "focused", "rejected")
MAX_TESTING = 3

# GTM motion-fit decision table (docs/business-factory.md §3): headcount 0 +
# sales-led = fail. These channels need a human seller on payroll.
SALES_LED = {"sales", "business-development"}

# Referral/WOM: a multiplier on an existing acquisition stream, not a source
# (Balfour). Product-side mechanization = the `referral-invite-loop` pattern.
REFERRAL_CHANNELS = {"viral-marketing"}
REFERRAL_METRIC_KEYS = ("k_factor", "invite_accept_rate", "reward_cost_per_attributed_signup")

RESULT_KEYS = ("customers_acquired", "measured_cac", "verdict")


# ---- channels.json -------------------------------------------------------------


def channels_path(root: Path, bet_id: str) -> Path:
    return betlib.bet_dir(root, bet_id) / "channels.json"


def load_channels(root: Path, bet_id: str) -> list[dict]:
    p = channels_path(root, bet_id)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise BetError(f"cannot parse {p}: {e}") from e
    chans = data.get("channels") if isinstance(data, dict) else data
    if not isinstance(chans, list):
        raise BetError(f"{p}: expected {{'channels': [...]}}")
    return [c for c in chans if isinstance(c, dict)]


def save_channels(root: Path, bet_id: str, channels: list[dict]) -> None:
    channels_path(root, bet_id).write_text(
        json.dumps({"channels": channels}, indent=2, sort_keys=True) + "\n"
    )


def get_record(channels: list[dict], channel: str) -> dict | None:
    return next((c for c in channels if c.get("channel") == channel), None)


def require_channel(name: str) -> str:
    if name not in CHANNELS:
        raise BetError(
            f"unknown channel {name!r} — the enum is fixed (Weinberg & Mares): "
            + ", ".join(CHANNELS)
        )
    return name


def rec_status(rec: dict) -> str:
    return rec.get("status") or rec.get("state") or "brainstormed"


def _mark(rec: dict, status: str, event: str, detail: str = "") -> None:
    rec["status"] = status
    rec.setdefault("history", []).append(
        {"ts": betlib.now_iso(), "event": event, **({"detail": detail} if detail else {})}
    )


# ---- gate checks (report-only by default — docs/gate-rollout.md) ---------------


def claims_results(rec: dict) -> bool:
    """An experiment 'claims results' once any result field is recorded or the
    channel reached a decided state — mid-flight testing with nothing recorded
    yet is NOT incomplete, it is just young."""
    if rec_status(rec) in ("focused", "rejected"):
        return True
    if any(rec.get(k) is not None for k in RESULT_KEYS):
        return True
    return any((rec.get("referral_metrics") or {}).get(k) is not None for k in REFERRAL_METRIC_KEYS)


def effective_cac(rec: dict) -> float | None:
    """Measured CAC; for a referral channel the stamped pattern's
    `reward_cost_per_attributed_signup` (its CAC figure) is accepted."""
    if isinstance(rec.get("measured_cac"), (int, float)):
        return rec["measured_cac"]
    if rec.get("channel") in REFERRAL_CHANNELS:
        v = (rec.get("referral_metrics") or {}).get("reward_cost_per_attributed_signup")
        if isinstance(v, (int, float)):
            return v
    return None


def completeness_findings(rec: dict) -> list[str]:
    """A test without measured CAC + n-acquired is invalid; a referral
    experiment with no recorded baseline input flow is invalid the same way
    (a multiplier with no input stream measured nothing)."""
    if not claims_results(rec):
        return []
    ch = rec.get("channel", "?")
    out = []
    if effective_cac(rec) is None:
        out.append(f"{ch}: experiment invalid — results claimed without a measured CAC")
    if not isinstance(rec.get("customers_acquired"), (int, float)):
        out.append(f"{ch}: experiment invalid — results claimed without customers_acquired")
    if ch in REFERRAL_CHANNELS and not (rec.get("baseline_input_flow") or "").strip():
        out.append(
            f"{ch}: referral experiment invalid — no baseline input flow recorded "
            "(referral is a multiplier on an existing acquisition stream, not a "
            "source; record the stream it amplified)"
        )
    return out


def focused_findings(channels: list[dict], entering: str | None = None) -> list[str]:
    focused = sorted(c["channel"] for c in channels if rec_status(c) == "focused")
    if entering and entering not in focused:
        focused = sorted(focused + [entering])
    if len(focused) > 1:
        return [
            f"exactly-one-focused-channel violated: {', '.join(focused)} — Bullseye "
            "focuses ONE channel; demote (re-test) or reject before focusing another"
        ]
    return []


def testing_findings(channels: list[dict], entering: str | None = None) -> list[str]:
    testing = sorted(c["channel"] for c in channels if rec_status(c) == "testing")
    if entering and entering not in testing:
        testing = sorted(testing + [entering])
    if len(testing) > MAX_TESTING:
        return [
            f"{len(testing)} channels testing ({', '.join(testing)}) exceed the "
            f"Bullseye cap of {MAX_TESTING} concurrent — cheap parallel tests, "
            "not a spray"
        ]
    return []


def cac_findings(bet: dict, channels: list[dict], candidate: dict | None = None) -> list[str]:
    """Channel-CAC ≤ target-CAC join for focused (or about-to-focus) channels.
    No declared target → skipped, reported, never a finding."""
    subjects = [c for c in channels if rec_status(c) == "focused"]
    if candidate is not None and candidate not in subjects:
        subjects.append(candidate)
    if not subjects:
        return []
    target = bet.get("target_cac_usd")
    if not isinstance(target, (int, float)):
        return ["skipped: bet declares no target_cac_usd — channel-CAC join skipped"]
    out = []
    for c in subjects:
        cac = effective_cac(c)
        if cac is not None and cac > target:
            out.append(
                f"{c['channel']}: measured CAC ${cac:g} exceeds the bet's target "
                f"CAC ${target:g} — the channel is underwater at this price point; "
                "the verdict must say so, not let spend drift"
            )
    return out


def headcount(root: Path, bet: dict) -> int:
    hc = bet.get("headcount")
    if isinstance(hc, (int, float)):
        return int(hc)
    means = betlib.load_means(root) or {}
    hc = means.get("headcount")
    return int(hc) if isinstance(hc, (int, float)) else 0


def zero_headcount_findings(
    root: Path, bet: dict, channels: list[dict], entering: str | None = None
) -> list[str]:
    hc = headcount(root, bet)
    if hc > 0:
        return []
    active = {c["channel"] for c in channels if rec_status(c) in ("testing", "focused")}
    if entering:
        active.add(entering)
    return [
        f"{ch}: sales-led channel active at headcount 0 — GTM motion-fit fail "
        "(headcount 0 + sales-led); founder-led reps still count via the rep "
        "ledger, but the channel cannot carry the motion"
        for ch in sorted(active & SALES_LED)
    ]


def all_findings(root: Path, bet: dict, channels: list[dict]) -> list[str]:
    out: list[str] = []
    for c in channels:
        out += completeness_findings(c)
    out += focused_findings(channels)
    out += testing_findings(channels)
    out += cac_findings(bet, channels)
    out += zero_headcount_findings(root, bet, channels)
    return out


# ---- subcommand plumbing -------------------------------------------------------


def _load(args) -> tuple[Path, dict, list[dict]]:
    root = betlib.portfolio_root(args.portfolio_dir)
    bet = betlib.load_bet(root, args.bet_id)
    if bet["state"] in betlib.TERMINALS:
        raise BetError(
            f"{args.bet_id} is terminal ({bet['state']}) — no further channel work"
        )
    return root, bet, load_channels(root, args.bet_id)


def cmd_brainstorm(args) -> int:
    root, bet, channels = _load(args)
    have = {c.get("channel") for c in channels}
    added = []
    for ch in CHANNELS:
        if ch not in have:
            rec = {"channel": ch}
            _mark(rec, "brainstormed", "brainstorm")
            channels.append(rec)
            added.append(ch)
    if not added:
        print(f"channel: all {len(CHANNELS)} channels already brainstormed for {args.bet_id}")
        return 0
    save_channels(root, args.bet_id, channels)
    betlib.append_event(root, "channel-brainstorm", args.bet_id, {"added": added})
    print(
        f"channel: brainstormed {len(added)} channel(s) for {args.bet_id} — the "
        f"full {len(CHANNELS)}-channel enum is on the table (ranking is human: "
        "`channel.py rank`)"
    )
    return 0


def cmd_rank(args) -> int:
    root, bet, channels = _load(args)
    ranked = [require_channel(c) for c in args.channels]
    if len(set(ranked)) != len(ranked):
        raise BetError("duplicate channels in the ranking")
    for pos, ch in enumerate(ranked, 1):
        rec = get_record(channels, ch)
        if rec is None:
            raise BetError(f"{ch} not brainstormed yet — run `channel.py brainstorm` first")
        if rec_status(rec) not in ("brainstormed", "ranked"):
            raise BetError(f"{ch} is {rec_status(rec)} — ranking applies before testing")
        rec["rank"] = pos
        _mark(rec, "ranked", "rank", f"rank {pos}")
    save_channels(root, args.bet_id, channels)
    betlib.append_event(root, "channel-rank", args.bet_id, {"ranking": ranked})
    print(f"channel: ranked {len(ranked)} channel(s) for {args.bet_id}: {', '.join(ranked)}")
    return 0


def cmd_test(args) -> int:
    root, bet, channels = _load(args)
    ch = require_channel(args.channel)
    rec = get_record(channels, ch)
    if rec is None:
        raise BetError(f"{ch} not brainstormed yet — run `channel.py brainstorm` first")
    cur = rec_status(rec)
    if cur == "testing":
        raise BetError(f"{ch} is already testing")
    if cur == "rejected":
        raise BetError(f"{ch} is rejected — a fresh thesis belongs on a fresh bet")
    if cur == "focused" and not args.reason:
        raise BetError(
            f"{ch} is focused — re-entering testing (saturation) requires --reason "
            "(a journaled act)"
        )

    findings = testing_findings(channels, entering=ch)
    # Only the entering channel is judged here — pre-existing sales-led actives
    # are `status`'s sweep, and must not block unrelated channel work.
    findings += zero_headcount_findings(root, bet, [], entering=ch)
    rc = betlib.report(findings, args.gate, label="channel")
    if rc:
        print(f"channel: test {ch} on {args.bet_id} REFUSED (--gate)")
        return rc

    if args.hypothesis:
        rec["hypothesis"] = args.hypothesis
    if args.budget_usd is not None:
        rec["budget_usd"] = args.budget_usd
    if args.duration_days is not None:
        rec["duration_days"] = args.duration_days
    detail = "re-entry on saturation" if cur == "focused" else ""
    _mark(rec, "testing", "test", detail or (args.reason or ""))
    save_channels(root, args.bet_id, channels)
    betlib.append_event(root, "channel-test", args.bet_id, {
        "channel": ch, "from": cur, "hypothesis": rec.get("hypothesis"),
        "budget_usd": rec.get("budget_usd"), "duration_days": rec.get("duration_days"),
        "reason": args.reason or "",
    })
    n_testing = sum(1 for c in channels if rec_status(c) == "testing")
    print(f"channel: {ch} testing on {args.bet_id} ({n_testing}/{MAX_TESTING} concurrent)")
    return 0


def cmd_record(args) -> int:
    root, bet, channels = _load(args)
    ch = require_channel(args.channel)
    rec = get_record(channels, ch)
    if rec is None or rec_status(rec) not in ("testing", "focused"):
        raise BetError(f"{ch} is not testing/focused — results attach to a live experiment")

    for key, val in (
        ("customers_acquired", args.customers_acquired),
        ("measured_cac", args.measured_cac),
        ("icp_quality_note", args.icp_note),
        ("verdict", args.verdict),
        ("baseline_input_flow", args.baseline_flow),
    ):
        if val is not None:
            rec[key] = val
    referral = dict(rec.get("referral_metrics") or {})
    for key, val in (
        ("k_factor", args.k_factor),
        ("invite_accept_rate", args.invite_accept_rate),
        ("reward_cost_per_attributed_signup", args.reward_cost),
    ):
        if val is not None:
            referral[key] = val
    if referral:
        if ch not in REFERRAL_CHANNELS:
            raise BetError(
                f"referral-invite-loop metrics belong on the referral/WOM channel "
                f"({', '.join(sorted(REFERRAL_CHANNELS))}), not {ch}"
            )
        rec["referral_metrics"] = referral

    findings = completeness_findings(rec)
    findings += cac_findings(bet, channels)
    rc = betlib.report(findings, args.gate, label="channel")
    if rc:
        print(f"channel: record on {ch} ({args.bet_id}) REFUSED (--gate)")
        return rc

    rec.setdefault("history", []).append({"ts": betlib.now_iso(), "event": "record"})
    save_channels(root, args.bet_id, channels)
    betlib.append_event(root, "channel-record", args.bet_id, {
        "channel": ch,
        "customers_acquired": rec.get("customers_acquired"),
        "measured_cac": rec.get("measured_cac"),
        "verdict": rec.get("verdict"),
        "baseline_input_flow": rec.get("baseline_input_flow"),
        "referral_metrics": rec.get("referral_metrics"),
    })
    cac = effective_cac(rec)
    print(
        f"channel: recorded results on {ch} ({args.bet_id}) — "
        f"acquired {rec.get('customers_acquired', '?')}, "
        f"CAC {f'${cac:g}' if cac is not None else 'unmeasured'}"
    )
    return 0


def cmd_focus(args) -> int:
    root, bet, channels = _load(args)
    ch = require_channel(args.channel)
    rec = get_record(channels, ch)
    if rec is None or rec_status(rec) != "testing":
        raise BetError(f"{ch} is not testing — focus promotes a tested channel")

    findings = focused_findings(channels, entering=ch)
    # Focusing IS claiming results — completeness is judged as-if focused.
    findings += completeness_findings({**rec, "status": "focused"})
    findings += cac_findings(bet, channels, candidate=rec)
    findings += zero_headcount_findings(root, bet, [], entering=ch)
    rc = betlib.report(findings, args.gate, label="channel")
    if rc:
        print(f"channel: focus {ch} on {args.bet_id} REFUSED (--gate)")
        return rc

    if args.verdict:
        rec["verdict"] = args.verdict
    _mark(rec, "focused", "focus", args.verdict or "")
    save_channels(root, args.bet_id, channels)
    betlib.append_event(root, "channel-focus", args.bet_id, {
        "channel": ch, "measured_cac": effective_cac(rec), "verdict": rec.get("verdict"),
    })
    print(f"channel: {ch} FOCUSED on {args.bet_id} — all traction effort concentrates here")
    return 0


def cmd_reject(args) -> int:
    root, bet, channels = _load(args)
    ch = require_channel(args.channel)
    rec = get_record(channels, ch)
    if rec is None or rec_status(rec) != "testing":
        raise BetError(f"{ch} is not testing — reject closes a live experiment")
    if args.verdict:
        rec["verdict"] = args.verdict
    # Rejecting is also a decided state — judge completeness as-if rejected.
    findings = completeness_findings({**rec, "status": "rejected"})
    rc = betlib.report(findings, args.gate, label="channel")
    if rc:
        print(f"channel: reject {ch} on {args.bet_id} REFUSED (--gate)")
        return rc
    _mark(rec, "rejected", "reject", args.verdict or "")
    save_channels(root, args.bet_id, channels)
    betlib.append_event(root, "channel-reject", args.bet_id, {
        "channel": ch, "verdict": rec.get("verdict"),
    })
    print(f"channel: {ch} rejected on {args.bet_id}")
    return 0


def cmd_status(args) -> int:
    root = betlib.portfolio_root(args.portfolio_dir)
    bet = betlib.load_bet(root, args.bet_id)  # status works on terminal bets too
    channels = load_channels(root, args.bet_id)

    print(f"channel: {args.bet_id} [{bet['state']}] — {len(channels)}/{len(CHANNELS)} "
          "channels on the table")
    by_status: dict[str, list[dict]] = {}
    for c in channels:
        by_status.setdefault(rec_status(c), []).append(c)
    for st in STATES:
        recs = by_status.get(st, [])
        if not recs:
            continue
        if st == "brainstormed":
            print(f"  {st}: {len(recs)}")
            continue
        for c in sorted(recs, key=lambda r: (r.get("rank") or 99, r["channel"])):
            cac = effective_cac(c)
            bits = [f"rank {c['rank']}" if c.get("rank") else None,
                    f"budget ${c['budget_usd']:g}" if isinstance(c.get("budget_usd"), (int, float)) else None,
                    f"acquired {c['customers_acquired']:g}" if isinstance(c.get("customers_acquired"), (int, float)) else None,
                    f"CAC ${cac:g}" if cac is not None else None,
                    f"verdict: {c['verdict']}" if c.get("verdict") else None]
            print(f"  {st}: {c['channel']}" + (" — " + ", ".join(b for b in bits if b) if any(bits) else ""))

    findings = all_findings(root, bet, channels)

    # The ledger-side doing-gap checks surface here too — the channel report is
    # where the operator looks, and tool output must not hide the rep debt.
    entries = betlib.load_ledger(root, args.bet_id)
    cad = betlib.rep_cadence_status(bet, entries)
    if cad is not None:
        print(f"  rep cadence: {cad['line']}")
        findings += cad["findings"]
    findings += betlib.traction_findings(bet, entries)

    return betlib.report(findings, args.gate, label="channel")


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

    sp = sub.add_parser("brainstorm", help=f"seed all {len(CHANNELS)} channels as brainstormed")
    common(sp)
    sp.add_argument("bet_id")

    sp = sub.add_parser("rank", help="record the human ranking (ordered channels)")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("channels", nargs="+", metavar="CHANNEL")

    sp = sub.add_parser("test", help=f"move a channel into testing (≤{MAX_TESTING} concurrent)")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("channel")
    sp.add_argument("--hypothesis", default=None,
                    help="what this channel test is expected to show, falsifiably")
    sp.add_argument("--budget-usd", type=float, default=None)
    sp.add_argument("--duration-days", type=float, default=None)
    sp.add_argument("--reason", default=None,
                    help="required when re-entering testing from focused (saturation)")

    sp = sub.add_parser("record", help="record experiment results on a live channel")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("channel")
    sp.add_argument("--customers-acquired", type=int, default=None)
    sp.add_argument("--measured-cac", type=float, default=None, metavar="USD")
    sp.add_argument("--icp-note", default=None,
                    help="ICP quality of the acquired customers (who actually showed up)")
    sp.add_argument("--verdict", default=None,
                    help="short human verdict — an underwater channel's verdict must say so")
    sp.add_argument("--baseline-flow", default=None,
                    help="referral only: the existing acquisition stream the loop amplified "
                         "(a referral experiment with no baseline flow is invalid)")
    sp.add_argument("--k-factor", type=float, default=None,
                    help="referral-invite-loop declared metric (viral coefficient)")
    sp.add_argument("--invite-accept-rate", type=float, default=None,
                    help="referral-invite-loop declared metric (accepted/sent)")
    sp.add_argument("--reward-cost", type=float, default=None, metavar="USD",
                    help="referral-invite-loop reward_cost_per_attributed_signup — "
                         "accepted as the referral experiment's measured CAC")

    sp = sub.add_parser("focus", help="promote a testing channel to focused (exactly one)")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("channel")
    sp.add_argument("--verdict", default=None)

    sp = sub.add_parser("reject", help="close a testing channel as rejected")
    common(sp)
    sp.add_argument("bet_id")
    sp.add_argument("channel")
    sp.add_argument("--verdict", default=None)

    sp = sub.add_parser("status", help="list channel records + run every gate check")
    common(sp)
    sp.add_argument("bet_id")

    args = p.parse_args()
    dispatch = {
        "brainstorm": cmd_brainstorm, "rank": cmd_rank, "test": cmd_test,
        "record": cmd_record, "focus": cmd_focus, "reject": cmd_reject,
        "status": cmd_status,
    }
    try:
        return dispatch[args.cmd](args)
    except BetError as e:
        sys.stderr.write(f"channel: {e}\n")
        return 2
    except TamperError as e:
        sys.stderr.write(f"channel: {e}\n")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
