#!/usr/bin/env python3
"""Architecture model checker (#174): STATIC consistency of the DECLARED system model.

``docs/system/architecture.json`` (schema:
``templates/formal-models/architecture-schema.json``) declares a C4-flavored
system model: ``ARC-`` deployables/externals (nodes) and ``EDG-`` connectors
(edges) between them. This checker proves the declaration is INTERNALLY
CONSISTENT — it never inspects running code. Whether the code actually matches
the declared model is a separate, deliberately deferred question (the
reflexion/extraction conformance gate, #171). Every report — text or JSON —
states this boundary verbatim::

    proves the DECLARED model is internally consistent; does not prove the
    code matches the model

Declaring a system model is cheap; only the extraction/reflexion conformance
machinery is expensive, and that stays out of scope here (ADR-fh-07).

**Three distinct edge meanings** (ADR-fh-01/07), read separately by different
checks: ``criticality`` (hard/soft) is specifically an AVAILABILITY
dependency — a low-tier logging sink may carry sensitive data without being an
availability dependency, and a low-tier auth provider may be
availability-critical without carrying any payload. ``carries`` is a data
class. ``trust_zone_crossing``/``region_crossing`` are a zone/locality
crossing. None of the three implies another.

**CHECKS — frozen inventory (ADR-fh-06).** ``check_architecture`` is the FIFTH
#184 gate. Per ADR-fh-06, its check set freezes as a module-level tuple BEFORE
#184 authors this gate's validation record — a retroactive test
(``tests/test_check_architecture.py`` today; #184's table-driven test later)
asserts one genuinely-passing ``fire`` trial per entry in ``CHECKS``, closing
the gap where a check-specific omission could slip past #184's only-generic
``required_seed_classes`` set. Adding a NEW rule means adding a new entry here
— never silently folding it into an existing one.

**Report-only by default.** Exit ``0`` even WITH findings; ``--gate`` opts
into blocking (exit ``1`` on findings); ``2`` is reserved for genuine USAGE
errors (bad flags/paths) — a malformed/unparseable ``architecture.json``, or
any other consistency-rule violation, is a FINDING (exit 0 report-only / 1
under ``--gate``), never a usage error. Absent ``architecture.json`` exits 0
with a distinct "no architecture model found" note — "not checked" is never
conflated with "passed", so ``/architect`` can adopt this incrementally.
Absent optional cross-artifacts (``--system-contracts``) degrade the same way:
reported ``not_checked``, never silently "passed".

``--scanner-version`` prints a hash-derived version
(``chief_wiggum.hashing.scanner_version``) covering this module and every
``chief_wiggum`` dependency whose logic affects findings, and exits — this is
what makes a stale #184 validation record structurally detectable (INV-fh-005).

Follows ``check_budget_tree.py``'s bespoke, stdlib-only ``_validate_value``
schema walker (no ``jsonschema`` dependency in gate scripts) so every schema
finding carries a JSON path.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.hashing import scanner_version  # noqa: E402

DEFAULT_SCHEMA = (
    Path(__file__).resolve().parents[1] / "templates" / "formal-models" / "architecture-schema.json"
)

AUTHORITY = (
    "proves the DECLARED model is internally consistent; does not prove the "
    "code matches the model"
)

# --- CHECKS: frozen inventory (ADR-fh-06) -----------------------------------
# One canonical seed class per entry, in the order ADR-fh-06 lists them. This
# tuple is the contract #184 tests against — do not reorder/rename entries
# once a #184 validation record exists; add a NEW entry for a new rule.
# @cw-trace guards CTR-fh-026
CHECKS = (
    "dangling-endpoint",        # edge.from/to does not resolve to a declared node
    "retired-node-edge",        # an ACTIVE edge touches a retired node
    "unlabelled-external",      # external node reached by a hard edge has no asm_refs
    "tier-inversion",           # a tier-1 hard-availability path reaches a lower tier
    "label-propagation",        # carries x trust_zone/region rule violated, unwaived
    "undeclared-cross-ref",     # system-contracts.json names an undeclared ARC-/EDG-/binding
    "missing-tier",             # node has no criticality_tier
    "authored-crossing-label",  # trust_zone_crossing/region_crossing authored non-null
)

# Data-class lattice (contracts.md ArchitectureModel entity): a MONOTONE
# comparison, never string equality — public < internal < pii < secret <
# official-sensitive.
DATA_CLASS_RANK = {"public": 0, "internal": 1, "pii": 2, "secret": 3, "official-sensitive": 4}
# The highest data-class rank a trust_zone may legitimately RECEIVE. A
# `carries` label whose rank exceeds the target edge's `to` zone's ceiling is
# a label-propagation finding (declared-graph only — NO taint analysis).
# 'dmz' (a public-facing ingest/gateway) may legitimately see PII in transit
# (that's normal for any TLS-terminating ingest) but never secret/
# official-sensitive without stepping into a more trusted zone first.
TRUST_ZONE_CEILING = {"public": 0, "dmz": 2, "internal": 3, "restricted": 4}
EVIDENCE_CHOICES = ("sla-doc", "live-probe", "justified")
TIER_RANK = {"tier-1": 1, "tier-2": 2, "tier-3": 3}
ASM_ID_RE = re.compile(r"^ASM-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3}$")
CROSS_REF_ID_RE = re.compile(r"^(ARC|EDG)-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3}$")


# --- report data model -------------------------------------------------------


@dataclass
class Finding:
    check: str  # one of CHECKS, or "schema" for a structural/schema violation
    id: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Waiver:
    check: str
    id: str
    asm_id: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NotChecked:
    artifact: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DerivedLabel:
    edge: str
    trust_zone_crossing: str | None
    region_crossing: bool | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ArchitectureReport:
    model_present: bool = True
    nodes: int = 0
    edges: int = 0
    findings: list[Finding] = field(default_factory=list)
    waivers: list[Waiver] = field(default_factory=list)
    not_checked: list[NotChecked] = field(default_factory=list)
    derived_labels: list[DerivedLabel] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Gateable status: no findings. Waivers, not_checked notes, and
        derived-label bookkeeping never affect this."""
        return not self.findings

    @property
    def authority(self) -> str:
        return AUTHORITY

    @property
    def counts(self) -> dict:
        by_check: dict[str, int] = {}
        for f in self.findings:
            by_check[f.check] = by_check.get(f.check, 0) + 1
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "findings": len(self.findings),
            "by_check": by_check,
            "waivers": len(self.waivers),
        }

    def to_dict(self) -> dict:
        return {
            "model_present": self.model_present,
            "authority": self.authority,
            "ok": self.ok,
            "counts": self.counts,
            "findings": [f.to_dict() for f in self.findings],
            "waivers": [w.to_dict() for w in self.waivers],
            "not_checked": [n.to_dict() for n in self.not_checked],
            "derived_labels": [d.to_dict() for d in self.derived_labels],
        }


# --- loading ------------------------------------------------------------------


def load_schema(path: Path = DEFAULT_SCHEMA) -> dict:
    return json.loads(Path(path).read_text())


def load_doc(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


# --- schema validation --------------------------------------------------------
#
# Bespoke, stdlib-only structural validator (mirrors check_budget_tree.py):
# walks the schema's $ref/type/required/properties/additionalProperties/
# enum/pattern/minimum/maximum/items keywords against the document. Violations
# are "schema"-category findings, gateable like any other finding.


def _resolve_ref(schema_node: dict, root_schema: dict) -> dict:
    while isinstance(schema_node, dict) and "$ref" in schema_node:
        target: object = root_schema
        for part in schema_node["$ref"].lstrip("#/").split("/"):
            target = target[part]  # type: ignore[index]
        schema_node = target  # type: ignore[assignment]
    return schema_node


def _validate_value(value, schema_node: dict, path: str, root_schema: dict, errors: list[tuple[str, str]]) -> None:
    schema_node = _resolve_ref(schema_node, root_schema)
    expected = schema_node.get("type")

    if "enum" in schema_node and value not in schema_node["enum"]:
        errors.append((path, f"value {value!r} not in allowed set {schema_node['enum']}"))
        return

    if isinstance(expected, list):
        # e.g. ["string", "null"] — accept if value matches any listed type.
        if value is None and "null" in expected:
            return
        expected = next((t for t in expected if t != "null"), None)

    if expected == "object":
        if not isinstance(value, dict):
            errors.append((path, f"expected object, got {type(value).__name__}"))
            return
        for req in schema_node.get("required", []):
            if req not in value:
                errors.append((path, f"missing required field '{req}'"))
        props = schema_node.get("properties", {})
        if schema_node.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append((path, f"unknown field '{key}'"))
        for key, sub in value.items():
            if key in props:
                _validate_value(sub, props[key], f"{path}.{key}", root_schema, errors)
    elif expected == "array":
        if not isinstance(value, list):
            errors.append((path, f"expected array, got {type(value).__name__}"))
            return
        min_items = schema_node.get("minItems")
        if min_items is not None and len(value) < min_items:
            errors.append((path, f"expected at least {min_items} item(s), got {len(value)}"))
        item_schema = schema_node.get("items")
        if item_schema:
            for i, item in enumerate(value):
                _validate_value(item, item_schema, f"{path}[{i}]", root_schema, errors)
    elif expected == "string":
        if not isinstance(value, str):
            errors.append((path, f"expected string, got {type(value).__name__}"))
            return
        pattern = schema_node.get("pattern")
        if pattern and not re.search(pattern, value):
            errors.append((path, f"value {value!r} does not match pattern {pattern}"))
        min_length = schema_node.get("minLength")
        if min_length is not None and len(value) < min_length:
            errors.append((path, f"string {value!r} shorter than minLength {min_length}"))
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append((path, f"expected number, got {type(value).__name__}"))
            return
        minimum, maximum = schema_node.get("minimum"), schema_node.get("maximum")
        if minimum is not None and value < minimum:
            errors.append((path, f"value {value} below minimum {minimum}"))
        if maximum is not None and value > maximum:
            errors.append((path, f"value {value} above maximum {maximum}"))
    elif expected == "boolean":
        if not isinstance(value, bool):
            errors.append((path, f"expected boolean, got {type(value).__name__}"))


def validate_doc(doc, schema: dict) -> list[Finding]:
    """Structurally validate an architecture document against the JSON schema.

    Returns ``schema``-category findings for every violation — a malformed or
    unparseable document is a FINDING (report-only exit 0 / --gate exit 1),
    never a usage error.
    @cw-trace guards CTR-fh-020"""
    errors: list[tuple[str, str]] = []
    _validate_value(doc, schema, "$", schema, errors)
    return [Finding("schema", path, f"{path}: {msg}") for path, msg in errors]


# --- consistency checks (CHECKS) ----------------------------------------------


def _check_missing_tier(nodes: dict, report: ArchitectureReport) -> None:
    """A node with no criticality_tier is a FINDING, never a silently-skipped
    node — else a node could opt itself out of the tier-inversion check by
    simply omitting its tier.
    @cw-trace guards CTR-fh-022"""
    for nid, node in nodes.items():
        if not node.get("criticality_tier"):
            report.findings.append(
                Finding("missing-tier", nid or "<node>", f"node {nid}: no criticality_tier declared")
            )


def _check_dangling_endpoints(nodes: dict, edges: list[dict], report: ArchitectureReport) -> None:
    """@cw-trace guards CTR-fh-021"""
    for e in edges:
        eid = e.get("id", "<edge>")
        missing = [end for end in ("from", "to") if e.get(end) not in nodes]
        if missing:
            bad = ", ".join(f"{end}={e.get(end)!r}" for end in missing)
            report.findings.append(
                Finding("dangling-endpoint", eid, f"edge {eid}: {bad} does not resolve to a declared node")
            )


def _check_retired_node_edges(nodes: dict, edges: list[dict], report: ArchitectureReport) -> None:
    """No ACTIVE edge may reference a retired node — a declared-but-inactive
    edge (``active: false``) is exempt (a documented, retired path)."""
    for e in edges:
        eid = e.get("id", "<edge>")
        if e.get("active", True) is False:
            continue
        for end in ("from", "to"):
            node = nodes.get(e.get(end))
            if node is not None and node.get("status") == "retired":
                report.findings.append(
                    Finding(
                        "retired-node-edge",
                        eid,
                        f"edge {eid}: ACTIVE edge references retired node {node.get('id')} ({end})",
                    )
                )


def _check_unlabelled_external(nodes: dict, edges: list[dict], report: ArchitectureReport) -> None:
    """An external node reached by a HARD (availability) edge is the whole
    point of an ASM reference — an unlabelled vendor dependency is a
    finding."""
    flagged: set[str] = set()
    for e in edges:
        if e.get("criticality") != "hard":
            continue
        eid = e.get("id", "<edge>")
        for end in ("from", "to"):
            node = nodes.get(e.get(end))
            if node is None:
                continue
            node_id = node.get("id")
            if node_id in flagged:
                continue
            if node.get("external") and not node.get("asm_refs"):
                flagged.add(node_id)
                report.findings.append(
                    Finding(
                        "unlabelled-external",
                        node_id,
                        f"external node {node_id} is reached by hard edge {eid} but declares no asm_refs",
                    )
                )


def _check_tier_inversion(nodes: dict, edges: list[dict], report: ArchitectureReport) -> None:
    """No hard-availability-dependency path may run from a tier-1 node through
    a lower-criticality node. Modeled as reachability over the ACTIVE, HARD
    edge subgraph starting at every tier-1 node: any node reached with a
    lower tier (tier-2/tier-3) is a violation, whether it is the final hop or
    an intermediate one the path merely passes 'through' — both make the
    tier-1 node's availability transitively depend on a lower-tier
    component."""
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for e in edges:
        if e.get("criticality") != "hard" or e.get("active", True) is False:
            continue
        frm, to = e.get("from"), e.get("to")
        if frm in nodes and to in nodes:
            adjacency.setdefault(frm, []).append((to, e.get("id", "<edge>")))

    tier1_roots = [nid for nid, n in nodes.items() if TIER_RANK.get(n.get("criticality_tier")) == 1]
    flagged: set[tuple[str, str]] = set()
    for root in tier1_roots:
        visited = {root}
        queue = [root]
        while queue:
            cur = queue.pop(0)
            for nxt, eid in adjacency.get(cur, []):
                if nxt in visited:
                    continue
                visited.add(nxt)
                queue.append(nxt)
                rank = TIER_RANK.get(nodes[nxt].get("criticality_tier"))
                if rank is not None and rank > 1 and (root, nxt) not in flagged:
                    flagged.add((root, nxt))
                    report.findings.append(
                        Finding(
                            "tier-inversion",
                            nxt,
                            f"tier-1 node {root} has a hard-availability-dependency path (via {eid}) "
                            f"reaching lower-tier node {nxt} ({nodes[nxt].get('criticality_tier')})",
                        )
                    )


def _valid_asm(ref) -> bool:
    return (
        isinstance(ref, dict)
        and bool(ASM_ID_RE.match(str(ref.get("id", ""))))
        and ref.get("evidence") in EVIDENCE_CHOICES
        and bool(ref.get("ref"))
    )


def _check_label_propagation(nodes: dict, edges: list[dict], report: ArchitectureReport) -> None:
    """Declared-graph-only label propagation (NO taint analysis): a connector
    must not `carries` a data class into a trust_zone/region the target's
    labels forbid. A violation with a valid `asm_refs` waiver on the SAME
    edge is recorded as a documented waiver, not a silent pass and not a hard
    finding (mirrors check_budget_tree.py's covered/waived/missing ASM
    statuses)."""
    for e in edges:
        eid = e.get("id", "<edge>")
        frm, to = nodes.get(e.get("from")), nodes.get(e.get("to"))
        if frm is None or to is None:
            continue  # already reported by dangling-endpoint
        carries = e.get("carries") or []
        if not carries:
            continue
        max_class = max(carries, key=lambda c: DATA_CLASS_RANK.get(c, 0))
        max_rank = DATA_CLASS_RANK.get(max_class, 0)

        violations = []
        to_zone = to.get("trust_zone")
        ceiling = TRUST_ZONE_CEILING.get(to_zone)
        if ceiling is not None and max_rank > ceiling:
            violations.append(f"carries {max_class!r} into trust_zone {to_zone!r} which forbids it")

        from_region, to_region = frm.get("region"), to.get("region")
        region_crossing = from_region is not None and to_region is not None and from_region != to_region
        if region_crossing and max_rank >= DATA_CLASS_RANK["secret"]:
            violations.append(f"carries {max_class!r} across a region boundary ({from_region} -> {to_region})")

        if not violations:
            continue

        asm_refs = e.get("asm_refs") or []
        waiver_ref = next((a for a in asm_refs if _valid_asm(a)), None)
        message = f"edge {eid}: " + "; ".join(violations)
        if waiver_ref is not None:
            report.waivers.append(Waiver("label-propagation", eid, waiver_ref.get("id"), message))
        else:
            report.findings.append(Finding("label-propagation", eid, message + " (no valid asm_refs waiver)"))


def _check_authored_crossing_labels(edges: list[dict], report: ArchitectureReport) -> None:
    """trust_zone_crossing/region_crossing are COMPUTED-ONLY. The schema
    permits the null placeholder; ANY authored non-null value is itself a
    finding — a hand-authored 'safe' label could otherwise mask a real
    trust-zone violation (INV-fh-006).
    @cw-trace ensures INV-fh-006 CTR-fh-025"""
    for e in edges:
        eid = e.get("id", "<edge>")
        if e.get("trust_zone_crossing") is not None:
            report.findings.append(
                Finding(
                    "authored-crossing-label",
                    eid,
                    f"edge {eid}: trust_zone_crossing is AUTHORED ({e.get('trust_zone_crossing')!r}) — "
                    "this field is computed-only, never authored",
                )
            )
        if e.get("region_crossing") is not None:
            report.findings.append(
                Finding(
                    "authored-crossing-label",
                    eid,
                    f"edge {eid}: region_crossing is AUTHORED ({e.get('region_crossing')!r}) — "
                    "this field is computed-only, never authored",
                )
            )


def _compute_derived_labels(nodes: dict, edges: list[dict], report: ArchitectureReport) -> None:
    """Always computes trust_zone_crossing/region_crossing for every
    resolvable edge — informational, reports-never-mutates (the checker opens
    architecture.json read-only; it never writes the authored file).
    @cw-trace ensures INV-fh-006 CTR-fh-025 CTR-fh-023"""
    for e in edges:
        eid = e.get("id", "<edge>")
        frm, to = nodes.get(e.get("from")), nodes.get(e.get("to"))
        if frm is None or to is None:
            continue
        tz_crossing = None
        if frm.get("trust_zone") != to.get("trust_zone"):
            tz_crossing = f"{frm.get('trust_zone')}->{to.get('trust_zone')}"
        region_crossing = None
        if frm.get("region") is not None and to.get("region") is not None:
            region_crossing = frm.get("region") != to.get("region")
        report.derived_labels.append(DerivedLabel(eid, tz_crossing, region_crossing))


def _walk_budget_nodes(node, telemetry_refs: set) -> None:
    if not isinstance(node, dict):
        return
    ref = node.get("telemetry_ref")
    if ref:
        telemetry_refs.add(ref)
    for child in node.get("children") or []:
        _walk_budget_nodes(child, telemetry_refs)
    residual = node.get("residual")
    if residual:
        _walk_budget_nodes(residual, telemetry_refs)


def _check_cross_artifact(nodes: dict, edges: list[dict], system_contracts: dict, report: ArchitectureReport) -> None:
    """Every node/connector referenced by system-contracts.json budget-tree
    `chains` and telemetry bindings must name a declared ARC-/EDG- in
    architecture.json (INV-fh-008) — neither model may silently invent the
    other's nodes.
    @cw-trace guards INV-fh-008"""
    if not isinstance(system_contracts, dict):
        return
    edge_ids = set(edges and [e.get("id") for e in edges] or [])
    node_ids = set(nodes.keys())

    for chain in system_contracts.get("chains") or []:
        cid = chain.get("id", "<chain>")
        for hop in chain.get("hops") or []:
            if not isinstance(hop, dict):
                continue  # malformed hop shape is check_budget_tree's schema concern
            for role in ("caller", "callee"):
                token = hop.get(role)
                if not token:
                    continue  # absent endpoint is check_budget_tree's schema concern
                token = str(token)
                if not CROSS_REF_ID_RE.match(token):
                    # A legacy plain service-name hop ("gateway", "billing-api")
                    # cannot be cross-checked against the declared model at all —
                    # silently skipping it would recreate exactly the INV-fh-008
                    # blind spot this check exists to close. Visible finding:
                    # declare the node/edge and reference its ARC-/EDG- id.
                    report.findings.append(
                        Finding(
                            "undeclared-cross-ref",
                            token,
                            f"system-contracts.json chain {cid} hop.{role}={token!r} is not an "
                            "ARC-/EDG- id — a plain service name cannot be resolved against "
                            "architecture.json; declare the node/edge and reference its id",
                        )
                    )
                    continue
                kind = token.split("-", 1)[0]
                pool = node_ids if kind == "ARC" else edge_ids
                if token not in pool:
                    report.findings.append(
                        Finding(
                            "undeclared-cross-ref",
                            token,
                            f"system-contracts.json chain {cid} hop.{role}={token} is not a declared "
                            f"{kind}- node/edge in architecture.json",
                        )
                    )

    declared_emits: set = set()
    for n in nodes.values():
        declared_emits.update(n.get("emits") or [])
    if declared_emits:
        telemetry_refs: set = set()
        for tree in system_contracts.get("trees") or []:
            _walk_budget_nodes(tree.get("root") or {}, telemetry_refs)
        for ref in sorted(telemetry_refs):
            if ref not in declared_emits:
                report.findings.append(
                    Finding(
                        "undeclared-cross-ref",
                        ref,
                        f"system-contracts.json telemetry_ref={ref!r} is not declared in any "
                        "architecture.json node's emits[]",
                    )
                )


# --- top-level check ----------------------------------------------------------


def check_static(
    doc,
    schema: dict | None = None,
    system_contracts: dict | None = None,
    system_contracts_path: str | None = None,
    system_contracts_error: str | None = None,
) -> ArchitectureReport:
    report = ArchitectureReport()
    schema = schema if schema is not None else load_schema()
    report.findings.extend(validate_doc(doc, schema))

    if not isinstance(doc, dict):
        return report

    raw_nodes = doc.get("nodes")
    raw_edges = doc.get("edges")
    # Normalize to dict-only items: a non-dict node/edge already produced a
    # "schema"-category finding via validate_doc above; the graph checks must
    # then run over the well-shaped remainder instead of crashing (a malformed
    # model must ALWAYS end as findings, never a traceback).
    node_list = [n for n in raw_nodes if isinstance(n, dict)] if isinstance(raw_nodes, list) else []
    edges = [e for e in raw_edges if isinstance(e, dict)] if isinstance(raw_edges, list) else []

    # Duplicate stable IDs must be a finding BEFORE dict construction: the
    # last-wins dict below would otherwise silently collapse duplicates (e.g.
    # an active node shadowing a retired duplicate, hiding its retired-edge
    # findings).
    seen_node_ids: set[str] = set()
    for n in node_list:
        nid = n.get("id")
        if not nid:
            continue
        if nid in seen_node_ids:
            report.findings.append(
                Finding("schema", nid, f"duplicate node id {nid}: declared more than once in nodes[]")
            )
        seen_node_ids.add(nid)
    seen_edge_ids: set[str] = set()
    for e in edges:
        eid = e.get("id")
        if not eid:
            continue
        if eid in seen_edge_ids:
            report.findings.append(
                Finding("schema", eid, f"duplicate edge id {eid}: declared more than once in edges[]")
            )
        seen_edge_ids.add(eid)

    nodes = {n.get("id"): n for n in node_list if n.get("id")}
    report.nodes = len(nodes)
    report.edges = len(edges)

    _check_missing_tier(nodes, report)
    _check_dangling_endpoints(nodes, edges, report)
    _check_retired_node_edges(nodes, edges, report)
    _check_unlabelled_external(nodes, edges, report)
    _check_tier_inversion(nodes, edges, report)
    _check_label_propagation(nodes, edges, report)
    _check_authored_crossing_labels(edges, report)
    _compute_derived_labels(nodes, edges, report)

    if system_contracts is not None:
        _check_cross_artifact(nodes, edges, system_contracts, report)
    else:
        report.not_checked.append(
            NotChecked(
                artifact=system_contracts_path or "system-contracts.json",
                reason=system_contracts_error
                or "no --system-contracts given — cross-artifact consistency not checked "
                "(never reported as passed)",
            )
        )

    return report


# --- rendering / CLI --------------------------------------------------------


def render_text(report: ArchitectureReport, note: str | None = None) -> str:
    lines = ["# Architecture Model Report", "", f"Authority: {AUTHORITY}", ""]

    if not report.model_present:
        lines.append(f"Status: NOT CHECKED — {note or 'no architecture model found'}")
        return "\n".join(lines) + "\n"

    c = report.counts
    lines.append(f"Nodes: {c['nodes']}  Edges: {c['edges']}  Findings: {c['findings']}  Waivers: {c['waivers']}")
    lines.append(f"Status: {'OK' if report.ok else 'FINDINGS'}")

    if report.findings:
        lines += ["", "## Findings (gateable under --gate)"]
        lines += [f"- [{f.check}] {f.message}" for f in report.findings]
    if report.waivers:
        lines += ["", "## Waivers (documented ASM evidence, never a finding)"]
        lines += [f"- [{w.check}] {w.message} (waived by {w.asm_id})" for w in report.waivers]
    if report.not_checked:
        lines += ["", "## Not checked (absent optional artifact — never reported as 'passed')"]
        lines += [f"- {n.artifact}: {n.reason}" for n in report.not_checked]
    if report.derived_labels:
        lines += ["", "## Derived crossing labels (computed, never authored)"]
        for d in report.derived_labels:
            lines.append(
                f"- {d.edge}: trust_zone_crossing={d.trust_zone_crossing!r} region_crossing={d.region_crossing!r}"
            )

    return "\n".join(lines) + "\n"


def _scanner_version() -> str:
    """Hash-derived ``--scanner-version``: the source of this module plus its
    ``chief_wiggum`` dependencies. No hand-bumped constant to forget
    (INV-fh-005). Makes check_architecture the fifth #184 gate whose
    validation record can be staleness-checked.
    @cw-trace guards CTR-fh-026 INV-fh-005"""
    here = Path(__file__).resolve()
    cw_dir = here.parent / "chief_wiggum"
    return scanner_version(here, cw_dir / "hashing.py")


def _emit(report: ArchitectureReport) -> None:
    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        import os

        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from factory_log import emit_gate

        emit_gate("check_architecture", "fail" if report.findings else "pass", caught=len(report.findings))
    except Exception:
        pass


def _print(report: ArchitectureReport, fmt: str, note: str | None = None) -> None:
    if fmt == "json":
        out = report.to_dict()
        if note:
            out["note"] = note
        print(json.dumps(out, indent=2))
    else:
        print(render_text(report, note=note))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Architecture model checker: STATIC consistency of the declared system model (#174)"
    )
    parser.add_argument(
        "architecture_file",
        nargs="?",
        default=None,
        help="Path to architecture.json; not required with --scanner-version",
    )
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument(
        "--system-contracts",
        help="Optional docs/system/system-contracts.json for cross-artifact consistency (INV-fh-008); "
        "absent -> reported 'not checked', never 'passed'",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Exit 1 on findings (report-only default: exit 0 with findings printed)",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument(
        "--scanner-version",
        action="store_true",
        help="Print the hash-derived scanner version (source hash of this module + its "
        "chief_wiggum deps) and exit",
    )
    args = parser.parse_args(argv)

    if args.scanner_version:
        print(_scanner_version())
        return 0

    if not args.architecture_file:
        print("Error: architecture_file is required unless --scanner-version is given", file=sys.stderr)
        return 2

    path = Path(args.architecture_file)
    if not path.exists():
        # @cw-trace guards CTR-fh-023 CTR-fh-024
        report = ArchitectureReport(model_present=False)
        _print(report, args.format, note="no architecture model found")
        _emit(report)
        return 0

    try:
        schema = load_schema(Path(args.schema))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error: cannot load schema: {exc}", file=sys.stderr)
        return 2

    try:
        raw_text = path.read_text()
    except OSError as exc:
        print(f"Error: cannot read architecture file: {exc}", file=sys.stderr)
        return 2

    try:
        doc = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        # A malformed document is a FINDING (report-only 0 / --gate 1), never
        # a usage error — only bad CLI flags/paths are usage errors (2).
        # @cw-trace guards CTR-fh-020
        report = ArchitectureReport()
        report.findings.append(Finding("schema", str(path), f"invalid JSON in {path}: {exc}"))
        _print(report, args.format)
        _emit(report)
        return 1 if args.gate else 0

    system_contracts = None
    system_contracts_error = None
    if args.system_contracts:
        try:
            system_contracts = load_doc(args.system_contracts)
        except (OSError, json.JSONDecodeError) as exc:
            system_contracts_error = f"cannot load {args.system_contracts}: {exc}"

    report = check_static(
        doc,
        schema=schema,
        system_contracts=system_contracts,
        system_contracts_path=args.system_contracts,
        system_contracts_error=system_contracts_error,
    )

    _print(report, args.format)
    _emit(report)

    if args.gate and not report.ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
