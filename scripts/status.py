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
    return {
        "resolver": resolver.to_dict(),
        "gates": gate_ledger(quality_dir),
        "ratchet": rt,
        "patterns": adopted_patterns(resolver.patterns_dir()),
        "debt": debt_counts(quality_dir),
        "adoption": adoption_status(resolver.meta_root),
    }


# --- rendering ------------------------------------------------------------------


def render_text(status: dict) -> str:
    r = status["resolver"]
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
        lines.append(
            f"pass-set: {rt['pass_set']} case(s) | contracts: {rt['contracts']} | "
            f"verifier hashes: {rt['verifier_hashes']}"
        )
        if rt.get("stale"):
            lines.append(f"WARNING: scorecard {rt['stale']}")
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
