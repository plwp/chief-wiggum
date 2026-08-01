#!/usr/bin/env python3
"""debt_inventory.py — mechanical debt inventory with DEBT- stable IDs (#214).

Runs the four #214 debt engines (``quality/dead_code.py``, ``quality/clones.py``,
``quality/test_health.py``, ``quality/markers.py``) over the #213-resolved
scope (the same ``Resolver.in_scope`` population the hotspot/quality layer
uses) and writes ``debt.json`` to the resolver-determined quality dir — a list
of ADDRESSABLE work items, each with a stable ID, a mechanical severity, and a
blast radius.

**Stable IDs (a deliberate, argued departure from INV-fh-007):** hotspots are
risk priors and correctly carry no IDs; debt items are addressable work items
and need IDs for tickets, waivers, and trend lines. An ID is
``DEBT-`` + first 10 hex of sha256 over ``engine \\x00 normalized-path \\x00
anchor`` where the anchor is content-derived (symbol name, marker kind+text,
test kind+symbol, clone-class content hash — never a line number, never an
ordinal), so the same finding keeps its ID across runs and across SHA moves
that touch unrelated files, and a FIXED finding's ID simply disappears from
the next inventory. Clone classes span files, so their path component is empty
— the content hash IS the identity.

**Severity rubric (mechanical, per engine — see docs/debt-inventory.md):**

  - dead_code: tool tier (vulture/staticcheck/knip) = medium; conservative
    builtin-ast tier = low. +1 level (max high) when the file sits in hotspot
    decile >= 9 — dead weight in actively-churning complex code costs more.
  - clones: class size >= 3 = high; size 2 = medium.
  - test_health: orphaned_test / assertion_free_test = medium (a test that
    verifies nothing in a CI-run suite is false confidence); skipped_test =
    low. No hotspot bump (test files are rarely ranked).
  - markers: FIXME/HACK/XXX = medium in a hotspot-decile >= 9 file, else low;
    TODO = low always.

**Blast radius:** change-coupling partners from ``quality/process.py``
(``compute_coupling``/``partners_by_file`` — the ONE coupling engine,
INV-fh-001), filtered through the same #213 scope predicate as the population
(an out-of-scope partner never appears in ``blast_radius``), joined with the
file's hotspot decile when
``<quality_dir>/hotspots.json`` exists. When it doesn't, ``hotspot_decile`` is
null and the envelope says so — absence is stated, never implied.

**Report-only per docs/gate-rollout.md:** prints findings, exits 0, always.
There is no ``--gate`` flag; promotion (if ever) requires a
``validation/<gate>.json`` record per docs/gate-validation.md first.

Usage:
    python3 scripts/debt_inventory.py [owner/repo] [--repo PATH]
        [--out DIR] [--workdir DIR] [--format text|json]

``--out DIR`` writes ``debt.json`` (and reads the previous one for
first_seen/last_seen continuity) under DIR instead of the resolver-determined
quality dir — for read-only validation runs against repos whose meta must not
be written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifacts  # noqa: E402 — #213 meta-location resolver
from quality import clones, dead_code, markers, process, test_health  # noqa: E402

SCHEMA = "debt/1"
ID_HEX_LEN = 10
COUPLING_TOP_N = 5

AUTHORITY = (
    "mechanical debt findings from engine scans at {sha}; each item is an "
    "addressable work item, not a verdict; the absence of a finding in an "
    "unscanned language is NOT evidence of health (unscanned language counts "
    "are included in this envelope)."
)

SEVERITY_ORDER = ("low", "medium", "high")


def debt_id(engine: str, path: str, anchor: str) -> str:
    """DEBT- + 10 hex of sha256 over engine + normalized path + content anchor.
    Stable across runs and SHA moves; never ordinal, never line-based."""
    digest = hashlib.sha256(f"{engine}\x00{path}\x00{anchor}".encode()).hexdigest()
    return f"DEBT-{digest[:ID_HEX_LEN]}"


def _bump(severity: str) -> str:
    i = SEVERITY_ORDER.index(severity)
    return SEVERITY_ORDER[min(i + 1, len(SEVERITY_ORDER) - 1)]


def _norm_text(text: str) -> str:
    return " ".join(text.split()).lower()


# --- per-engine item builders -------------------------------------------------


def _dead_code_items(result: dict, decile_of: dict[str, int]) -> list[dict]:
    items = []
    for f in result.get("findings", []):
        severity = "low" if f.get("tier") == "builtin-ast" else "medium"
        if decile_of.get(f["file"], 0) >= 9:
            severity = _bump(severity)
        items.append({
            "id": debt_id("dead_code", f["file"], f["symbol"]),
            "engine": "dead_code",
            "kind": f.get("kind", "unused"),
            "severity": severity,
            "symbol": f["symbol"],
            "locations": [f"{f['file']}:{f.get('line', 0)}"],
            "detail": f"{f.get('tier')}: {f.get('kind', 'unused')} symbol {f['symbol']}",
        })
    return items


def _clone_items(result: dict) -> list[dict]:
    items = []
    for cls in result.get("clone_classes", []):
        severity = "high" if cls["size"] >= 3 else "medium"
        items.append({
            "id": debt_id("clones", "", cls["content_hash"]),
            "engine": "clones",
            "kind": "clone_class",
            "severity": severity,
            "symbol": cls["content_hash"],
            "locations": [
                f"{m['file']}:{m['start_line']}" for m in cls["members"]
            ],
            "detail": (
                f"clone class of {cls['size']} span(s), ~{cls['lines']} lines each "
                f"(content hash {cls['content_hash']})"
            ),
        })
    return items


def _test_health_items(result: dict) -> list[dict]:
    severity_by_kind = {
        "orphaned_test": "medium",
        "assertion_free_test": "medium",
        "skipped_test": "low",
    }
    items = []
    for f in result.get("findings", []):
        kind = f["kind"]
        items.append({
            "id": debt_id("test_health", f["file"], f"{kind}:{f.get('symbol', '')}"),
            "engine": "test_health",
            "kind": kind,
            "severity": severity_by_kind.get(kind, "low"),
            "symbol": f.get("symbol", ""),
            "locations": [f"{f['file']}:{f.get('line', 0)}"],
            "detail": (
                f"{kind}: {f.get('symbol', '')}"
                + (f" (subject stem '{f['subject_stem']}' — {f['mapping']})" if kind == "orphaned_test" else "")
            ),
        })
    return items


def _marker_items(result: dict, decile_of: dict[str, int]) -> list[dict]:
    # Identical (file, kind, normalized text) markers collapse into ONE item
    # with multiple locations — the ID must not depend on line numbers.
    grouped: dict[str, dict] = {}
    for f in result.get("findings", []):
        anchor = f"{f['kind']}:{_norm_text(f['text'])}"
        iid = debt_id("markers", f["file"], anchor)
        if f["kind"] == "TODO":
            severity = "low"
        else:
            severity = "medium" if decile_of.get(f["file"], 0) >= 9 else "low"
        item = grouped.setdefault(iid, {
            "id": iid,
            "engine": "markers",
            "kind": f["kind"],
            "severity": severity,
            "symbol": f["text"][:60],
            "locations": [],
            "detail": f"{f['kind']}: {f['text']}",
        })
        item["locations"].append(f"{f['file']}:{f['line']}")
    return list(grouped.values())


def _merge_duplicate_ids(items: list[dict]) -> list[dict]:
    """ID uniqueness is an invariant of the inventory: two findings whose
    content anchors coincide (e.g. two identical ``t.Skip("...")`` lines in
    one file — caught live on a real validation repo) are ONE work item with
    multiple locations, never two rows sharing an id. First occurrence wins
    the descriptive fields; locations concatenate (deduped, order kept);
    severity takes the max."""
    by_id: dict[str, dict] = {}
    for item in items:
        prev = by_id.get(item["id"])
        if prev is None:
            by_id[item["id"]] = item
            continue
        for loc in item["locations"]:
            if loc not in prev["locations"]:
                prev["locations"].append(loc)
        if SEVERITY_ORDER.index(item["severity"]) > SEVERITY_ORDER.index(prev["severity"]):
            prev["severity"] = item["severity"]
    return list(by_id.values())


def _engine_envelope(res: dict) -> dict:
    """Engine sub-envelope: metadata only, never finding payloads (the items
    list carries the data). clones' ``clone_classes`` payload is stripped the
    same way ``findings`` is — its count stays."""
    out = {k: v for k, v in res.items() if k not in ("findings", "clone_classes")}
    if "clone_classes" in res:
        out["clone_class_count"] = len(res["clone_classes"])
    return out


# --- blast radius -------------------------------------------------------------


def _load_hotspot_deciles(quality_dir: Path) -> tuple[dict[str, int], bool]:
    """file -> decile from <quality_dir>/hotspots.json; (map, available)."""
    p = quality_dir / "hotspots.json"
    if not p.is_file():
        return {}, False
    try:
        doc = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}, False
    out = {}
    for h in doc.get("hotspots", []) or []:
        if isinstance(h, dict) and h.get("file"):
            out[h["file"]] = h.get("decile") or 0
    return out, True


def _attach_blast_radius(
    items: list[dict], partners: dict[str, list[dict]],
    decile_of: dict[str, int], hotspots_available: bool,
) -> None:
    for item in items:
        files = sorted({loc.rsplit(":", 1)[0] for loc in item["locations"]})
        coupling = []
        deciles = []
        for f in files:
            coupling.extend(partners.get(f, [])[:COUPLING_TOP_N])
            if f in decile_of:
                deciles.append(decile_of[f])
        item["blast_radius"] = {
            "coupling_partners": coupling[:COUPLING_TOP_N],
            "hotspot_decile": (max(deciles) if deciles else None) if hotspots_available else None,
        }


# --- inventory ----------------------------------------------------------------


GRANDFATHER_RELPATH = Path("adoption") / "grandfathered.json"


def _load_grandfather(resolver: artifacts.Resolver) -> tuple[dict[str, dict], str | None]:
    """(id -> entry, file path) from ``<meta root>/adoption/grandfathered.json``
    (written by ``adopt.py grandfather``, chief-wiggum#215). Missing file ->
    ({}, None); an unparsable file is treated as absent — marking is additive
    labeling, never a gate input, so degrading is safe and honest (the /status
    adoption section names the record separately)."""
    p = resolver.meta_root / GRANDFATHER_RELPATH
    if not p.is_file():
        return {}, None
    try:
        doc = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}, None
    out: dict[str, dict] = {}
    for entry in (doc.get("entries") or []) if isinstance(doc, dict) else []:
        if isinstance(entry, dict) and entry.get("id"):
            out[entry["id"]] = entry
    return out, str(p)


def _grandfather_expired(expiry: str | None, today: str) -> bool:
    """ISO-date compare; an unparseable/missing expiry counts as EXPIRED, not
    a silent pass — same posture as the JUSTIFIED-waiver ``is_expired``."""
    if not isinstance(expiry, str):
        return True
    try:
        return date.fromisoformat(expiry) < date.fromisoformat(today)
    except ValueError:
        return True


def _apply_grandfather(items: list[dict], resolver: artifacts.Resolver,
                       today: str) -> dict:
    """Mark grandfathered items IN PLACE — they stay in the inventory (visible
    pressure), labeled so every surfacing layer (code-metrics debt section,
    slop-gate block, orient facts) can say so; expired grandfathers are
    flagged prominently. Returns the envelope's ``grandfather`` block."""
    entries, gf_file = _load_grandfather(resolver)
    expired_ids: list[str] = []
    count = 0
    for item in items:
        entry = entries.get(item["id"])
        if entry is None:
            continue
        count += 1
        item["grandfathered"] = True
        item["grandfather_expiry"] = entry.get("expiry")
        item["grandfather_expired"] = _grandfather_expired(entry.get("expiry"), today)
        if item["grandfather_expired"]:
            expired_ids.append(item["id"])
    return {"file": gf_file, "count": count, "expired": sorted(expired_ids)}


def _previous_seen(out_path: Path) -> dict[str, dict]:
    """id -> {first_seen, last_seen} from the previous debt.json, if any."""
    if not out_path.is_file():
        return {}
    try:
        doc = json.loads(out_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for item in doc.get("items", []) or []:
        if isinstance(item, dict) and item.get("id"):
            out[item["id"]] = {
                "first_seen": item.get("first_seen"),
                "last_seen": item.get("last_seen"),
            }
    return out


def build_inventory(repo: str, workdir: str, out_path: Path,
                    resolver: artifacts.Resolver | None = None,
                    now: str | None = None) -> dict:
    """Run the four engines and assemble the debt/1 envelope. Pure of any
    printing; ``run()`` handles the CLI face."""
    resolver = resolver or artifacts.Resolver.resolve(repo)
    path_filter = resolver.in_scope
    now = now or datetime.now(timezone.utc).isoformat()

    engine_results = {
        "dead_code": dead_code.analyze(repo, path_filter=path_filter),
        "clones": clones.analyze(repo, os.path.join(workdir, "jscpd"),
                                 path_filter=path_filter),
        "test_health": test_health.analyze(repo, path_filter=path_filter),
        "markers": markers.analyze(repo, path_filter=path_filter),
    }

    quality_dir = resolver.quality_dir()
    decile_of, hotspots_available = _load_hotspot_deciles(quality_dir)
    coupling_pairs = process.compute_coupling(repo)
    partners = process.partners_by_file(coupling_pairs)
    if path_filter is not None:
        # Coupling is DETECTED over the full git history (the one coupling
        # engine sees everything), but blast_radius is an authority surface:
        # a partner outside the #213 scope must never appear in it. Same
        # predicate as the population; if every partner drops, the empty
        # coupling list reflects that honestly.
        partners = {
            f: [p for p in ps if path_filter(p["file"])]
            for f, ps in partners.items()
        }

    items: list[dict] = []
    items += _dead_code_items(engine_results["dead_code"], decile_of)
    items += _clone_items(engine_results["clones"])
    items += _test_health_items(engine_results["test_health"])
    items += _marker_items(engine_results["markers"], decile_of)
    items = _merge_duplicate_ids(items)
    _attach_blast_radius(items, partners, decile_of, hotspots_available)

    previous = _previous_seen(out_path)
    sha = artifacts.head_sha(str(repo))
    for item in items:
        prev = previous.get(item["id"])
        item["first_seen"] = (prev or {}).get("first_seen") or now
        item["last_seen"] = now
        # Per-item target_sha per the documented schema — the SHA the finding
        # was last observed at (envelope carries it too; items are quoted alone).
        item["target_sha"] = sha

    # Grandfathered items (#215): marked, never removed — pre-adoption debt is
    # visible pressure, and expiry makes it LOUD, not amnestied.
    grandfather = _apply_grandfather(items, resolver, now[:10])

    # Deterministic order: severity desc, engine, id.
    sev_rank = {s: i for i, s in enumerate(reversed(SEVERITY_ORDER))}
    items.sort(key=lambda x: (sev_rank[x["severity"]], x["engine"], x["id"]))

    counts: dict[str, dict[str, int]] = {}
    for item in items:
        counts.setdefault(item["engine"], {})[item["severity"]] = (
            counts.get(item["engine"], {}).get(item["severity"], 0) + 1
        )

    unscanned: dict[str, int] = dict(engine_results["dead_code"].get("unscanned", {}))
    envelope = resolver.stamp({
        "schema": SCHEMA,
        "generated_at": now,
        "authority": AUTHORITY.format(
            sha=artifacts.head_sha(repo) or "HEAD"),
        "scope": resolver.scope_summary(),
        "hotspots_available": hotspots_available,
        "hotspots_note": None if hotspots_available else (
            f"{quality_dir / 'hotspots.json'} absent — blast_radius.hotspot_decile is "
            "null for every item; run scripts/hotspot_discovery.py to populate it"
        ),
        "engines": {
            name: _engine_envelope(res) for name, res in engine_results.items()
        },
        "unscanned_languages": unscanned,
        "counts": counts,
        "grandfather": grandfather,
        "items": items,
    })
    return envelope


# --- CLI ----------------------------------------------------------------------


def _current_repo_root() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        return out.stdout.strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def resolve_target(owner_repo: str | None, repo_path: str | None) -> str:
    """Resolve the target repo to a local absolute path (mirrors the other skills)."""
    if repo_path:
        p = Path(repo_path).expanduser().resolve()
        if not (p / ".git").exists():
            print(f"Error: {p} is not a git repository", file=sys.stderr)
            sys.exit(1)
        return str(p)
    if owner_repo:
        from repo import resolve_repo  # local import: only needed for owner/repo
        return str(resolve_repo(owner_repo))
    root = _current_repo_root()
    if not root:
        print("Error: not inside a git repo; pass owner/repo or --repo PATH", file=sys.stderr)
        sys.exit(1)
    return root


def format_report(envelope: dict) -> str:
    lines = ["## Debt inventory (report-only)"]
    lines.append(envelope["authority"])
    lines.append("")
    total = sum(sum(v.values()) for v in envelope["counts"].values())
    lines.append(f"{total} item(s) at target_sha {envelope.get('target_sha')}; scope: {envelope['scope']}")
    for engine, sevs in sorted(envelope["counts"].items()):
        parts = ", ".join(f"{sevs[s]} {s}" for s in ("high", "medium", "low") if s in sevs)
        lines.append(f"- {engine}: {parts}")
    for name, res in envelope["engines"].items():
        if res.get("skipped"):
            lines.append(f"- {name}: skipped — {res['skipped']}")
    if envelope["unscanned_languages"]:
        uns = ", ".join(f"{k}: {v} file(s)" for k, v in sorted(envelope["unscanned_languages"].items()))
        lines.append(f"- unscanned languages (dead_code): {uns}")
    gap = test_health.assertion_scan_gap(envelope["engines"])
    if gap:
        lines.append(f"- {gap}")
    if envelope.get("hotspots_note"):
        lines.append(f"- {envelope['hotspots_note']}")
    gf = envelope.get("grandfather") or {}
    if gf.get("count"):
        lines.append(
            f"- grandfathered: {gf['count']} item(s) (pre-adoption baseline — "
            "labeled, non-blocking; expiry is visible pressure, not amnesty)"
        )
    if gf.get("expired"):
        lines.append(
            "- EXPIRED grandfather: " + ", ".join(gf["expired"])
            + " — expiry passed; re-triage or remediate (see docs/adopt.md)"
        )
    top = envelope["items"][:10]
    if top:
        lines.append("")
        lines.append("Top items:")
        for item in top:
            loc = item["locations"][0] if item["locations"] else "?"
            more = f" (+{len(item['locations']) - 1} more)" if len(item["locations"]) > 1 else ""
            tag = ""
            if item.get("grandfathered"):
                tag = (" [EXPIRED grandfather]" if item.get("grandfather_expired")
                       else " [grandfathered]")
            lines.append(f"  {item['id']} [{item['severity']}]{tag} {item['engine']}/{item['kind']} "
                         f"{loc}{more} — {item['detail'][:100]}")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    target = resolve_target(args.owner_repo, args.repo)
    resolver = artifacts.Resolver.resolve(target)
    if args.out:
        out_dir = Path(args.out).expanduser()
    else:
        out_dir = resolver.quality_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "debt.json"

    if args.workdir:
        workdir = args.workdir
    else:
        import env  # session temp dir under ~/.chief-wiggum/tmp — never the target repo
        workdir = os.path.join(str(env.create_tmp()), "debt", Path(target).name)

    print(f"[debt-inventory] target: {target}", file=sys.stderr)
    print(f"[debt-inventory] out:    {out_path}", file=sys.stderr)

    envelope = build_inventory(target, workdir, out_path, resolver=resolver)
    out_path.write_text(json.dumps(envelope, indent=2) + "\n")

    if args.format == "json":
        print(json.dumps(envelope, indent=2))
    else:
        print(format_report(envelope))

    try:  # factory telemetry; no-op unless enabled, never breaks the run
        from factory_log import emit_gate
        emit_gate("debt_inventory", "pass",
                  caught=len(envelope["items"]), repo=Path(target).name)
    except Exception:
        pass

    return 0  # report-only, always


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mechanical debt inventory (DEBT- stable IDs; report-only)",
    )
    parser.add_argument("owner_repo", nargs="?", default=None,
                        help="owner/repo to resolve+clone (optional)")
    parser.add_argument("--repo", default=None, help="direct local repo path")
    parser.add_argument("--out", default=None,
                        help="directory for debt.json (default: the resolver-determined "
                             "quality dir; use for read-only validation runs)")
    parser.add_argument("--workdir", default=None, help="scratch dir for tool output")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
