#!/usr/bin/env python3
"""/status — one live-derived screen of a target's chief-wiggum state (#213).

The operator-facing sibling of ``code_query orient``: everything printed here
is DERIVED at call time from the target's resolved meta locations — never a
hand-maintained doc, and this script never writes anything.

Sections (text) / keys (json):

- **resolver** — footprint mode, backing, meta root, target id
  (``artifacts.Resolver.to_dict``) + the domain-scope summary.
- **gates** — the gate ledger: every ``<quality_dir>/validation/<gate>.json``
  record, its verdict via ``check_gate_validation.check`` (passing / failing /
  missing-or-invalid), and its wired state via the tamper-evident ratchet
  journal (``ratchet.last_authority_action`` — wired means the last journaled
  gate-authority event is ``wire``).
- **ratchet** — high-water state from ``<quality_dir>/ratchet-scorecard.json``:
  pass-set size, contract-definition count, verifier-test-hash count.
  ``no ratchet config`` when the target has none.
- **partial_coverage** — section -> reason where something WAS measured but
  not everything was (an engine with no tier for a language in the
  population). Weaker than ``not_measured`` on purpose: over-claiming a gap
  trains the operator to ignore the marker.
- **not_measured** — section -> reason for every surface that measured
  NOTHING (#259). A zero-case pass-set and a zero-item debt inventory render
  identically to healthy ones, so "CW has no opinion here" reads as "CW
  approves"; a ``NOT MEASURED`` marker carrying the reason (no runner
  detected / no known-language sources) is the difference.
- **patterns** — adopted registry patterns (``<patterns_dir>/adopted.json``).
- **debt** — counts by severity from ``<quality_dir>/debt.json`` when the
  debt inventory exists (#214), else ``no inventory``.
- **adoption** — the brownfield adoption record (#215):
  ``<meta_root>/adoption/adoption.json`` + grandfather counts, nearest expiry,
  and prominent EXPIRED warnings from ``grandfathered.json``.

Usage::

    status.py --repo <path>          # local path
    status.py owner/repo             # resolved via scripts/repo.py
    status.py --repo . --format json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifacts  # noqa: E402
import check_gate_validation as gate_validation  # noqa: E402
import ratchet  # noqa: E402
from chief_wiggum import grandfather  # noqa: E402

RATCHET_CONFIG_NAME = ratchet.CONFIG_NAME
SCORECARD_NAME = ratchet.SCORECARD_NAME
JOURNAL_NAME = ratchet.JOURNAL_NAME
DEBT_NAME = "debt.json"
ADOPTED_NAME = "adopted.json"
VALIDATION_DIRNAME = "validation"
ADOPTION_DIRNAME = "adoption"
ADOPTION_NAME = "adoption.json"
GRANDFATHER_NAME = "grandfathered.json"


def _load_json(path: Path) -> dict | list | None:
    """Read a JSON file; missing/unparsable degrades to None (status must
    describe a broken state, never crash on one)."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# --- sections -------------------------------------------------------------------


def gate_ledger(quality_dir: Path) -> list[dict]:
    """One entry per ``validation/<gate>.json`` record: the verdict is read
    live via ``check_gate_validation.check`` (never trusted from the record's
    own status field) and the wired state from the journaled gate-authority
    events (never a hand-writable file)."""
    validation_dir = quality_dir / VALIDATION_DIRNAME
    journal = quality_dir / JOURNAL_NAME
    entries: list[dict] = []
    if not validation_dir.is_dir():
        return entries
    for path in sorted(validation_dir.glob("*.json")):
        gate = path.stem
        report = gate_validation.check(gate, validation_dir)
        if report.passing:
            verdict = "passing"
        elif not report.record_found:
            verdict = "missing"
        else:
            verdict = "failing"
        action = ratchet.last_authority_action(journal, gate)
        wired = action == "wire"
        entries.append({
            "gate": gate,
            "verdict": verdict,
            "wired": wired,
            "last_authority_action": action,
        })
    return entries


def ratchet_status(quality_dir: Path) -> dict:
    """High-water state from the scorecard snapshot. Presence of ratchet.json
    decides configured-ness; the counts come from the scorecard (#213: the
    display surface, not a re-derivation of the journal)."""
    if not (quality_dir / RATCHET_CONFIG_NAME).is_file():
        return {"configured": False}
    sc = _load_json(quality_dir / SCORECARD_NAME)
    if not isinstance(sc, dict):
        return {"configured": True, "scorecard": False}
    return {
        "configured": True,
        "scorecard": True,
        "pass_set": len(sc.get("pass_set", []) or []),
        "contracts": len(sc.get("contract_hashes", {}) or {}),
        "verifier_hashes": len(sc.get("verifier_test_hashes", {}) or {}),
    }


def ratchet_quarantines(quality_dir: Path) -> dict:
    """Quarantined high-water cases (#278) from the TOLERANT verified journal
    prefix — /status must describe a broken chain, never crash on one. Expiry
    is computed LIVE here (renderer overlay), so a quarantine authored before
    its expiry still reads EXPIRED after the date passes.

    Reads the journal directly via ``ratchet.verified_prefix`` (not
    ``ratchet.load_journal``, which raises ``TamperError`` and would crash a
    read-only screen) — the same pattern ``gate_ledger`` already uses.
    """
    journal = quality_dir / JOURNAL_NAME
    empty = {"count": 0, "entries": [], "expired": [], "nearest_expiry": None,
             "chain_broken": False}
    if not journal.is_file():
        return empty
    raw_lines = [ln for ln in journal.read_text().splitlines() if ln.strip()]
    prefix = ratchet.verified_prefix(journal)
    chain_broken = len(prefix) != len(raw_lines)
    quarantined = ratchet.derive_highwater(prefix).get("quarantined") or {}
    entries = sorted(quarantined.values(), key=lambda e: e.get("id", ""))
    expired = [e for e in entries if grandfather.is_expired(e)]
    expiries = sorted(e["expiry"] for e in entries if isinstance(e.get("expiry"), str))
    return {
        "count": len(entries),
        "entries": entries,
        "expired": expired,
        "nearest_expiry": expiries[0] if expiries else None,
        "chain_broken": chain_broken,
    }


def ratchet_quarantine_reason(q: dict) -> str | None:
    """PARTIAL COVERAGE reason for a non-empty quarantine, or None (#278):
    coverage is deliberately below the high-water mark while cases are
    quarantined — reuses the existing PARTIAL COVERAGE channel rather than
    inventing a new one (that channel exists precisely for "something WAS
    measured but not everything was")."""
    if not q["count"]:
        return None
    return (f"{q['count']} high-water case(s) quarantined — coverage is below the "
            "high-water mark (docs/ratchet.md)")


def ratchet_not_measured(quality_dir: Path, rt: dict) -> str | None:
    """Why the ratchet's pass-set measured NOTHING, or None when it measured
    something (#259).

    A zero-case pass-set is a technically-intact, practically-meaningless
    high-water mark: it can never slide backwards because there is nothing to
    slide. Rendered identically to a healthy pass-set, "honest zero" reads as
    "everything measured, all clean" — so the reason is carried here and shown
    where humans look."""
    if not rt.get("scorecard") or rt.get("pass_set"):
        return None
    sc = _load_json(quality_dir / SCORECARD_NAME)
    sc = sc if isinstance(sc, dict) else {}
    cfg = _load_json(quality_dir / RATCHET_CONFIG_NAME)
    suites = (cfg.get("suites") or []) if isinstance(cfg, dict) else []
    if not suites:
        return ("no test runner detected — ratchet.json configures 0 suite(s), so the "
                "high-water mark is zero and can never slide")
    names = ", ".join(str(s.get("name", "?")) for s in suites if isinstance(s, dict))
    if sc.get("tests_run") is False:
        return (f"tests not run (tests_run: false) — {len(suites)} suite(s) configured "
                f"({names}) but the pass-set was recorded as not-run")
    return (f"{len(suites)} suite(s) configured ({names}) produced 0 passing case(s) — "
            "a runner that reports nothing is not a clean run")


def debt_not_measured(quality_dir: Path, counts: dict | None) -> str | None:
    """Why the debt inventory scanned NOTHING, or None when it scanned files
    (#259). Proven from the inventory's own population count — never inferred
    from an empty item list, which is also what a genuinely clean repo has."""
    if counts is None or counts:
        return None
    doc = _load_json(quality_dir / DEBT_NAME)
    if not isinstance(doc, dict):
        return None
    population = ((doc.get("engines") or {}).get("dead_code") or {}).get("files_in_population")
    if not isinstance(population, int) or population > 0:
        return None
    unscanned = doc.get("unscanned_languages")
    detail = ""
    if isinstance(unscanned, dict) and unscanned:
        top = sorted(unscanned.items(), key=lambda kv: -kv[1])[:4]
        detail = " — unscanned: " + ", ".join(f"{k}: {n}" for k, n in top)
    return ("no known-language source files in the scan population: the engines had "
            f"nothing to scan{detail}")


def debt_partial_coverage(quality_dir: Path, counts: dict | None) -> str | None:
    """Why a zero-item inventory over a NON-empty population is still not a
    clean bill of health (#259).

    Distinct from :func:`debt_not_measured`: something was scanned, so the
    result is not vacuous — but an engine that has no tier for a language in
    the population contributes no findings for it, and "zero items" would read
    as if it had. Reported as partial coverage, not as unmeasured, because
    over-claiming a gap trains the operator to ignore the marker."""
    if counts is None or counts:
        return None
    doc = _load_json(quality_dir / DEBT_NAME)
    if not isinstance(doc, dict):
        return None
    population = ((doc.get("engines") or {}).get("dead_code") or {}).get("files_in_population")
    if not isinstance(population, int) or population <= 0:
        return None  # zero population is the NOT MEASURED case, not this one
    unscanned = doc.get("unscanned_languages")
    if not isinstance(unscanned, dict) or not unscanned:
        return None
    top = sorted(unscanned.items(), key=lambda kv: -kv[1])[:4]
    return ("some of the population was not scanned by every engine — "
            + ", ".join(f"{k}: {n} file(s)" for k, n in top))


def adopted_patterns(patterns_dir: Path) -> list[dict]:
    doc = _load_json(patterns_dir / ADOPTED_NAME)
    if not isinstance(doc, dict):
        return []
    out: list[dict] = []
    for pid, rec in sorted((doc.get("patterns") or {}).items()):
        rec = rec if isinstance(rec, dict) else {}
        out.append({
            "id": pid,
            "version": rec.get("version"),
            "applied_at": rec.get("applied_at"),
        })
    return out


def debt_counts(quality_dir: Path) -> dict | None:
    """Severity histogram of ``<quality_dir>/debt.json`` (#214's inventory).
    Tolerant of shape: a top-level list of items, or an object whose first
    list-of-dicts value is the item list. None = no inventory."""
    doc = _load_json(quality_dir / DEBT_NAME)
    if doc is None:
        return None
    items: list = []
    if isinstance(doc, list):
        items = doc
    elif isinstance(doc, dict):
        for key in ("items", "debts", "entries"):
            if isinstance(doc.get(key), list):
                items = doc[key]
                break
        else:
            for v in doc.values():
                if isinstance(v, list) and all(isinstance(i, dict) for i in v):
                    items = v
                    break
    counts: dict[str, int] = {}
    for item in items:
        sev = item.get("severity", "unknown") if isinstance(item, dict) else "unknown"
        counts[str(sev)] = counts.get(str(sev), 0) + 1
    return counts


def adoption_status(meta_root: Path) -> dict | None:
    """The #215 adoption record + grandfather pressure, or None when the
    target was never adopted (CW-built, or pre-adoption). Grandfather expiry
    is VISIBLE PRESSURE: counts, the nearest expiry, and any already-expired
    entry ids are surfaced here — expired grandfathers warn prominently."""
    adir = meta_root / ADOPTION_DIRNAME
    rec = _load_json(adir / ADOPTION_NAME)
    if not isinstance(rec, dict):
        return None
    gf = _load_json(adir / GRANDFATHER_NAME)
    entries = (gf.get("entries") or []) if isinstance(gf, dict) else []
    expiries = sorted(
        e.get("expiry") for e in entries
        if isinstance(e, dict) and isinstance(e.get("expiry"), str)
    )
    today = date.today()
    expired = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        exp = e.get("expiry")
        try:
            is_expired = date.fromisoformat(exp) < today if isinstance(exp, str) else True
        except ValueError:
            is_expired = True  # unparseable expiry is expired, never a silent pass
        if is_expired and e.get("id"):
            expired.append(e["id"])
    return {
        "brownfield": bool(rec.get("brownfield")),
        "adopted_at": rec.get("adopted_at"),
        "mode": rec.get("mode"),
        "grandfathered": len(entries),
        "nearest_expiry": expiries[0] if expiries else None,
        "expired": sorted(expired),
    }


def gather(target: str | Path, cw_home: Path | str | None = None) -> dict:
    """Everything /status shows, as one JSON-ready dict. Read-only."""
    resolver = artifacts.Resolver.resolve(Path(target).resolve(), cw_home=cw_home)
    quality_dir = resolver.quality_dir()
    rt = ratchet_status(quality_dir)
    # Version-binding staleness (#213 F12): the scorecard names the target HEAD
    # it was computed against (ratchet score stamps target_sha); warn when it
    # no longer matches. Only meaningful for git targets — a non-git target has
    # no HEAD to be stale against.
    if rt.get("scorecard") and artifacts.head_sha(resolver.target) is not None:
        sc = _load_json(quality_dir / SCORECARD_NAME)
        stale = resolver.check_stale(sc if isinstance(sc, dict) else {})
        if stale:
            rt["stale"] = stale
    # Pass-set case quarantine (#278): count/expiry pressure is surfaced on
    # the ratchet dict, and the detail (entries, nearest expiry, chain
    # health) is carried for render_text — but ONLY when there is something
    # to say, so a target with zero quarantines renders byte-identically to
    # pre-#278 /status.
    q = ratchet_quarantines(quality_dir)
    if q["count"] or q["chain_broken"]:
        if q["count"]:
            rt["quarantined"] = q["count"]
            rt["quarantined_expired"] = len(q["expired"])
        if q["chain_broken"]:
            rt["quarantine_chain_broken"] = True
        rt["quarantine_detail"] = q
    debt = debt_counts(quality_dir)
    # NOT MEASURED (#259): section -> reason, for every surface whose "clean"
    # rendering is indistinguishable from "measured nothing". A section absent
    # from this map WAS measured.
    not_measured = {
        section: reason
        for section, reason in (
            ("ratchet", ratchet_not_measured(quality_dir, rt)),
            ("debt", debt_not_measured(quality_dir, debt)),
        )
        if reason
    }
    partial_coverage = {
        section: reason
        for section, reason in (
            ("debt", debt_partial_coverage(quality_dir, debt)),
            ("ratchet", ratchet_quarantine_reason(q)),
        )
        if reason
    }
    return {
        "resolver": resolver.to_dict(),
        "gates": gate_ledger(quality_dir),
        "ratchet": rt,
        "patterns": adopted_patterns(resolver.patterns_dir()),
        "debt": debt,
        "not_measured": not_measured,
        "partial_coverage": partial_coverage,
        "adoption": adoption_status(resolver.meta_root),
    }


# --- rendering ------------------------------------------------------------------


def render_text(status: dict) -> str:
    r = status["resolver"]
    not_measured = status.get("not_measured") or {}
    partial_coverage = status.get("partial_coverage") or {}
    lines = [
        f"# Chief Wiggum status — {r['target_id']}",
        "",
        f"Footprint: {r['mode']} (backing: {r['backing']})",
        f"Meta root: {r['meta_root']}",
        f"Scope:     {r['scope']}",
        "",
        "## Gate ledger",
        "",
    ]
    gates = status["gates"]
    if not gates:
        lines.append("(no gate-validation records)")
    for g in gates:
        wired = "wired (blocking)" if g["wired"] else (
            "unwired" if g["last_authority_action"] == "unwire" else "never wired"
        )
        lines.append(f"- {g['gate']}: {g['verdict']} | {wired}")
    lines += ["", "## Ratchet high-water", ""]
    rt = status["ratchet"]
    if not rt.get("configured"):
        lines.append("no ratchet config")
    elif not rt.get("scorecard"):
        lines.append("configured, but no scorecard (run `ratchet.py score`)")
    else:
        if "ratchet" in not_measured:
            lines.append(f"NOT MEASURED: {not_measured['ratchet']}")
        lines.append(
            f"pass-set: {rt['pass_set']} case(s) | contracts: {rt['contracts']} | "
            f"verifier hashes: {rt['verifier_hashes']}"
        )
        if rt.get("stale"):
            lines.append(f"WARNING: scorecard {rt['stale']}")
        # Pass-set case quarantine (#278) — AC #3: an expired quarantine must
        # read as loudly as an expired grandfather (below).
        if rt.get("quarantined") or rt.get("quarantine_chain_broken"):
            detail = rt.get("quarantine_detail") or {}
            if "ratchet" in partial_coverage:
                lines.append(f"PARTIAL COVERAGE: {partial_coverage['ratchet']}")
            if rt.get("quarantined"):
                entries = detail.get("entries") or []
                nearest = detail.get("nearest_expiry")
                reason = ""
                for e in entries:
                    if e.get("expiry") == nearest:
                        reason = e.get("reason", "")
                        break
                lines.append(
                    f"quarantined: {rt['quarantined']} case(s), nearest expiry "
                    f'{nearest or "?"} — "{reason}"'
                )
                expired = detail.get("expired") or []
                if expired:
                    ids = ", ".join(e.get("id", "?") for e in expired)
                    lines.append(
                        f"WARNING: {len(expired)} EXPIRED quarantine(s) — expiry passed; "
                        f"the cases block again (docs/ratchet.md): {ids}"
                    )
            if rt.get("quarantine_chain_broken"):
                lines.append(
                    "WARNING: ratchet journal chain broken — quarantine list may be incomplete"
                )
    lines += ["", "## Adopted patterns", ""]
    if not status["patterns"]:
        lines.append("(none adopted)")
    for p in status["patterns"]:
        ver = f" v{p['version']}" if p.get("version") else ""
        lines.append(f"- {p['id']}{ver} (applied {p.get('applied_at') or '?'})")
    lines += ["", "## Debt", ""]
    debt = status["debt"]
    if debt is None:
        lines.append("no inventory (docs/quality/debt.json absent)")
    elif not debt:
        if "debt" in not_measured:
            lines.append(f"NOT MEASURED: {not_measured['debt']}")
            lines.append("inventory present, zero items — absence of findings is NOT health")
        elif "debt" in partial_coverage:
            lines.append(f"PARTIAL COVERAGE: {partial_coverage['debt']}")
            lines.append("inventory present, zero items")
        else:
            lines.append("inventory present, zero items")
    else:
        lines.append("  ".join(f"{sev}: {n}" for sev, n in sorted(debt.items())))
    lines += ["", "## Adoption", ""]
    adoption = status.get("adoption")
    if adoption is None:
        lines.append("no adoption record (CW-built, or /adopt not yet run)")
    else:
        lines.append(
            f"brownfield: {str(adoption['brownfield']).lower()} | "
            f"adopted {adoption.get('adopted_at') or '?'} | mode {adoption.get('mode') or '?'}"
        )
        if adoption["grandfathered"]:
            lines.append(
                f"grandfathered: {adoption['grandfathered']} finding(s), "
                f"nearest expiry {adoption.get('nearest_expiry') or '?'}"
            )
        else:
            lines.append("grandfathered: none")
        if adoption["expired"]:
            lines.append(
                f"WARNING: {len(adoption['expired'])} EXPIRED grandfather(s): "
                + ", ".join(adoption["expired"])
                + " — expiry passed; re-triage or remediate (docs/adopt.md)"
            )
    return "\n".join(lines) + "\n"


# --- CLI ------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One live-derived screen of a target's chief-wiggum state (#213)"
    )
    parser.add_argument(
        "target", nargs="?", default=None,
        help="owner/repo, resolved via scripts/repo.py (or use --repo for a local path)",
    )
    parser.add_argument("--repo", help="local path to the target repo (beats owner/repo)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    if args.repo:
        target = Path(args.repo)
    elif args.target:
        from repo import resolve_repo  # noqa: PLC0415 — needs gh; only when asked
        target = resolve_repo(args.target)
    else:
        target = Path(".")
    if not Path(target).is_dir():
        print(f"status: target not found: {target}", file=sys.stderr)
        return 2

    try:
        status = gather(target)
    except ValueError as exc:
        # e.g. a malformed election file or an unknown-key scope.json (#213
        # F6) — a legible usage error, never a traceback or a silent
        # whole-repo degradation.
        print(f"status: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(status, indent=2))
    else:
        print(render_text(status), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
