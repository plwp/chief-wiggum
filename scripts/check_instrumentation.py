#!/usr/bin/env python3
"""Instrumentation-coverage checker (#170): telemetry bindings must be provable.

Completeness-lens top finding on the system layer: a budget tree (or, once
they ship, an SLO/trace-conformance contract) can bind ``telemetry_ref`` to a
span/metric name that NOTHING in the codebase ever emits. Every gate built on
top of that binding — ``check_budget_tree.py --measured``, future trace
conformance — then reports "zero violations" not because the system is
healthy, but because it never looked at anything. A budget bound to a metric
nobody emits, or a conformance check over an empty history, PASSES VACUOUSLY.

This checker closes that gap **statically**: for every telemetry binding
referenced in a ``system-contracts.json`` budget doc (or any future doc that
reuses the ``telemetry_ref`` field — this walk is schema-agnostic, so BUD-/
SLO-/trace-conformance nodes are all covered without a checker update), it
verifies that an ``@cw-emits <binding-name>`` annotation exists somewhere in
the source tree marking the code site that emits it. A binding with no
``@cw-emits`` site is reported as **missing**.

Annotation grammar (``chief_wiggum.annotations.EMITS_TAG_RE`` — same regex
tier and grammar family as ``@cw-writes``, see ``docs/single-writer.md``):

    @cw-emits <binding-name>[, <binding-name> ...]

Multiple bindings on one tag must be COMMA-separated; after the first
binding, space-separated tokens are treated as prose and ignored (so a
trailing comment cannot mint phantom bindings).

Place it in a comment at the code site that emits the span/event/metric:

    # @cw-emits endpointing_latency_ms
    def on_endpoint_detected(ts):
        span.set_attribute("endpointing_latency_ms", ts - start)

**What this does NOT prove.** Static presence of an ``@cw-emits`` site proves
the binding is *wired to code*, not that the line executes, not that it
executes on the path the budget doc assumes, and not that the metric is ever
actually emitted at runtime — that's what ``check_budget_tree.py --measured``
(an observed-at-least-once check against a real export) is for. This checker
and that one are complementary, not redundant: this is "does an emitter
exist"; that is "did it fire". Deleting the ``@cw-emits`` line while the
runtime code path is untouched is exactly the regression this checker is
built to catch (the "instrumentation deleted" seed class from the issue) —
see the fixture in ``tests/test_check_instrumentation.py``.

**Optional, minimal integration with check_budget_tree.py.** This checker's
JSON report exposes ``emitted_bindings`` (every binding name with at least one
``@cw-emits`` site, repo-wide). Pass that report to
``check_budget_tree.py --measured ... --emits-report <this-report.json>`` and
measured mode's ``no_observations`` status is refined to ``not_emitted``
when NO code site emits the binding at all (a structural gap, not just a
quiet measurement window) — see docs/budget-trees.md. The flag is optional;
omitting it leaves measured mode's three-way status exactly as it was.

Report-only per doctrine (``docs/gate-rollout.md``): prints findings and
exits 0 by default; ``--gate`` exits 1 on any missing binding. Mirrors
``check_budget_tree.py``'s and ``check_single_writer.py``'s shape (dataclasses,
report object with counts/ok, argparse, best-effort factory_log emit).
Stdlib only.

Exit codes: 0 = ok/report-only, 1 = ``--gate`` violation, 2 = usage error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.annotations import EMITS_TAG_RE, split_binding_names  # noqa: E402

AUTHORITY = (
    "static mode proves an @cw-emits site exists in source for each telemetry "
    "binding declared in the given budget doc(s); it does NOT prove the site "
    "executes, or that the metric is ever observed at runtime — see "
    "check_budget_tree.py --measured for observed-at-least-once evidence"
)

SOURCE_EXTS = {".go", ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".rb", ".rs"}
SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", "vendor", "dist", "build"}


def _excluded(rel: str, patterns: list[str]) -> bool:
    """Same convention as ``check_single_writer._excluded``: a bare token matches
    that directory and everything under it; a glob matches via fnmatch."""
    for g in patterns:
        g = g.rstrip("/")
        if not g:
            continue
        if rel == g or rel.startswith(g + "/"):
            return True
        if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g + "/*"):
            return True
    return False


# --- report data model -------------------------------------------------------


@dataclass
class Binding:
    """A telemetry binding declared by ``telemetry_ref`` on one or more nodes."""

    name: str
    nodes: list[str]
    source: str  # budget doc file it was declared in

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EmitSite:
    """A source-code site carrying an ``@cw-emits <name>`` annotation."""

    name: str
    file: str
    line: int
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Finding:
    category: str  # "missing"
    id: str  # binding name
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InstrumentationReport:
    bindings: list[Binding] = field(default_factory=list)
    emit_sites: list[EmitSite] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict:
        return {
            "bindings": len(self.bindings),
            "emit_sites": len(self.emit_sites),
            "findings": len(self.findings),
        }

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def authority(self) -> str:
        return AUTHORITY

    @property
    def emitted_bindings(self) -> list[str]:
        """Every binding name with at least one ``@cw-emits`` site, repo-wide —
        the artifact ``check_budget_tree.py --emits-report`` consumes."""
        return sorted({s.name for s in self.emit_sites})

    def to_dict(self) -> dict:
        return {
            "authority": self.authority,
            "counts": self.counts,
            "ok": self.ok,
            "bindings": [b.to_dict() for b in self.bindings],
            "emit_sites": [e.to_dict() for e in self.emit_sites],
            "emitted_bindings": self.emitted_bindings,
            "findings": [f.to_dict() for f in self.findings],
            "warnings": self.warnings,
        }


# --- collecting declared bindings --------------------------------------------


def collect_bindings(doc: dict, source: str) -> list[Binding]:
    """Recursively walk a budget-tree (or any ``telemetry_ref``-bearing) doc,
    collecting every ``telemetry_ref`` along with the id(s) of the node(s) that
    declare it. Schema-agnostic on purpose: this walks ANY dict/list shape
    looking for a ``telemetry_ref`` string field, so it covers ``BUD-`` budget
    trees today and ``SLO-``/trace-conformance docs the moment they reuse the
    same field name, with no checker update required.
    """
    found: dict[str, list[str]] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            ref = node.get("telemetry_ref")
            if isinstance(ref, str) and ref:
                found.setdefault(ref, []).append(str(node.get("id", "<node>")))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return [Binding(name=name, nodes=ids, source=source) for name, ids in sorted(found.items())]


def collect_bindings_from_files(paths: list[Path]) -> tuple[list[Binding], list[str]]:
    """Load and merge bindings from one or more budget-doc JSON files. A
    binding name declared in more than one file/node accumulates all
    declaring node ids rather than being reported twice."""
    merged: dict[str, Binding] = {}
    warnings: list[str] = []
    for path in paths:
        try:
            doc = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"cannot load {path}: {exc}")
            continue
        for b in collect_bindings(doc, str(path)):
            if b.name in merged:
                merged[b.name].nodes.extend(n for n in b.nodes if n not in merged[b.name].nodes)
            else:
                merged[b.name] = b
    return [merged[name] for name in sorted(merged)], warnings


# --- scanning the repo for @cw-emits sites -----------------------------------


def scan_emit_sites(source_root: str | Path, exclude: list[str] | None = None) -> list[EmitSite]:
    """Find every ``@cw-emits`` annotation across the source tree."""
    root = Path(source_root)
    exclude = exclude or []
    sites: list[EmitSite] = []
    if not root.exists():
        return sites

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SOURCE_EXTS:
            continue
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        rel = str(path.relative_to(root))
        if _excluded(rel, exclude):
            continue
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            for m in EMITS_TAG_RE.finditer(line):
                for name in split_binding_names(m.group("names")):
                    sites.append(EmitSite(name=name, file=rel, line=i + 1, text=line.strip()[:200]))
    return sites


# --- top-level check ----------------------------------------------------------


def check(
    budget_files: list[str | Path],
    source_root: str | Path | None = None,
    exclude: list[str] | None = None,
) -> InstrumentationReport:
    report = InstrumentationReport()
    bindings, warnings = collect_bindings_from_files([Path(p) for p in budget_files])
    report.bindings = bindings
    report.warnings.extend(warnings)

    if not bindings:
        report.warnings.append(
            "no telemetry bindings found (no telemetry_ref field in the given budget doc(s)); "
            "nothing to check"
        )
        return report

    if not source_root:
        # Mirrors check_single_writer.py: without a repo scan we cannot claim a
        # binding is "missing" — we simply haven't looked. Degrade gracefully
        # (report bindings parsed, no findings) rather than false-positive.
        report.warnings.append("no --source given; parsed telemetry bindings only (no repo scan)")
        return report

    sites = scan_emit_sites(source_root, exclude=exclude)
    report.emit_sites = sites
    emitted_names = {s.name for s in sites}

    for b in bindings:
        if b.name not in emitted_names:
            report.findings.append(
                Finding(
                    "missing",
                    b.name,
                    f"{b.name}: declared as telemetry_ref on {', '.join(b.nodes)} but no "
                    f"@cw-emits site found in {source_root}",
                )
            )

    return report


# --- rendering ----------------------------------------------------------------


def render_text(report: InstrumentationReport) -> str:
    c = report.counts
    lines = [
        "# Instrumentation Coverage Report",
        "",
        f"Authority: {report.authority}",
        "",
        f"Bindings declared: {c['bindings']}  |  @cw-emits sites found: {c['emit_sites']}  |  "
        f"Missing: {c['findings']}",
        f"Status: {'OK' if report.ok else 'FINDINGS'}",
    ]
    if report.findings:
        lines += ["", "## Missing bindings (gateable under --gate)", ""]
        lines += [f"- {f.message}" for f in report.findings]
    if report.bindings and not report.findings:
        lines += ["", "## Bound telemetry bindings", ""]
        for b in report.bindings:
            lines.append(f"- {b.name} (declared on {', '.join(b.nodes)})")
    if report.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in report.warnings]
    return "\n".join(lines) + "\n"


# --- CLI ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Instrumentation-coverage checker: telemetry bindings must be provable (#170)"
    )
    parser.add_argument(
        "budget_files",
        nargs="+",
        help="One or more system-contracts.json budget-tree documents to read telemetry_ref bindings from",
    )
    parser.add_argument("--source", help="Repo root to scan for @cw-emits sites")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="Repo-relative path/dir/glob to skip; repeatable",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Exit 1 on any missing binding (no @cw-emits site found)",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    # A corrupt/unreadable budget doc must be a USAGE ERROR (exit 2), never a
    # silent degrade to "nothing to check" — that would let a broken contract
    # file disable the gate while appearing green (Codex review of PR #180).
    for bf in args.budget_files:
        path = Path(bf)
        if not path.exists():
            print(f"Error: budget file not found: {bf}", file=sys.stderr)
            return 2
        try:
            json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Error: cannot load budget file {bf}: {exc}", file=sys.stderr)
            return 2

    report = check(args.budget_files, args.source, exclude=args.exclude)

    print(json.dumps(report.to_dict(), indent=2) if args.format == "json" else render_text(report))

    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        import os

        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from factory_log import emit_gate

        emit_gate("check_instrumentation", "fail" if not report.ok else "pass", caught=len(report.findings))
    except Exception:
        pass

    if args.gate and not report.ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
