#!/usr/bin/env python3
"""plan_from_debt.py — remediation-epic planning from the debt inventory (#216).

Turns ``debt.json`` (the #214 inventory) into a **budgeted, dependency-ordered
ticket plan** that `/plan-epic --from-debt` consumes. Three subcommands:

- ``plan``     cluster DEBT- items into tickets, apply the REQUIRED budget,
               emit ``remediation-plan.json`` + a markdown summary.
- ``pathset``  derive one ticket's sanctioned pathset in exactly the
               ``{"paths": [...], "source": "..."}`` shape ``ratchet.py
               pathset`` consumes (chief-wiggum#213 parking machinery).
- ``verify``   the /close-epic acceptance test: every TICKETED DEBT- id must
               be gone from a FRESH inventory (or explicitly waived post-plan
               via the ``adopt.py grandfather --extend`` path). An id that
               merely MOVED — a new item (id not in the plan's recorded
               ``baseline_ids``) carrying the SAME content anchor, e.g. a
               ``git mv`` that renamed the path without fixing the finding —
               counts UNRESOLVED (#216 F1). Ticketed CANDIDATE ids resolve
               against the pending store, not the fresh inventory (#216 F2).
               NEW ids that appeared in the tickets' own pathset files are
               REPORTED (never a failure) for review before closing. Exit 1
               listing unresolved/moved ids; exit 0 clean.

**Anchor-compare boundary (stated):** the moved check compares content
anchors, which for markers is the kind + normalized marker text — REWORDING a
TODO/FIXME mints a new anchor, so a reworded-not-fixed marker is NOT caught as
moved; it surfaces in the new-ids-in-ticket-files report instead (review
before closing). Clone classes compare by member-content hash (the anchor),
fully path-independent.

**Clustering precedence (documented, mechanical):**

  (a) **clone class** — a ``clones``-engine item (one clone class = one DEBT-
      item) is always its own ticket: deduplicating a class is one coherent
      unit of work that legitimately spans modules. Clone tickets never merge
      further.
  (b) **module/directory** — every other item clusters by the directory of
      its first location: co-located debt is one ticket.
  (c) **change-coupling partnership** — module clusters whose items'
      ``blast_radius.coupling_partners`` name files in another module cluster
      merge (union-find): change-coupled debt is one ticket even across
      directories. Applies to module clusters only — (a) is final.

**Budget is REQUIRED** (``--budget-count`` and/or ``--budget-severity-floor``
and/or ``--budget-cluster-cap``): an unbudgeted remediation epic is unbounded
scope — the CLI refuses (exit 2) without at least one. Everything left behind
is recorded in the plan's ``excluded`` list with a reason
(``over_budget`` / ``below_severity_floor`` / ``over_cluster_cap``) — leftover
inventory is the NORMAL end state of a remediation epic, not a failure.

**Boundary findings are never ticketed.** An item marked ``boundary`` — or
whose EVERY location falls outside the #213 resolver scope — lands in
``boundary_referrals`` with a filled issue body from
``templates/boundary-finding.md``: report to the owning team, never auto-fix.

**Grandfathered items ARE valid input** (#215): remediating them before their
expiry is the point. The plan marks them (``grandfathered_ids`` per ticket) so
``verify`` can tell a pre-plan grandfather (still unresolved if present) from
an explicit post-plan waiver (a NEW grandfather entry added via ``--extend``).

**Dependency ordering:** tickets are ordered by severity rollup (high, then
medium, then low counts, then size), and a ticket ``depends_on`` every
earlier ticket whose pathset files overlap its own pathset or its items'
coupling partners — overlapping-scope tickets are serialized, disjoint ones
can run in the same wave.

Report-only by construction: this script plans work; it never gates anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifacts  # noqa: E402 — #213 meta-location resolver
import debt_inventory  # noqa: E402 — pending candidate store (#216 F2)
from chief_wiggum.grandfather import expired_live  # noqa: E402 — #215 live expiry
from debt_inventory import SEVERITY_ORDER, resolve_target  # noqa: E402

SCHEMA = "remediation-plan/1"
PLAN_NAME = "remediation-plan.json"
PLAN_MD_NAME = "remediation-plan.md"
BOUNDARY_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "boundary-finding.md"

CLUSTERING_PRECEDENCE = [
    "(a) clone-class: each clones-engine item is its own ticket (one clone class = "
    "one coherent dedup unit, legitimately cross-module); never merged further",
    "(b) module/directory: remaining items cluster by the directory of their first "
    "location",
    "(c) change-coupling: module clusters whose blast_radius.coupling_partners "
    "name files in another module cluster merge (union-find); applies to module "
    "clusters only",
]

BUDGET_REFUSAL = (
    "an unbudgeted remediation epic is unbounded scope — pass --budget-count "
    "and/or --budget-severity-floor and/or --budget-cluster-cap"
)

FLOOR_LOW_REFUSAL = (
    "--budget-severity-floor low excludes nothing ('low' is the lowest "
    "severity) — it is not a budget on its own; combine it with "
    "--budget-count and/or --budget-cluster-cap"
)


def _positive_int(value: str) -> int:
    """argparse type for budget counts/caps: >= 1, or the budget is vacuous
    (0) / nonsensical (negative) — refuse loudly (#216 F5)."""
    try:
        n = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if n < 1:
        raise argparse.ArgumentTypeError(
            f"{n} is not a valid budget (must be >= 1 — a budget of 0 or less "
            "is vacuous, not a plan)")
    return n


# --- helpers ------------------------------------------------------------------


def _loc_file(loc: str) -> str:
    """``file`` part of a ``file:line`` location string."""
    return loc.rsplit(":", 1)[0] if ":" in loc else loc


def _item_files(item: dict) -> list[str]:
    return sorted({_loc_file(loc) for loc in item.get("locations") or []})


def _partner_files(item: dict) -> set[str]:
    partners = (item.get("blast_radius") or {}).get("coupling_partners") or []
    return {p["file"] for p in partners if isinstance(p, dict) and p.get("file")}


def _sev_rank(sev: str) -> int:
    """high=0, medium=1, low=2 (ascending sorts most-severe first)."""
    try:
        return len(SEVERITY_ORDER) - 1 - SEVERITY_ORDER.index(sev)
    except ValueError:
        return len(SEVERITY_ORDER)


def load_debt(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"debt file not found: {p}")
    doc = json.loads(p.read_text())
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise ValueError(f"{p} is not a debt/1 envelope (no items list)")
    return doc


# --- boundary + budget filters ------------------------------------------------


def split_boundary(items: list[dict], in_scope) -> tuple[list[dict], list[dict]]:
    """(workable, boundary). Boundary = explicitly marked ``boundary``, or
    every location file out-of-scope per the live resolver predicate — those
    are the owning team's debt (#213 authority split), reported, never
    ticketed. An item with no locations cannot be judged out-of-scope."""
    workable, boundary = [], []
    for item in items:
        files = _item_files(item)
        if item.get("boundary"):
            boundary.append(item)
        elif in_scope is not None and files and not any(in_scope(f) for f in files):
            boundary.append(item)
        else:
            workable.append(item)
    return workable, boundary


def apply_severity_floor(items: list[dict], floor: str | None) -> tuple[list[dict], list[dict]]:
    if floor is None:
        return items, []
    floor_i = SEVERITY_ORDER.index(floor)
    kept = [i for i in items if SEVERITY_ORDER.index(i["severity"]) >= floor_i]
    dropped = [i for i in items if SEVERITY_ORDER.index(i["severity"]) < floor_i]
    return kept, dropped


# --- clustering ---------------------------------------------------------------


def cluster_items(items: list[dict]) -> list[dict]:
    """Clusters per the documented precedence. Each cluster:
    ``{"strategy", "key", "items"}`` — deterministic order within and across
    clusters."""
    clone_clusters: list[dict] = []
    module_of: dict[str, list[dict]] = {}
    for item in items:
        if item.get("engine") == "clones":
            clone_clusters.append({
                "strategy": "clone-class", "key": item.get("symbol") or item["id"],
                "items": [item],
            })
            continue
        files = _item_files(item)
        module = str(Path(files[0]).parent.as_posix()) if files else "(no-location)"
        module_of.setdefault(module, []).append(item)

    # (c) union-find over module clusters via coupling-partner overlap.
    modules = sorted(module_of)
    parent = {m: m for m in modules}

    def find(m: str) -> str:
        while parent[m] != m:
            parent[m] = parent[parent[m]]
            m = parent[m]
        return m

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            ra, rb = sorted((ra, rb))
            parent[rb] = ra

    files_of_module = {
        m: {f for item in its for f in _item_files(item)}
        for m, its in module_of.items()
    }
    partners_of_module = {
        m: set().union(*[_partner_files(item) for item in its]) if its else set()
        for m, its in module_of.items()
    }
    for a in modules:
        for b in modules:
            if a >= b:
                continue
            if partners_of_module[a] & files_of_module[b] or partners_of_module[b] & files_of_module[a]:
                union(a, b)

    merged: dict[str, list[str]] = {}
    for m in modules:
        merged.setdefault(find(m), []).append(m)

    module_clusters = []
    for _root, members in sorted(merged.items()):
        its = [i for m in sorted(members) for i in module_of[m]]
        its.sort(key=lambda i: (_sev_rank(i["severity"]), i["engine"], i["id"]))
        if len(members) == 1:
            module_clusters.append({"strategy": "module", "key": members[0], "items": its})
        else:
            module_clusters.append({
                "strategy": "coupling", "key": " + ".join(sorted(members)), "items": its,
            })

    for c in clone_clusters:
        c["items"].sort(key=lambda i: i["id"])
    return clone_clusters + module_clusters


def apply_cluster_cap(clusters: list[dict], cap: int | None) -> tuple[list[dict], list[dict]]:
    """Trim each cluster to at most ``cap`` items (most severe kept; ties by
    id). Overflow items are excluded with reason ``over_cluster_cap``."""
    if cap is None:
        return clusters, []
    dropped: list[dict] = []
    out = []
    for c in clusters:
        its = sorted(c["items"], key=lambda i: (_sev_rank(i["severity"]), i["id"]))
        keep, over = its[:cap], its[cap:]
        dropped.extend(over)
        if keep:
            out.append({**c, "items": keep})
    return out, dropped


def _rollup(items: list[dict]) -> dict[str, int]:
    out = {s: 0 for s in reversed(SEVERITY_ORDER)}
    for i in items:
        out[i["severity"]] = out.get(i["severity"], 0) + 1
    return out


def _cluster_order_key(c: dict):
    r = _rollup(c["items"])
    return (-r.get("high", 0), -r.get("medium", 0), -r.get("low", 0),
            -len(c["items"]), c["strategy"], c["key"])


def order_and_budget(clusters: list[dict], count: int | None) -> tuple[list[dict], list[dict]]:
    """Order clusters (severity rollup desc, size desc, stable key) and keep
    the first ``count``. Items in cut clusters are excluded ``over_budget``."""
    ordered = sorted(clusters, key=_cluster_order_key)
    if count is None:
        return ordered, []
    kept, cut = ordered[:count], ordered[count:]
    return kept, [i for c in cut for i in c["items"]]


# --- tickets ------------------------------------------------------------------


def _ticket_title(cluster: dict) -> str:
    n = len(cluster["items"])
    if cluster["strategy"] == "clone-class":
        item = cluster["items"][0]
        spans = len(item.get("locations") or [])
        return f"refactor: deduplicate clone class {cluster['key'][:12]} ({spans} spans)"
    if cluster["strategy"] == "coupling":
        return f"refactor: remediate {n} change-coupled debt item(s) ({cluster['key']})"
    return f"refactor: remediate {n} debt item(s) in {cluster['key']}/"


def build_tickets(clusters: list[dict], debt_file: str, in_scope=None) -> list[dict]:
    tickets = []
    for n, cluster in enumerate(clusters, start=1):
        tid = f"RT-{n:03d}"
        items = cluster["items"]
        all_files = sorted({f for i in items for f in _item_files(i)})
        # F3: the derived SANCTIONED pathset carries only in-scope files — a
        # partially-out-of-scope item (e.g. a clone class with one foot in
        # another team's tree) must not smuggle out-of-scope files into the
        # sanctioned set. Out-of-scope locations are listed separately as
        # boundary_locations: informational, feeds the referral note, never
        # sanctioned.
        if in_scope is None:
            files = all_files
            boundary_locations: list[str] = []
        else:
            files = [f for f in all_files if in_scope(f)]
            boundary_locations = sorted({
                loc for i in items for loc in i.get("locations") or []
                if not in_scope(_loc_file(loc))
            })
        partner_files = set().union(*[_partner_files(i) for i in items]) if items else set()
        tickets.append({
            "id": tid,
            "kind": "refactor",
            "title": _ticket_title(cluster),
            "cluster": {"strategy": cluster["strategy"], "key": cluster["key"]},
            "debt_ids": [i["id"] for i in items],
            "grandfathered_ids": sorted(i["id"] for i in items if i.get("grandfathered")),
            "severity_rollup": _rollup(items),
            "locations": sorted({loc for i in items for loc in i.get("locations") or []}),
            "boundary_locations": boundary_locations,
            # The derived sanctioned pathset: the items' IN-SCOPE location
            # files. The `collateral` slot is DECLARED at approach time
            # (/implement Step 4) — callers/tests that must move with the
            # refactor — and appended by the `pathset` subcommand.
            "pathset": {
                "paths": files,
                "source": f"plan_from_debt {tid} ({debt_file})",
            },
            "collateral": [],
            "depends_on": [],
            "items": [
                {k: i.get(k) for k in ("id", "engine", "kind", "severity", "detail")}
                | ({"grandfathered": True} if i.get("grandfathered") else {})
                | ({"candidate": True} if i.get("candidate") else {})
                | ({"anchor": i["anchor"]} if i.get("anchor") else {})
                for i in items
            ],
            "_partner_files": sorted(partner_files),  # stripped before emit
        })

    # Dependency ordering: T depends on every EARLIER ticket whose pathset
    # overlaps T's pathset or either side's coupling partners — overlapping
    # scope is serialized; disjoint tickets can share a wave.
    for j, tj in enumerate(tickets):
        fj = set(tj["pathset"]["paths"])
        pj = set(tj["_partner_files"])
        for ti in tickets[:j]:
            fi = set(ti["pathset"]["paths"])
            pi = set(ti["_partner_files"])
            if fi & fj or fi & pj or fj & pi:
                tj["depends_on"].append(ti["id"])
    for t in tickets:
        del t["_partner_files"]
    return tickets


# --- boundary referrals -------------------------------------------------------


def _boundary_body(item: dict, scope_summary: str, target_sha: str | None) -> str:
    template = BOUNDARY_TEMPLATE.read_text()
    locations = "\n".join(f"- `{loc}`" for loc in item.get("locations") or []) or "- (none recorded)"
    return template.format(
        id=item["id"],
        engine=item.get("engine", "?"),
        kind=item.get("kind", "?"),
        severity=item.get("severity", "?"),
        detail=item.get("detail", ""),
        locations=locations,
        scope=scope_summary,
        target_sha=target_sha or "(unknown)",
    )


def build_boundary_referrals(items: list[dict], scope_summary: str,
                             target_sha: str | None) -> list[dict]:
    out = []
    for item in sorted(items, key=lambda i: (_sev_rank(i["severity"]), i["id"])):
        out.append({
            "id": item["id"],
            "engine": item.get("engine"),
            "kind": item.get("kind"),
            "severity": item.get("severity"),
            "locations": item.get("locations") or [],
            "issue_title": f"[boundary finding] {item['id']}: "
                           f"{(item.get('detail') or item.get('kind') or '')[:80]}",
            "issue_body": _boundary_body(item, scope_summary, target_sha),
        })
    return out


# --- plan assembly ------------------------------------------------------------


def build_plan(debt: dict, debt_file: str, resolver: artifacts.Resolver,
               budget_count: int | None, severity_floor: str | None,
               cluster_cap: int | None, now: str | None = None) -> dict:
    """Pure of any printing; the CLI face is ``cmd_plan``."""
    now = now or datetime.now(timezone.utc).isoformat()
    items = [i for i in debt.get("items") or [] if isinstance(i, dict) and i.get("id")]

    workable, boundary = split_boundary(items, resolver.in_scope)
    workable, below_floor = apply_severity_floor(workable, severity_floor)
    clusters = cluster_items(workable)
    clusters, over_cap = apply_cluster_cap(clusters, cluster_cap)
    clusters, over_budget = order_and_budget(clusters, budget_count)
    tickets = build_tickets(clusters, debt_file, in_scope=resolver.in_scope)

    excluded = (
        [{"id": i["id"], "engine": i.get("engine"), "severity": i.get("severity"),
          "reason": "below_severity_floor"} for i in below_floor]
        + [{"id": i["id"], "engine": i.get("engine"), "severity": i.get("severity"),
            "reason": "over_cluster_cap"} for i in over_cap]
        + [{"id": i["id"], "engine": i.get("engine"), "severity": i.get("severity"),
            "reason": "over_budget"} for i in over_budget]
    )
    excluded.sort(key=lambda e: (e["reason"], e["id"]))
    # C2: the inventory's dedicated `boundary` section (engine-captured
    # wholly-out-of-scope evidence, e.g. clone classes dropped for
    # out-of-scope members) feeds referrals ALONGSIDE marked/out-of-scope
    # items — the engines no longer drop that evidence on the floor.
    seen_boundary = {i["id"] for i in boundary}
    boundary = boundary + [
        b for b in debt.get("boundary") or []
        if isinstance(b, dict) and b.get("id") and b["id"] not in seen_boundary
    ]
    referrals = build_boundary_referrals(
        boundary, resolver.scope_summary(), debt.get("target_sha"))

    ticketed = [i for t in tickets for i in t["debt_ids"]]
    envelope = resolver.stamp({
        "schema": SCHEMA,
        "generated_at": now,
        "debt_file": str(debt_file),
        "debt_sha256": hashlib.sha256(Path(debt_file).read_bytes()).hexdigest(),
        "debt_target_sha": debt.get("target_sha"),
        # F1: the full id population at plan time (items + boundary section).
        # verify uses this to tell a NEW id (moved/appeared work) from one
        # that already existed when the plan was cut.
        "baseline_ids": sorted(
            {i["id"] for i in items}
            | {b["id"] for b in debt.get("boundary") or []
               if isinstance(b, dict) and b.get("id")}
        ),
        "budget": {
            "count": budget_count,
            "severity_floor": severity_floor,
            "cluster_cap": cluster_cap,
        },
        "clustering": {"precedence": CLUSTERING_PRECEDENCE},
        "counts": {
            "inventory_items": len(items),
            "ticketed_items": len(ticketed),
            "excluded_items": len(excluded),
            "boundary_referrals": len(referrals),
            "grandfathered_ticketed": sum(len(t["grandfathered_ids"]) for t in tickets),
        },
        "tickets": tickets,
        "excluded": excluded,
        "boundary_referrals": referrals,
    })
    return envelope


def render_plan_md(plan: dict) -> str:
    lines = ["# Remediation plan (from debt inventory)", ""]
    b = plan["budget"]
    budget_bits = [f"count={b['count']}" if b["count"] is not None else None,
                   f"severity-floor={b['severity_floor']}" if b["severity_floor"] else None,
                   f"cluster-cap={b['cluster_cap']}" if b["cluster_cap"] is not None else None]
    lines.append(f"Budget: {', '.join(x for x in budget_bits if x)}. "
                 f"{plan['counts']['ticketed_items']} item(s) ticketed, "
                 f"{plan['counts']['excluded_items']} excluded, "
                 f"{plan['counts']['boundary_referrals']} boundary referral(s). "
                 "Leftover inventory is the normal end state.")
    lines.append("")
    lines.append("## Tickets (dependency-ordered)")
    for t in plan["tickets"]:
        r = t["severity_rollup"]
        dep = f" — depends on {', '.join(t['depends_on'])}" if t["depends_on"] else ""
        gf = f" [{len(t['grandfathered_ids'])} grandfathered]" if t["grandfathered_ids"] else ""
        lines.append(f"- **{t['id']}** `{t['kind']}` {t['title']}{dep}{gf}")
        lines.append(f"  - debt ids: {', '.join(t['debt_ids'])}")
        lines.append(f"  - severity: {r.get('high', 0)} high / {r.get('medium', 0)} medium / {r.get('low', 0)} low")
        lines.append(f"  - pathset: {', '.join(t['pathset']['paths']) or '(none)'}")
        if t.get("boundary_locations"):
            lines.append("  - boundary locations (out-of-scope, informational, "
                         "never sanctioned): " + ", ".join(t["boundary_locations"]))
    if plan["excluded"]:
        lines.append("")
        lines.append("## Excluded (deliberately left behind)")
        for e in plan["excluded"]:
            lines.append(f"- {e['id']} [{e['severity']}] — {e['reason']}")
    if plan["boundary_referrals"]:
        lines.append("")
        lines.append("## Boundary referrals (owning team; never auto-fixed)")
        for r in plan["boundary_referrals"]:
            lines.append(f"- {r['id']} [{r['severity']}] — {r['issue_title']}")
    return "\n".join(lines) + "\n"


# --- pathset ------------------------------------------------------------------


def derive_pathset(ticket: dict, extra_collateral: list[str] | None = None) -> dict:
    """One ticket's sanctioned pathset in the exact shape ``ratchet.py
    pathset --pathset-file`` consumes: item location files + declared
    collateral (the ticket's ``collateral`` slot + any ``--collateral``
    args)."""
    paths = list(ticket.get("pathset", {}).get("paths") or [])
    paths += list(ticket.get("collateral") or [])
    paths += list(extra_collateral or [])
    source = (ticket.get("pathset", {}).get("source")
              or f"plan_from_debt {ticket.get('id', '?')}")
    return {"paths": sorted(dict.fromkeys(paths)), "source": source}


# --- verify -------------------------------------------------------------------


def _postdates_plan(entry_ts: str | None, plan_ts: str | None) -> bool:
    """True iff the grandfather entry's timestamp strictly postdates the
    plan's created_at (#216 F8). Fail closed: a missing/unparseable timestamp
    on EITHER side never waives — a pre-#216 entry (no created_at) or a
    hand-built plan (no generated_at) cannot silently amnesty a ticketed id."""
    if not isinstance(entry_ts, str) or not isinstance(plan_ts, str):
        return False
    try:
        return datetime.fromisoformat(entry_ts) > datetime.fromisoformat(plan_ts)
    except ValueError:
        return False


def verify_plan(plan: dict, fresh_debt: dict,
                pending_ids: set[str] | frozenset[str] = frozenset()) -> dict:
    """The /close-epic acceptance check: every TICKETED id gone from the fresh
    inventory, or explicitly waived POST-plan. Only ticketed ids are checked —
    budgeted-out leftovers and boundary referrals are the normal end state.

    **Moved is not resolved (#216 F1):** a ticketed id absent from the fresh
    inventory only counts resolved if NO new item — id not in the plan's
    recorded ``baseline_ids`` — carries the SAME content anchor. Anchors are
    path-independent (clone classes: the member-content hash), so a ``git
    mv`` that renames the finding's file without fixing it is caught as
    ``moved`` (old id -> new id + new location) and counted UNRESOLVED.
    Stated boundary: marker REWORDING mints a new anchor and is not caught
    here — it lands in the informational new-ids report instead.

    **Candidates resolve against the pending store (#216 F2):** a ticketed
    candidate id is resolved IFF absent from ``pending_ids`` (the operator
    ran ``debt_inventory.py resolve-candidate``), never by its absence from
    the fresh inventory.

    **A waiver must be explicit relative to the plan:** an id that was
    ALREADY grandfathered at plan time was ticketed knowingly (remediating it
    before expiry was the point), so its survival is unresolved, not waived.
    A waiving grandfather entry must both be non-expired AND carry a
    timestamp that POSTDATES the plan (#216 F8) — only the loud
    ``adopt.py grandfather --extend`` operator act mints those; entries
    without timestamps never waive.

    **Informational (never a failure):** NEW ids whose location files
    intersect a ticket's pathset files are listed in ``new_in_ticket_files``
    — new debt appeared in the ticket's own files; review before closing."""
    plan_grandfathered = {i for t in plan.get("tickets") or []
                          for i in t.get("grandfathered_ids") or []}
    tickets = plan.get("tickets") or []
    ticketed: dict[str, str] = {}
    plan_item_meta: dict[str, dict] = {}
    for t in tickets:
        for i in t.get("debt_ids") or []:
            ticketed.setdefault(i, t.get("id", "?"))
        for item in t.get("items") or []:
            if isinstance(item, dict) and item.get("id"):
                plan_item_meta.setdefault(item["id"], item)

    fresh = {i["id"]: i for i in fresh_debt.get("items") or []
             if isinstance(i, dict) and i.get("id")}
    baseline = set(plan.get("baseline_ids") or [])
    new_items = [i for iid, i in sorted(fresh.items())
                 if iid not in baseline and iid not in ticketed]
    anchor_of_new: dict[str, dict] = {}
    for i in new_items:
        if i.get("anchor"):
            anchor_of_new.setdefault(i["anchor"], i)

    plan_ts = plan.get("generated_at")
    resolved, waived, unresolved, moved = [], [], [], []
    for iid, tid in sorted(ticketed.items()):
        meta = plan_item_meta.get(iid) or {}
        if meta.get("candidate"):
            if iid in pending_ids:
                unresolved.append({
                    "id": iid, "ticket": tid,
                    "severity": meta.get("severity"),
                    "detail": "candidate still in the pending store — resolving "
                              "it is the explicit operator act "
                              "(debt_inventory.py resolve-candidate)"})
            else:
                resolved.append(iid)
            continue
        item = fresh.get(iid)
        if item is None:
            match = anchor_of_new.get(meta.get("anchor") or "")
            if match is not None:
                moved.append({
                    "id": iid, "ticket": tid, "new_id": match["id"],
                    "new_locations": match.get("locations") or [],
                    "detail": (match.get("detail") or "")[:100]})
            else:
                resolved.append(iid)
        elif (item.get("grandfathered") and not expired_live(item)
              and iid not in plan_grandfathered
              and _postdates_plan(item.get("grandfather_created_at"), plan_ts)):
            waived.append({"id": iid, "ticket": tid,
                           "expiry": item.get("grandfather_expiry")})
        else:
            unresolved.append({"id": iid, "ticket": tid,
                               "severity": item.get("severity"),
                               "detail": (item.get("detail") or "")[:100]})

    # (c) informational: NEW ids that landed in a ticket's own pathset files.
    new_in_ticket_files = []
    for i in new_items:
        files = set(_item_files(i))
        hit_tickets = sorted({
            t.get("id", "?") for t in tickets
            if files & set((t.get("pathset") or {}).get("paths") or [])
        })
        if hit_tickets:
            new_in_ticket_files.append({
                "id": i["id"], "tickets": hit_tickets,
                "locations": i.get("locations") or [],
                "detail": (i.get("detail") or "")[:100]})

    return {
        "ticketed": len(ticketed),
        "resolved": resolved,
        "waived": waived,
        "unresolved": unresolved,
        "moved": moved,
        "new_in_ticket_files": new_in_ticket_files,
        "ok": not unresolved and not moved,
    }


# --- CLI ----------------------------------------------------------------------


def _resolve(args) -> tuple[str, artifacts.Resolver]:
    target = resolve_target(args.owner_repo, args.repo)
    return target, artifacts.Resolver.resolve(target)


def cmd_plan(args) -> int:
    if args.budget_count is None and args.budget_severity_floor is None \
            and args.budget_cluster_cap is None:
        print(f"plan_from_debt: {BUDGET_REFUSAL}", file=sys.stderr)
        return 2
    if (args.budget_count is None and args.budget_cluster_cap is None
            and args.budget_severity_floor == SEVERITY_ORDER[0]):
        # F5/C3: a floor of 'low' excludes nothing — alone it is a vacuous
        # budget, not a bound.
        print(f"plan_from_debt: {FLOOR_LOW_REFUSAL}", file=sys.stderr)
        return 2
    target, resolver = _resolve(args)
    debt_file = Path(args.debt) if args.debt else resolver.quality_dir() / "debt.json"
    try:
        debt = load_debt(debt_file)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"plan_from_debt: {exc} — run scripts/debt_inventory.py first", file=sys.stderr)
        return 1

    stale = resolver.check_stale(debt)
    if stale:
        print(f"plan_from_debt: WARNING: {stale}", file=sys.stderr)

    plan = build_plan(
        debt, str(debt_file), resolver,
        budget_count=args.budget_count,
        severity_floor=args.budget_severity_floor,
        cluster_cap=args.budget_cluster_cap,
    )
    out_dir = Path(args.out).expanduser() if args.out else resolver.quality_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / PLAN_NAME).write_text(json.dumps(plan, indent=2) + "\n")
    (out_dir / PLAN_MD_NAME).write_text(render_plan_md(plan))
    print(f"[plan-from-debt] target: {target}", file=sys.stderr)
    print(f"[plan-from-debt] wrote:  {out_dir / PLAN_NAME}", file=sys.stderr)
    if args.format == "json":
        print(json.dumps(plan, indent=2))
    else:
        print(render_plan_md(plan))
    return 0


def _load_ticket(args) -> dict:
    if args.ticket:
        doc = json.loads(Path(args.ticket).read_text())
        if isinstance(doc, dict) and "pathset" in doc:
            return doc
        raise ValueError(f"{args.ticket} is not a ticket object (no pathset)")
    plan = json.loads(Path(args.plan).read_text())
    for t in plan.get("tickets") or []:
        if t.get("id") == args.id:
            return t
    raise ValueError(f"ticket {args.id} not found in {args.plan}")


def cmd_pathset(args) -> int:
    try:
        ticket = _load_ticket(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"plan_from_debt: {exc}", file=sys.stderr)
        return 1
    spec = derive_pathset(ticket, args.collateral)
    text = json.dumps(spec, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
        print(f"[plan-from-debt] pathset for {ticket.get('id')}: {args.output}", file=sys.stderr)
    else:
        print(text, end="")
    return 0


def cmd_verify(args) -> int:
    try:
        plan = json.loads(Path(args.plan).read_text())
        fresh = load_debt(args.debt)
    except (OSError, ValueError, json.JSONDecodeError, FileNotFoundError) as exc:
        print(f"plan_from_debt: {exc}", file=sys.stderr)
        return 2

    # Ticketed CANDIDATE ids resolve against the pending store (#216 F2) —
    # only resolve the target (and read the store) when the plan has any.
    has_candidates = any(
        isinstance(item, dict) and item.get("candidate")
        for t in plan.get("tickets") or [] for item in t.get("items") or []
    )
    pending_ids: set[str] = set()
    if has_candidates:
        target = resolve_target(args.owner_repo, args.repo)
        resolver = artifacts.Resolver.resolve(target)
        pending_ids = {i["id"] for i in debt_inventory.load_pending(resolver)}

    result = verify_plan(plan, fresh, pending_ids=pending_ids)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"remediation verify: {len(result['resolved'])}/{result['ticketed']} "
              f"ticketed DEBT- id(s) resolved, {len(result['waived'])} waived, "
              f"{len(result['moved'])} moved, "
              f"{len(result['unresolved'])} unresolved")
        for w in result["waived"]:
            print(f"  WAIVED {w['id']} ({w['ticket']}) — post-plan grandfather, "
                  f"expiry {w['expiry']} (adopt.py grandfather --extend)")
        for m in result["moved"]:
            print(f"  MOVED {m['id']} -> {m['new_id']} ({m['ticket']}) at "
                  f"{', '.join(m['new_locations']) or '?'} — same content anchor "
                  "at a new path: renamed, not resolved")
        for u in result["unresolved"]:
            print(f"  UNRESOLVED {u['id']} ({u['ticket']}) [{u['severity']}] {u['detail']}")
        for n in result["new_in_ticket_files"]:
            print(f"  NEW {n['id']} in {', '.join(n['tickets'])} pathset file(s) "
                  f"({', '.join(n['locations']) or '?'}) — new debt appeared in "
                  "the ticket's own files; review before closing (informational)")
    if not result["ok"]:
        print("plan_from_debt: ticketed DEBT- ids remain (present, moved, or "
              "still pending) — the remediation epic's acceptance test FAILED "
              "(fix them, resolve candidates explicitly, or waive via "
              "adopt.py grandfather --extend)", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="remediation-epic planning from the debt inventory (#216)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="cluster DEBT- items into a budgeted, "
                                     "dependency-ordered ticket plan")
    sp.add_argument("owner_repo", nargs="?", default=None)
    sp.add_argument("--repo", default=None, help="direct local repo path")
    sp.add_argument("--debt", default=None,
                    help="debt.json path (default: resolver quality dir)")
    sp.add_argument("--budget-count", type=_positive_int, default=None,
                    help="max tickets in the epic (>= 1)")
    sp.add_argument("--budget-severity-floor", choices=list(SEVERITY_ORDER), default=None,
                    help="items below this severity are excluded ('low' alone "
                         "excludes nothing — combine with a count/cap)")
    sp.add_argument("--budget-cluster-cap", type=_positive_int, default=None,
                    help="max items per ticket (>= 1)")
    sp.add_argument("--out", default=None,
                    help="directory for remediation-plan.{json,md} (default: "
                         "resolver quality dir)")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(fn=cmd_plan)

    sp = sub.add_parser("pathset", help="emit one ticket's sanctioned pathset "
                                        "(the shape ratchet.py pathset consumes)")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticket", default=None, help="JSON file holding ONE ticket object")
    g.add_argument("--plan", default=None, help="remediation-plan.json (with --id)")
    sp.add_argument("--id", default=None, help="ticket id within --plan (e.g. RT-001)")
    sp.add_argument("--collateral", action="append", default=[],
                    help="declared collateral path/glob (repeatable) — callers/tests "
                         "that must move with the refactor")
    sp.add_argument("-o", "--output", default=None)
    sp.set_defaults(fn=cmd_pathset)

    sp = sub.add_parser("verify", help="/close-epic acceptance: every ticketed "
                                       "DEBT- id gone from a fresh inventory "
                                       "(moved-not-resolved caught by anchor; "
                                       "candidates checked against the pending store)")
    sp.add_argument("owner_repo", nargs="?", default=None)
    sp.add_argument("--repo", default=None,
                    help="direct local repo path (needed to read the pending "
                         "candidate store when the plan tickets candidates)")
    sp.add_argument("--plan", required=True, help="remediation-plan.json")
    sp.add_argument("--debt", required=True, help="FRESH debt.json (re-run the inventory)")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(fn=cmd_verify)

    args = parser.parse_args()
    if args.cmd == "pathset" and args.plan and not args.id:
        parser.error("--plan requires --id")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
