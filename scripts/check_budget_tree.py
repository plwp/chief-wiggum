#!/usr/bin/env python3
"""Budget tree checker (#164): typed NFR budget trees with sound tail arithmetic.

The first system-layer gate: latency/throughput/spend budgets as checked
contracts, not prose. Pilot use case: a ~800ms mouth-to-ear voice-agent budget
decomposed into endpointing / LLM TTFT / TTS TTFB / transport.

Schema: ``templates/formal-models/system-contracts-schema.json``. Nodes carry a
stable ``BUD-`` ID, ``{kind, unit, bound, alpha, telemetry_ref, children,
residual, asm_refs}``.

**Sound tail arithmetic (the core).** Percentiles do NOT sum. A child set is
consistent with its parent under the default ``union-bound`` arithmetic iff::

    sum(child.alpha) <= parent.alpha                       (tail-probability budget)
    sum(child.bound) + parent.headroom <= parent.bound      (union bound)

This is coherent WITHOUT assuming child latencies are independent. A tree may
opt into ``arithmetic: "naive"`` (plain sum-of-bounds, ignoring alpha) but that
mode is WARN-only and can never gate a workflow — it is unsound for correlated
tails (see the correlated-tails counterexample in the test suite: two children
each bounded at p95=300ms comfortably "pass" a naive 700ms parent by simple
addition, while the union-bound check on their tail-probability budgets can
still show the parent's own alpha is oversubscribed).

Union-bound arithmetic requires alphas to be DECLARED: a non-leaf union-bound
node missing its own ``alpha``, or any child/residual missing ``alpha``, is a
structure finding — missing alphas must never silently degrade the sound check
into a naive sum-of-bounds. Likewise every child and residual must carry the
same ``kind`` and ``unit`` as its parent (a ms parent must not sum usd/tokens
children); a mismatch is a structure finding and the mismatched sums are
skipped.

The document is also validated structurally against the schema
(``required`` fields, ``enum`` values, ``pattern``-ed IDs, numeric bounds,
unknown fields) — violations are ``schema``-category findings, gateable in
static mode like any other structure finding.

**Coverage.** Every node that declares ``children`` MUST also declare a
``residual`` child (an explicit unaccounted-budget bucket) — a missing residual
is a structure finding.

**Timeout monotonicity** (optional ``chains``): each chain's hops (outermost to
innermost) must have strictly decreasing ``timeout_ms`` — an outer caller's
timeout must exceed its nested callee's, else the callee can still be in
flight when the caller has already given up. Violations are WARN-only (never
gateable) and always come with a note that retries/hedging multiply worst-case
occupancy beyond what a single-hop timeout chain models.

**Assumption refs** (``asm_refs``): each entry is ``{id, evidence, ref}``.
``evidence in {sla-doc, live-probe}`` renders "covered"; ``evidence: justified``
renders as a documented WAIVER (a distinct, non-finding status); missing or
invalid evidence is a finding.

**Two modes:**

- ``static`` (default) — well-formedness + arithmetic + monotonicity. Supports
  ``--gate`` (exit 1) on structure/arithmetic findings only — monotonicity and
  naive-arithmetic are WARN-only and never gate.
- ``--measured <file>`` — evaluate declared bounds against a k6 summary export
  (``{"metrics": {name: {...p95...}}}``) or a flat ``{metric: {p95: ...}}``
  export. Each node gets a status:

    - ``unbound``       — the node declares NO ``telemetry_ref``: it is not
                          bound to any metric. A coverage finding, never a pass.
    - ``no_observations`` — a ``telemetry_ref`` is declared but the metric was
                          never observed (missing from the export, or present
                          with an explicit zero count). A FINDING, never a pass.
    - ``held``          — an observation exists and satisfies the bound.
    - ``breached``      — an observation exists and EXCEEDS the declared bound.

  ``unbound`` vs ``no_observations`` is a deliberate distinction (a missing
  binding is a spec gap; a bound metric with no data is a measurement gap);
  ``held`` vs ``breached`` is the bound evaluation for nodes that have data.

  Measured mode is EVIDENCE-ONLY, permanently: it never exits non-zero,
  regardless of ``--gate`` (environment variance means a measured latency claim
  should never hard-block CI — see docs/gate-rollout.md).

  **Optional refinement (#170):** pass ``--emits-report <check_instrumentation
  JSON>`` to distinguish, within ``no_observations``, a binding with a real
  ``@cw-emits`` site that simply wasn't triggered this run from one with NO
  emitter anywhere in source — the latter is reclassified to a new status,
  ``not_emitted`` (a structural gap, not a measurement window). Omitting the
  flag leaves ``unbound``/``no_observations``/``held``/``breached`` exactly as
  they were — this integration is additive and optional, never required.

Every report (text and JSON) carries an ``authority`` line stating exactly what
was proven:

    static:   "static mode proves budget-declaration consistency, not runtime latency"
    measured: "measured mode reports observations from <source>; not a proof of runtime behaviour"

Mirrors ``check_traceability.py``'s shape (dataclasses, report object with
counts/ok, argparse, best-effort factory_log emit). Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_SCHEMA = (
    Path(__file__).resolve().parents[1] / "templates" / "formal-models" / "system-contracts-schema.json"
)

ARITHMETIC_CHOICES = ("union-bound", "naive")
EVIDENCE_CHOICES = ("sla-doc", "live-probe", "justified")
_ALPHA_TOL = 1e-9

STATIC_AUTHORITY = "static mode proves budget-declaration consistency, not runtime latency"
MONOTONICITY_NOTE = (
    "timeout monotonicity checks a single-hop chain only; retries/hedging multiply "
    "worst-case occupancy beyond what this check models"
)


def measured_authority(source: str) -> str:
    return f"measured mode reports observations from {source}; not a proof of runtime behaviour"


# --- report data model -------------------------------------------------------


@dataclass
class Finding:
    category: str  # "structure" | "arithmetic" | "schema"
    id: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Note:
    category: str  # "naive-arithmetic" | "monotonicity"
    id: str | None
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AsmStatus:
    id: str
    node: str
    status: str  # "covered" | "waived" | "missing"
    evidence: str | None
    ref: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MeasuredResult:
    id: str
    telemetry_ref: str | None
    bound: float | None
    observed: float | None
    status: str  # "held" | "breached" | "no_observations" | "not_emitted" | "unbound"
    # None unless an --emits-report was supplied (#170): then True/False records
    # whether ANY @cw-emits site was found for this telemetry_ref, repo-wide.
    emitter_bound: bool | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BudgetTreeReport:
    mode: str  # "static" | "measured"
    source: str | None = None
    trees: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    warnings: list[Note] = field(default_factory=list)
    asm_statuses: list[AsmStatus] = field(default_factory=list)
    measured: list[MeasuredResult] = field(default_factory=list)

    @property
    def counts(self) -> dict:
        return {
            "trees": len(self.trees),
            "findings": len(self.findings),
            "warnings": len(self.warnings),
            "asm_missing": sum(1 for a in self.asm_statuses if a.status == "missing"),
            "asm_waived": sum(1 for a in self.asm_statuses if a.status == "waived"),
            "measured_held": sum(1 for m in self.measured if m.status == "held"),
            "measured_breached": sum(1 for m in self.measured if m.status == "breached"),
            "measured_no_observations": sum(1 for m in self.measured if m.status == "no_observations"),
            "measured_not_emitted": sum(1 for m in self.measured if m.status == "not_emitted"),
            "measured_unbound": sum(1 for m in self.measured if m.status == "unbound"),
        }

    @property
    def ok(self) -> bool:
        """Gateable status. Measured mode is evidence-only and is ALWAYS ok — it
        never blocks a workflow, regardless of what it observed."""
        if self.mode == "measured":
            return True
        return not self.findings

    @property
    def authority(self) -> str:
        return STATIC_AUTHORITY if self.mode == "static" else measured_authority(self.source or "<unknown>")

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "authority": self.authority,
            "trees": self.trees,
            "counts": self.counts,
            "ok": self.ok,
            "findings": [f.to_dict() for f in self.findings],
            "warnings": [w.to_dict() for w in self.warnings],
            "asm_statuses": [a.to_dict() for a in self.asm_statuses],
            "measured": [m.to_dict() for m in self.measured],
        }


# --- loading ------------------------------------------------------------------


def load_schema(path: Path = DEFAULT_SCHEMA) -> dict:
    return json.loads(Path(path).read_text())


def load_doc(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def load_measured(path: str | Path) -> dict[str, dict]:
    """Normalize a k6 summary export (``{"metrics": {name: {...}}}``) or a flat
    ``{metric: {p95: ...}}`` export into ``{metric: {"p95": float|None, "count": int|None}}``.

    Tolerant of the several shapes k6 has shipped: ``values`` may be nested
    (``{"values": {"p(95)": ...}}``) or flat on the metric itself; the p95 key
    may be ``p95``, ``p(95)``, or ``P95``; an observation count may be
    ``count``, ``len``, or ``Count``.
    """
    raw = json.loads(Path(path).read_text())
    metrics = raw.get("metrics") if isinstance(raw, dict) and "metrics" in raw else raw
    out: dict[str, dict] = {}
    for name, stats in (metrics or {}).items():
        if not isinstance(stats, dict):
            continue
        values = stats.get("values") if isinstance(stats.get("values"), dict) else stats
        p95 = next((values[k] for k in ("p95", "p(95)", "P95") if values.get(k) is not None), None)
        count = next((values[k] for k in ("count", "len", "Count") if values.get(k) is not None), None)
        out[name] = {"p95": p95, "count": count}
    return out


# --- schema validation --------------------------------------------------------
#
# The schema (templates/formal-models/system-contracts-schema.json) is ENFORCED,
# not just documentation: a small stdlib structural validator walks the schema's
# $ref/type/required/properties/additionalProperties/enum/pattern/min/max/items
# keywords against the document. Violations are "schema"-category findings —
# gateable in static mode like any other structure finding. (No jsonschema
# dependency: gate scripts are stdlib-only.)


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
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append((path, f"expected number, got {type(value).__name__}"))
            return
        minimum, maximum = schema_node.get("minimum"), schema_node.get("maximum")
        if minimum is not None and value < minimum:
            errors.append((path, f"value {value} below minimum {minimum}"))
        if maximum is not None and value > maximum:
            errors.append((path, f"value {value} above maximum {maximum}"))


def validate_doc(doc: dict, schema: dict) -> list[Finding]:
    """Structurally validate a budget-tree document against the JSON schema.

    Returns ``schema``-category findings for every violation (missing required
    fields, enum violations like a bad ``arithmetic`` or ``evidence`` value,
    malformed ``BUD-``/``ASM-`` IDs, out-of-range ``alpha``, unknown fields).
    """
    errors: list[tuple[str, str]] = []
    _validate_value(doc, schema, "$", schema, errors)
    return [Finding("schema", path, f"{path}: {msg}") for path, msg in errors]


# --- static mode --------------------------------------------------------------


def check_static(doc: dict, schema: dict | None = None) -> BudgetTreeReport:
    report = BudgetTreeReport(mode="static")
    report.findings.extend(validate_doc(doc, schema if schema is not None else load_schema()))
    for idx, tree in enumerate(doc.get("trees") or []):
        root = tree.get("root") or {}
        arithmetic = tree.get("arithmetic", "union-bound")
        report.trees.append(root.get("id", f"tree[{idx}]"))
        _check_node(root, arithmetic, report)
    _check_chains(doc.get("chains") or [], report)
    return report


def _check_node(node: dict, arithmetic: str, report: BudgetTreeReport) -> None:
    node_id = node.get("id", "<node>")

    for asm in node.get("asm_refs") or []:
        _check_asm(asm, node_id, report)

    children = node.get("children") or []
    residual = node.get("residual")

    if children:
        if not residual:
            report.findings.append(
                Finding(
                    "structure",
                    node_id,
                    f"{node_id}: has children but no residual budget declared "
                    "(every non-leaf node must account for unallocated budget)",
                )
            )
        effective = list(children) + ([residual] if residual else [])

        # Kind/unit compatibility precedes ANY arithmetic: a ms parent must not
        # sum usd/tokens children. Mismatched nodes are structure findings and
        # the (meaningless) mixed sums are skipped.
        compatible = True
        for c in effective:
            if c.get("kind") != node.get("kind") or c.get("unit") != node.get("unit"):
                compatible = False
                report.findings.append(
                    Finding(
                        "structure",
                        node_id,
                        f"{node_id}: child {c.get('id', '<node>')} has kind/unit "
                        f"{c.get('kind')}/{c.get('unit')} incompatible with parent's "
                        f"{node.get('kind')}/{node.get('unit')} — budget arithmetic "
                        "requires a homogeneous kind and unit per tree level",
                    )
                )

        if arithmetic == "naive":
            if compatible:
                total_bound = sum(c.get("bound", 0) or 0 for c in effective)
                parent_bound = node.get("bound")
                verdict = (
                    "would PASS" if parent_bound is None or total_bound <= parent_bound + _ALPHA_TOL else "would FAIL"
                )
                report.warnings.append(
                    Note(
                        "naive-arithmetic",
                        node_id,
                        f"{node_id}: naive sum-of-bounds check {verdict} "
                        f"(sum(children)={total_bound} vs parent bound={parent_bound}) — "
                        "advisory only; percentiles do not sum, this mode is unsound for "
                        "correlated tails and can never gate a workflow",
                    )
                )
        else:  # union-bound (default, sound, gateable)
            # Missing alphas must NEVER silently degrade the sound check into a
            # naive sum-of-bounds: a non-leaf union-bound node without alpha, or
            # any child/residual without alpha, is a structure finding.
            parent_alpha = node.get("alpha")
            missing_alpha = [c.get("id", "<node>") for c in effective if c.get("alpha") is None]
            if parent_alpha is None:
                report.findings.append(
                    Finding(
                        "structure",
                        node_id,
                        f"{node_id}: non-leaf union-bound node has no alpha — the "
                        "tail-probability half of the union-bound check cannot run; "
                        "declare alpha or the tree silently degrades to a naive sum",
                    )
                )
            if missing_alpha:
                report.findings.append(
                    Finding(
                        "structure",
                        node_id,
                        f"{node_id}: children missing alpha: {', '.join(missing_alpha)} — "
                        "every child and residual in a union-bound tree must declare its "
                        "tail-probability allocation",
                    )
                )
            if compatible and parent_alpha is not None and not missing_alpha:
                sum_alpha = sum(c["alpha"] for c in effective)
                if sum_alpha > parent_alpha + _ALPHA_TOL:
                    report.findings.append(
                        Finding(
                            "arithmetic",
                            node_id,
                            f"{node_id}: children's tail-probability budget sums to {sum_alpha} "
                            f"which exceeds parent alpha {parent_alpha} (union bound violated)",
                        )
                    )
            if compatible:
                sum_bound = sum(c.get("bound", 0) or 0 for c in effective)
                headroom = node.get("headroom") or 0
                parent_bound = node.get("bound")
                if parent_bound is not None and sum_bound + headroom > parent_bound + _ALPHA_TOL:
                    report.findings.append(
                        Finding(
                            "arithmetic",
                            node_id,
                            f"{node_id}: sum(children.bound)={sum_bound} + headroom={headroom} "
                            f"exceeds parent bound {parent_bound}",
                        )
                    )

        for child in children:
            _check_node(child, arithmetic, report)
        if residual:
            _check_node(residual, arithmetic, report)


def _check_asm(asm: dict, node_id: str, report: BudgetTreeReport) -> None:
    asm_id = asm.get("id", "<unnamed-asm>")
    evidence = asm.get("evidence")
    ref = asm.get("ref")
    if evidence not in EVIDENCE_CHOICES or not ref:
        report.findings.append(
            Finding(
                "structure",
                asm_id,
                f"{asm_id} (on {node_id}): missing/invalid evidence or ref — "
                f"asm_refs require evidence in {EVIDENCE_CHOICES} and a non-empty ref",
            )
        )
        report.asm_statuses.append(AsmStatus(asm_id, node_id, "missing", evidence, ref))
    elif evidence == "justified":
        report.asm_statuses.append(AsmStatus(asm_id, node_id, "waived", evidence, ref))
    else:
        report.asm_statuses.append(AsmStatus(asm_id, node_id, "covered", evidence, ref))


def _check_chains(chains: list[dict], report: BudgetTreeReport) -> None:
    noted = False
    for chain in chains:
        chain_id = chain.get("id", "<chain>")
        hops = chain.get("hops") or []
        for i in range(len(hops) - 1):
            outer, inner = hops[i], hops[i + 1]
            outer_t, inner_t = outer.get("timeout_ms"), inner.get("timeout_ms")
            if outer_t is None or inner_t is None:
                continue
            if not (outer_t > inner_t):
                report.warnings.append(
                    Note(
                        "monotonicity",
                        chain_id,
                        f"{chain_id}: hop {outer.get('caller')}->{outer.get('callee')} "
                        f"timeout={outer_t}ms is not greater than nested hop "
                        f"{inner.get('caller')}->{inner.get('callee')} timeout={inner_t}ms "
                        f"({MONOTONICITY_NOTE})",
                    )
                )
                noted = True
    if chains and not noted:
        report.warnings.append(Note("monotonicity", None, f"all chains monotonic. ({MONOTONICITY_NOTE})"))


# --- measured mode --------------------------------------------------------------


def load_emits_report(path: str | Path) -> set[str]:
    """Load a ``check_instrumentation.py`` JSON report and return its
    ``emitted_bindings`` set (#170) — every binding name with at least one
    ``@cw-emits`` site found, repo-wide."""
    raw = json.loads(Path(path).read_text())
    return set(raw.get("emitted_bindings") or [])


def check_measured(
    doc: dict,
    measured: dict[str, dict],
    source: str,
    schema: dict | None = None,
    emitted: set[str] | None = None,
) -> BudgetTreeReport:
    """``emitted``, if given, is the ``emitted_bindings`` set from a
    ``check_instrumentation.py`` report (#170) — optional and additive. When
    omitted, statuses are computed exactly as before this integration existed.
    """
    report = BudgetTreeReport(mode="measured", source=source)
    report.findings.extend(validate_doc(doc, schema if schema is not None else load_schema()))
    for idx, tree in enumerate(doc.get("trees") or []):
        root = tree.get("root") or {}
        report.trees.append(root.get("id", f"tree[{idx}]"))
        _measure_node(root, measured, report, emitted)
    return report


def _measure_node(
    node: dict,
    measured: dict[str, dict],
    report: BudgetTreeReport,
    emitted: set[str] | None = None,
) -> None:
    node_id = node.get("id", "<node>")
    telemetry_ref = node.get("telemetry_ref")
    bound = node.get("bound")

    stats = measured.get(telemetry_ref) if telemetry_ref else None
    observed = stats.get("p95") if stats else None
    count = stats.get("count") if stats else None

    # unbound (no binding declared) vs no_observations (binding declared but no
    # data) is a deliberate distinction: the first is a spec gap, the second a
    # measurement gap. Neither is EVER a pass.
    if not telemetry_ref:
        status = "unbound"
    elif stats is None or observed is None or count == 0:
        status = "no_observations"
    elif bound is not None and observed > bound:
        status = "breached"
    else:
        status = "held"

    # Optional refinement (#170): when an emits-report was supplied, a
    # no_observations binding with NO @cw-emits site anywhere in source is
    # reclassified as not_emitted — a structural gap (nothing ever emits it),
    # not a quiet measurement window (a real emitter that just didn't fire
    # this run). unbound/held/breached are untouched: unbound already means
    # "no binding at all" (stronger than not_emitted), and held/breached have
    # actual observations regardless of what the static scan found.
    emitter_bound: bool | None = None
    if emitted is not None and telemetry_ref:
        emitter_bound = telemetry_ref in emitted
        if status == "no_observations" and not emitter_bound:
            status = "not_emitted"

    report.measured.append(MeasuredResult(node_id, telemetry_ref, bound, observed, status, emitter_bound))

    for child in node.get("children") or []:
        _measure_node(child, measured, report, emitted)
    if node.get("residual"):
        _measure_node(node["residual"], measured, report, emitted)


# --- rendering ------------------------------------------------------------------


def render_text(report: BudgetTreeReport) -> str:
    lines = [f"# Budget Tree Report ({report.mode})", "", f"Authority: {report.authority}", ""]
    lines.append(f"Trees checked: {', '.join(report.trees) if report.trees else '(none)'}")

    if report.mode == "static":
        lines.append(f"Status: {'OK' if report.ok else 'FINDINGS'}")
        if report.findings:
            lines += ["", "## Findings (gateable under --gate)"]
            lines += [f"- [{f.category}] {f.message}" for f in report.findings]
        if report.warnings:
            lines += ["", "## Warnings (advisory, never gateable)"]
            lines += [f"- [{w.category}] {w.message}" for w in report.warnings]
        if report.asm_statuses:
            lines += ["", "## Assumption references"]
            for a in report.asm_statuses:
                lines.append(f"- {a.id} (on {a.node}): {a.status} [{a.evidence or '?'}] {a.ref or ''}")
    else:
        lines.append(f"Source: {report.source}")
        if report.findings:
            lines += ["", "## Findings (informational — measured mode never gates)"]
            lines += [f"- [{f.category}] {f.message}" for f in report.findings]
        if report.measured:
            lines += ["", "## Measured bindings"]
            for m in report.measured:
                obs = m.observed if m.observed is not None else "—"
                emit_note = "" if m.emitter_bound is None else f" (emitter {'found' if m.emitter_bound else 'MISSING'})"
                lines.append(
                    f"- {m.id} ref={m.telemetry_ref or '(none)'} bound={m.bound} observed={obs} "
                    f"-> {m.status}{emit_note}"
                )

    return "\n".join(lines) + "\n"


# --- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Budget tree checker: typed NFR budget trees with union-bound tail arithmetic (#164)"
    )
    parser.add_argument("budget_file", help="Path to a system-contracts.json budget tree document")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument(
        "--measured",
        help="k6 summary JSON or flat {metric: {p95: ...}} export; switches to measured mode "
        "(evidence-only, never gates)",
    )
    parser.add_argument(
        "--emits-report",
        help="Optional check_instrumentation.py JSON report (#170); when given with --measured, "
        "refines no_observations to not_emitted for bindings with no @cw-emits site anywhere "
        "in source. Omit for today's three-way status unchanged.",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Static mode only: exit 1 on structure/arithmetic findings. No-op (measured mode never gates).",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    try:
        schema = load_schema(Path(args.schema))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error: cannot load schema: {exc}", file=sys.stderr)
        return 2

    try:
        doc = load_doc(args.budget_file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error: cannot load budget tree file: {exc}", file=sys.stderr)
        return 2

    if args.measured:
        try:
            measured_data = load_measured(args.measured)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Error: cannot load measured data: {exc}", file=sys.stderr)
            return 2
        emitted = None
        if args.emits_report:
            try:
                emitted = load_emits_report(args.emits_report)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"Error: cannot load emits report: {exc}", file=sys.stderr)
                return 2
        report = check_measured(doc, measured_data, source=args.measured, schema=schema, emitted=emitted)
    else:
        report = check_static(doc, schema=schema)

    print(json.dumps(report.to_dict(), indent=2) if args.format == "json" else render_text(report))

    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        import os

        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from factory_log import emit_gate

        caught = len(report.findings) if report.mode == "static" else 0
        emit_gate("check_budget_tree", "fail" if (report.mode == "static" and not report.ok) else "pass", caught=caught)
    except Exception:
        pass

    if report.mode == "static" and args.gate and not report.ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
