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
    python3 scripts/debt_inventory.py append-candidate [owner/repo] [--repo PATH]
        --engine manual --path FILE[:LINE] --note "..." [--severity low]
    python3 scripts/debt_inventory.py resolve-candidate [owner/repo] [--repo PATH]
        --id DEBT-...

``--out DIR`` writes ``debt.json`` (and reads the previous one for
first_seen/last_seen continuity) under DIR instead of the resolver-determined
quality dir — for read-only validation runs against repos whose meta must not
be written.

**``append-candidate`` — found ≠ fixed (#216):** anything discovered
mid-ticket (dead code nearby, a clone, a smell) is filed in the SAME turn as a
candidate item and left untouched in the diff — scope discipline must not
cost information. The item uses the same stable-ID mechanics (``engine +
normalized path + content anchor``, here the normalized note text) and
carries ``engine: manual`` and ``candidate: true``. Candidates live in a
**mode-independent pending store** at
``<user_dir>/pending/<target-id>/candidates.json`` (``artifacts.user_dir`` —
NEVER the target tree, never docs/quality), so filing one is not a write into
the goalpost surface in embedded mode and is never lost to a scratch-dir
``--out`` inventory run. ``build_inventory`` merges pending candidates into
``items`` on every run regardless of ``out_path``; an engine finding landing
on the same id supersedes the candidate. Removing one is an explicit operator
act: ``debt_inventory.py resolve-candidate --repo X --id DEBT-...`` — never a
side effect of an engine re-run. Old-layout candidates found embedded in an
existing ``debt.json`` are adopted into the pending store once (stated in the
envelope and report).

**``anchor`` (#216 F1):** every item exposes the exact content-anchor string
used in its id derivation, so consumers (``plan_from_debt.py verify``) can
detect moved-not-resolved findings path-independently: a ``git mv`` changes
the id (path component) but not the anchor.
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
from chief_wiggum.grandfather import expired_live  # noqa: E402 — #215 F8 render overlay
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
            "anchor": f["symbol"],
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
            "anchor": cls["content_hash"],
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
            "anchor": f"{kind}:{f.get('symbol', '')}",
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
            "anchor": anchor,
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
    list carries the data). clones' ``clone_classes``/``boundary_classes``
    payloads are stripped the same way ``findings`` is — their counts stay."""
    out = {k: v for k, v in res.items()
           if k not in ("findings", "clone_classes", "boundary_classes")}
    if "clone_classes" in res:
        out["clone_class_count"] = len(res["clone_classes"])
    if "boundary_classes" in res:
        out["boundary_class_count"] = len(res["boundary_classes"])
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
    flagged prominently. Returns the envelope's ``grandfather`` block.

    ``grandfather_created_at`` carries the ENTRY's own timestamp (#216 F8):
    ``plan_from_debt.py verify`` only accepts a waiver whose entry POSTDATES
    the plan — an entry without a timestamp (pre-#216 files) never waives."""
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
        item["grandfather_created_at"] = (
            entry.get("extended_at") or entry.get("created_at"))
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


def _previous_candidates(out_path: Path) -> list[dict]:
    """``candidate: true`` items embedded in a previous debt.json — the OLD
    (pre-pending-store) layout. Used only for the one-time migration in
    ``build_inventory``: old-layout candidates are adopted into the pending
    store so they can never again vacuously resolve against a scratch-dir
    inventory (#216 F2). An envelope that already carries a
    ``pending_candidates`` block is NEW-layout — the pending store is
    authoritative for it, so nothing migrates (otherwise every rebuild would
    resurrect candidates the operator explicitly resolve-candidate'd)."""
    if not out_path.is_file():
        return []
    try:
        doc = json.loads(out_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(doc, dict) or "pending_candidates" in doc:
        return []
    return [item for item in doc.get("items", []) or []
            if isinstance(item, dict) and item.get("id") and item.get("candidate")]


# --- pending candidate store (#216 F2) ----------------------------------------
#
# Candidates are hand-filed observations, not engine findings. They live in a
# MODE-INDEPENDENT store under the CW user dir — never the target tree (no
# goalpost write in embedded mode), never a particular debt.json (no vacuous
# resolution when verify re-runs the inventory into a scratch dir). An engine
# re-run neither confirms nor removes them; removal is the explicit
# ``resolve-candidate`` operator act.


PENDING_SCHEMA = "pending-candidates/1"


def pending_path(resolver: artifacts.Resolver) -> Path:
    """``<user_dir>/pending/<target-id>/candidates.json`` for a target."""
    return artifacts.user_dir() / "pending" / Path(resolver.target_id) / "candidates.json"


def load_pending(resolver: artifacts.Resolver) -> list[dict]:
    """Candidate items currently in the pending store (missing/unparsable ->
    empty — an unreadable store files nothing and resolves nothing)."""
    p = pending_path(resolver)
    if not p.is_file():
        return []
    try:
        doc = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [i for i in (doc.get("items") or []) if isinstance(i, dict) and i.get("id")]


def save_pending(resolver: artifacts.Resolver, items: list[dict]) -> Path:
    p = pending_path(resolver)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "schema": PENDING_SCHEMA,
        "target_id": resolver.target_id,
        "items": items,
    }, indent=2) + "\n")
    return p


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

    # Manual candidates (#216 F2) come from the MODE-INDEPENDENT pending store
    # — merged on every build regardless of out_path, so a scratch-dir
    # (--out) inventory retains them and verify cannot vacuously resolve
    # them. An engine run cannot re-observe a candidate, so its seen
    # timestamps and target_sha stay as filed; an engine finding that lands
    # on the same id (identical anchor) supersedes it. Old-layout candidates
    # embedded in the previous debt.json are adopted into the pending store
    # ONCE (stated in the envelope + report).
    engine_ids = {item["id"] for item in items}
    pending = load_pending(resolver)
    pending_ids = {c["id"] for c in pending}
    migrated = [c for c in _previous_candidates(out_path)
                if c["id"] not in pending_ids]
    if migrated:
        pending = pending + migrated
        save_pending(resolver, pending)
    items += [c for c in pending if c["id"] not in engine_ids]
    pending_block = {
        "file": str(pending_path(resolver)),
        "count": len(pending),
        "migrated": sorted(c["id"] for c in migrated),
    }

    # Boundary findings (#216 C2): wholly-out-of-scope evidence captured where
    # the engines make it cheap. Clones: classes with >= 2 total spans that
    # fell below 2 in-scope members. Markers/dead_code/test_health emit no
    # boundary findings — their finding corpora are already scope-narrowed at
    # the source, so out-of-scope instances are never observed (that is the
    # boundary of the boundary, stated in boundary_note).
    boundary_items: list[dict] = []
    for cls in engine_results["clones"].get("boundary_classes") or []:
        boundary_items.append({
            "id": debt_id("clones", "", cls["content_hash"]),
            "anchor": cls["content_hash"],
            "engine": "clones",
            "kind": "clone_class",
            "severity": "high" if cls["size"] >= 3 else "medium",
            "symbol": cls["content_hash"],
            "boundary": True,
            "locations": [f"{m['file']}:{m['start_line']}" for m in cls["members"]],
            "detail": (
                f"clone class of {cls['size']} span(s), ~{cls['lines']} lines each "
                f"(content hash {cls['content_hash']}) — fewer than 2 spans in "
                "scope; owning-team debt, refer, never auto-fix"
            ),
            "target_sha": sha,
        })
    boundary_items.sort(key=lambda x: x["id"])

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
        "pending_candidates": pending_block,
        "boundary": boundary_items,
        "boundary_note": (
            "boundary captures wholly-out-of-scope evidence only where an "
            "engine sees it cheaply: clone classes dropped for out-of-scope "
            "members (>= 2 total spans). markers/dead_code/test_health corpora "
            "are scope-narrowed at the source, so their out-of-scope findings "
            "are never observed — absence from this section is NOT evidence "
            "the out-of-scope code is clean."
        ),
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
    pend = envelope.get("pending_candidates") or {}
    if pend.get("migrated"):
        lines.append(
            f"- migrated {len(pend['migrated'])} old-layout candidate(s) into "
            f"the pending store ({pend.get('file')}): "
            + ", ".join(pend["migrated"])
        )
    if pend.get("count"):
        lines.append(
            f"- pending candidates: {pend['count']} (store: {pend.get('file')} — "
            "removal is the explicit resolve-candidate act, never an engine re-run)"
        )
    boundary = envelope.get("boundary") or []
    if boundary:
        lines.append(
            f"- boundary: {len(boundary)} wholly-out-of-scope finding(s) — "
            "owning-team referrals (plan_from_debt boundary_referrals), never ticketed"
        )
    # Expired-ness is computed LIVE at render time (#215 F8): the stored
    # grandfather_expired flag is a build-time snapshot — an inventory built
    # before an expiry date must still render EXPIRED after it passes.
    expired_now = sorted(
        i["id"] for i in envelope.get("items") or []
        if isinstance(i, dict) and i.get("id") and expired_live(i)
    )
    if expired_now:
        lines.append(
            "- EXPIRED grandfather: " + ", ".join(expired_now)
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
                tag = (" [EXPIRED grandfather]" if expired_live(item)
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


class CandidateCollisionError(ValueError):
    """The derived candidate id belongs to a NON-candidate (engine) item —
    engines own their evidence; a hand-filed note may not shadow it (#216 F6)."""


def append_candidate(target: str, engine: str, path: str, note: str,
                     severity: str, resolver: artifacts.Resolver | None = None,
                     now: str | None = None) -> tuple[dict, bool]:
    """Append (or refresh) a manual candidate item in the PENDING STORE
    (``<user_dir>/pending/<target-id>/candidates.json``) — the #216
    found≠fixed hook. Mode-independent: never writes the target tree
    (embedded) or any debt.json (a scratch-dir inventory run can't lose it).
    Same stable-ID mechanics as engine items: the anchor is the NORMALIZED
    note text, so re-filing the same observation on the same file is
    idempotent (one item, refreshed ``last_seen``), and a reworded note is a
    different observation with a fresh id. Raises ``CandidateCollisionError``
    when the derived id belongs to a non-candidate item in the current
    inventory — engines own their evidence. Returns ``(item, created)``."""
    now = now or datetime.now(timezone.utc).isoformat()
    resolver = resolver or artifacts.Resolver.resolve(target)
    file_part, _, line_part = path.partition(":")
    location = f"{file_part}:{line_part or 0}"
    anchor = _norm_text(note)
    iid = debt_id(engine, file_part, anchor)

    # F6: refuse to shadow an engine finding. The current inventory (resolver
    # quality dir) is the engines' evidence surface; a candidate landing on a
    # non-candidate id would let a hand-filed note masquerade as (or later
    # supersede the provenance of) mechanical evidence.
    debt_file = resolver.quality_dir() / "debt.json"
    if debt_file.is_file():
        try:
            doc = json.loads(debt_file.read_text())
        except (OSError, json.JSONDecodeError):
            doc = {}
        for i in doc.get("items") or []:
            if isinstance(i, dict) and i.get("id") == iid and not i.get("candidate"):
                raise CandidateCollisionError(
                    f"candidate id {iid} collides with an engine finding "
                    f"({i.get('engine')}/{i.get('kind')} at "
                    f"{(i.get('locations') or ['?'])[0]}) — engines own their "
                    "evidence; reword the note or reference the engine item")

    items = load_pending(resolver)
    existing = next((i for i in items if i.get("id") == iid), None)
    if existing is not None:
        existing["last_seen"] = now
        if location not in existing.get("locations", []):
            existing.setdefault("locations", []).append(location)
        item, created = existing, False
    else:
        item = {
            "id": iid,
            "anchor": anchor,
            "engine": engine,
            "kind": "candidate",
            "candidate": True,
            "severity": severity,
            "symbol": note[:60],
            "locations": [location],
            "detail": f"manual candidate: {note}",
            "blast_radius": {"coupling_partners": [], "hotspot_decile": None},
            "first_seen": now,
            "last_seen": now,
            "target_sha": artifacts.head_sha(target),
        }
        items.append(item)
        created = True
    save_pending(resolver, items)
    return item, created


def resolve_candidate(resolver: artifacts.Resolver, iid: str) -> dict | None:
    """Remove one candidate from the pending store — the explicit operator
    act that resolves it (#216 F2). Returns the removed item, or None when the
    id is not pending."""
    items = load_pending(resolver)
    match = next((i for i in items if i.get("id") == iid), None)
    if match is None:
        return None
    save_pending(resolver, [i for i in items if i.get("id") != iid])
    return match


def run_append_candidate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="debt_inventory.py append-candidate",
        description="file a mid-ticket discovery as a DEBT- candidate item in "
                    "the pending store (found ≠ fixed, chief-wiggum#216) — "
                    "same turn, untouched diff, never the target tree",
    )
    parser.add_argument("owner_repo", nargs="?", default=None)
    parser.add_argument("--repo", default=None, help="direct local repo path")
    parser.add_argument("--engine", default="manual",
                        help="producing engine tag (default: manual)")
    parser.add_argument("--path", required=True, help="repo-relative FILE[:LINE]")
    parser.add_argument("--note", required=True,
                        help="what was found (the content anchor for the stable id)")
    parser.add_argument("--severity", choices=list(SEVERITY_ORDER), default="low")
    args = parser.parse_args(argv)

    target = resolve_target(args.owner_repo, args.repo)
    resolver = artifacts.Resolver.resolve(target)
    try:
        item, created = append_candidate(
            target, args.engine, args.path, args.note, args.severity,
            resolver=resolver)
    except CandidateCollisionError as exc:
        print(f"debt_inventory: {exc}", file=sys.stderr)
        return 2
    verb = "filed" if created else "already filed (last_seen refreshed)"
    print(f"debt_inventory: candidate {item['id']} {verb} — {args.path}: {args.note}")
    print(f"  pending store: {pending_path(resolver)}")
    return 0


def run_resolve_candidate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="debt_inventory.py resolve-candidate",
        description="remove one candidate from the pending store — the "
                    "explicit operator act that resolves it (chief-wiggum#216)",
    )
    parser.add_argument("owner_repo", nargs="?", default=None)
    parser.add_argument("--repo", default=None, help="direct local repo path")
    parser.add_argument("--id", required=True, help="the DEBT- candidate id to resolve")
    args = parser.parse_args(argv)

    target = resolve_target(args.owner_repo, args.repo)
    resolver = artifacts.Resolver.resolve(target)
    removed = resolve_candidate(resolver, args.id)
    if removed is None:
        print(f"debt_inventory: {args.id} is not in the pending store "
              f"({pending_path(resolver)}) — nothing to resolve", file=sys.stderr)
        return 1
    print(f"debt_inventory: candidate {args.id} resolved (removed from "
          f"{pending_path(resolver)})")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "append-candidate":
        return run_append_candidate(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "resolve-candidate":
        return run_resolve_candidate(sys.argv[2:])
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
