#!/usr/bin/env python3
"""Traceability graph checker (#36): business rule -> contract -> code -> test.

Builds a machine-readable traceability graph from stable IDs in the epic docs
plus ``@cw-trace`` annotations in source/tests, and reports:

- **orphan business rules** — a ``BR-*`` with no ``realizes`` link.
- **uncovered contracts** — a ``CTR-*``/``INV-*`` with no ``guards``/``ensures``
  code annotation.
- **untested contracts** — a ``CTR-*``/``INV-*`` with no ``verifies`` test annotation.
- **dangling annotations** — an annotation referencing an ID that isn't defined.
- **invalid links** — a verb whose source/target node types violate the TIM schema.

Annotation grammar (uniform across languages, LOBSTER-style namespaced tag):

    @cw-trace <verb> <ID> [<ID> ...]      verbs: realizes|guards|ensures|verifies

The checker is a *separate pass* (not compile-time enforcement) and degrades
gracefully: a repo/epic with no annotations reports absence rather than crashing.

Mirrors ``check_unresolved.py``. Gates:
    --gate soundness  -> fail on orphan BRs + dangling refs + invalid links (/architect)
    --gate coverage   -> fail on uncovered + untested contracts (/close-epic)

Internally, scanning is split into per-file EMISSION (``emit_epic_annotations``,
``emit_source_annotations``: every ``@cw-trace`` annotation in one file) and
report-time joins against the defined-ID set (``build_report``) — see
``docs/traceability.md``. ``--changed-since <ref>`` scopes the ``--source`` scan
to files changed since ``ref`` (never used by /close-epic's coverage gate, which
must see the whole repo). ``--scanner-version`` prints a hash of this module's
source plus its ``chief_wiggum`` deps.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The ID grammar and verb set are shared with ratchet.py and the TIM schema —
# a kind added in one place but not the others is silently dropped, so all
# three build from chief_wiggum.trace_ids (cross-checked in tests).
# Shared with check_single_writer.py: the hash-derived --scanner-version and
# the git-native manifest helper behind --changed-since (#160).
from chief_wiggum.hashing import scanner_version  # noqa: E402
from chief_wiggum.manifest import changed_paths  # noqa: E402
from chief_wiggum.trace_ids import DEFINE_RE, ID_RE, TRACE_RE  # noqa: E402

DEFAULT_SCHEMA = Path(__file__).resolve().parents[1] / "templates" / "formal-models" / "tim-schema.json"

# Code/test annotations live in code/test files — not markdown. Prose docs
# (including this checker's own examples and the epic's realizes lines) are
# handled only by scan_epic_annotations, so they aren't double-counted.
# .rego/.yaml/.yml are verification artifacts (policy/probe/telemetry — #166).
SOURCE_EXTS = {".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".java", ".rb", ".rs", ".rego", ".yaml", ".yml"}

# Directory names whose files are verification probes (k6 scenarios, chaos
# experiments) rather than product code — see classify_source_kind.
PROBE_DIR_PARTS = {"k6", "chaos", "probe", "probes", "load", "loadtest"}


@dataclass
class Annotation:
    verb: str
    target: str
    file: str
    line: int
    source_kind: str  # "code" | "test" | "probe" | "policy" | "telemetry" | a declared ID kind (CTR, INV, BUD, ...)
    source_id: str | None = None  # for realizes: the declaring contract/invariant ID

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TraceReport:
    defined: dict[str, str] = field(default_factory=dict)  # id -> kind
    orphan_business_rules: list[str] = field(default_factory=list)
    uncovered_contracts: list[str] = field(default_factory=list)
    untested_contracts: list[str] = field(default_factory=list)
    dangling: list[dict] = field(default_factory=list)
    invalid_links: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict:
        return {
            "defined": len(self.defined),
            "orphan_business_rules": len(self.orphan_business_rules),
            "uncovered_contracts": len(self.uncovered_contracts),
            "untested_contracts": len(self.untested_contracts),
            "dangling": len(self.dangling),
            "invalid_links": len(self.invalid_links),
        }

    @property
    def soundness_ok(self) -> bool:
        return not (self.orphan_business_rules or self.dangling or self.invalid_links)

    @property
    def coverage_ok(self) -> bool:
        return not (self.uncovered_contracts or self.untested_contracts)

    def to_dict(self) -> dict:
        return {
            "defined": self.defined,
            "counts": self.counts,
            "soundness_ok": self.soundness_ok,
            "coverage_ok": self.coverage_ok,
            "orphan_business_rules": self.orphan_business_rules,
            "uncovered_contracts": self.uncovered_contracts,
            "untested_contracts": self.untested_contracts,
            "dangling": self.dangling,
            "invalid_links": self.invalid_links,
            "warnings": self.warnings,
        }


def load_schema(path: Path = DEFAULT_SCHEMA) -> dict:
    return json.loads(Path(path).read_text())


def kind_of(node_id: str) -> str:
    return node_id.split("-", 1)[0].upper()


def canonical_id(node_id: str) -> str:
    """Canonical form: uppercase kind prefix, lowercase remainder.

    IDs are matched case-insensitively (CTR-order-001 == CTR-ORDER-001); this
    keeps the familiar display shape while making links immune to case drift
    between epic docs and code annotations.
    """
    kind, _, rest = node_id.partition("-")
    return f"{kind.upper()}-{rest.lower()}"


def parse_annotations(text: str) -> list[tuple[str, list[str]]]:
    """Parse ``@cw-trace`` tags from text into (verb, [ids]) pairs."""
    out: list[tuple[str, list[str]]] = []
    for m in TRACE_RE.finditer(text):
        verb = m.group("verb").lower()
        ids = [canonical_id(i.group(0)) for i in ID_RE.finditer(m.group("ids"))]
        if ids:
            out.append((verb, ids))
    return out


def extract_defined_ids(epic_dir: str | Path) -> dict[str, str]:
    """Collect IDs *declared* in the epic's prose + model artifacts."""
    root = Path(epic_dir)
    defined: dict[str, str] = {}
    if not root.exists():
        return defined
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".md", ".json") or not path.is_file():
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        for m in DEFINE_RE.finditer(text):
            node_id = canonical_id(m.group(1))
            defined[node_id] = kind_of(node_id)
    return defined


def emit_epic_annotations(rel: str, text: str) -> list[Annotation]:
    """Per-file EMISSION: every ``@cw-trace`` annotation declared in one epic doc's
    ``text``, attributed to the nearest stable ID declared above it. Pure function
    of file content — no knowledge of the full defined-ID set (that join is
    query-time, in ``build_report``).

    A realizes/derive annotation is attributed to the nearest stable ID
    *declared above it* in the same file, so it is tied to a real source.
    Any kind that can be a link SOURCE qualifies (BUD-/EDG-/... declare
    derive links the same way CTR-/INV- declare realizes — #166). BR is
    only ever a link target, so a BR declaration RESETS attribution: an
    annotation under a BR heading must not inherit an earlier contract
    (that would let a stray realizes clear the BR's own orphan status).
    """
    annotations: list[Annotation] = []
    nearest_contract: str | None = None
    for i, line in enumerate(text.splitlines(), start=1):
        for dm in DEFINE_RE.finditer(line):
            if kind_of(dm.group(1)) == "BR":
                nearest_contract = None
            else:
                nearest_contract = canonical_id(dm.group(1))
        for verb, ids in parse_annotations(line):
            src_kind = kind_of(nearest_contract) if nearest_contract else "CTR"
            for target in ids:
                annotations.append(
                    Annotation(verb, target, rel, i, src_kind, source_id=nearest_contract)
                )
    return annotations


def scan_epic_annotations(epic_dir: str | Path) -> list[Annotation]:
    """Walk the epic docs, emitting ``@cw-trace realizes`` (and other)
    annotations per file via ``emit_epic_annotations``.

    Annotations authored in the contract/invariant docs originate from a contract
    (source kind ``CTR``) — this is how a contract declares which business
    rule(s) it realizes.
    """
    root = Path(epic_dir)
    annotations: list[Annotation] = []
    if not root.exists():
        return annotations
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".md", ".json") or not path.is_file():
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        rel = str(path.relative_to(root))
        annotations.extend(emit_epic_annotations(rel, text))
    return annotations


def classify_source_kind(rel: str, suffix: str) -> str:
    """Classify a scanned file into a TIM artifact kind.

    ``probe`` (k6/chaos/load scenarios), ``policy`` (rego), and ``telemetry``
    (SLO/alert/metric YAML) are verification artifacts a ``verifies`` edge may
    originate from (#166); everything else keeps the original test/code
    path heuristic.
    """
    rel_lower = rel.lower()
    parts = set(Path(rel_lower).parts)
    if suffix == ".rego":
        return "policy"
    if parts & PROBE_DIR_PARTS:
        return "probe"
    if suffix in (".yaml", ".yml"):
        return "telemetry"
    # e2e directories are test infrastructure (setup/fixtures/helpers) even when
    # the filename itself carries no test/spec marker (e.g. ui/e2e/global-setup.ts).
    is_test = (
        "test" in rel_lower
        or "spec" in rel_lower
        or "e2e" in parts
    )
    return "test" if is_test else "code"


SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv"}


def _file_predicate(rel: str) -> bool:
    """The scanner's EXACT file-selection rule (extension allow-list + skipped
    directories) — the same predicate ``scan_source`` applies during its own
    walk, reused to build a manifest whose keys are exactly the files that walk
    would visit (see ``chief_wiggum.manifest``)."""
    p = Path(rel)
    if p.suffix not in SOURCE_EXTS:
        return False
    if any(part in SKIP_PARTS for part in p.parts):
        return False
    return True


def emit_source_annotations(rel: str, text: str, suffix: str) -> list[Annotation]:
    """Per-file EMISSION: every ``@cw-trace`` annotation in one source/test/
    verification file's ``text``, classified by this file's source kind. Pure
    function of file content — no knowledge of the defined-ID set (that join
    happens in ``build_report``, at query/report time)."""
    source_kind = classify_source_kind(rel, suffix)
    annotations: list[Annotation] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for verb, ids in parse_annotations(line):
            for target in ids:
                annotations.append(Annotation(verb, target, rel, i, source_kind))
    return annotations


def scan_source(source_root: str | Path, only_files: set[str] | None = None) -> list[Annotation]:
    """Walk source/test/verification files, emitting ``@cw-trace`` annotations
    per file via ``emit_source_annotations``. ``only_files`` (repo-relative
    paths), when given, restricts the walk to that set instead of the whole
    tree — used by ``--changed-since``."""
    root = Path(source_root)
    annotations: list[Annotation] = []
    if not root.exists():
        return annotations
    if only_files is not None:
        candidates = sorted(only_files)
    else:
        candidates = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    for rel in candidates:
        if not _file_predicate(rel):
            continue
        path = root / rel
        try:
            text = path.read_text()
        except OSError:
            continue
        annotations.extend(emit_source_annotations(rel, text, path.suffix))
    return annotations


def _scanner_version() -> str:
    """Hash-derived ``--scanner-version``: the source of this module plus its
    ``chief_wiggum`` dependencies. No hand-bumped constant to forget."""
    here = Path(__file__).resolve()
    cw_dir = here.parent / "chief_wiggum"
    return scanner_version(here, cw_dir / "trace_ids.py", cw_dir / "manifest.py", cw_dir / "hashing.py")


def build_report(
    defined: dict[str, str],
    annotations: list[Annotation],
    schema: dict,
) -> TraceReport:
    report = TraceReport(defined=dict(defined))
    link_types = schema.get("link_types", {})

    realized: set[str] = set()      # BR ids with an incoming realizes
    guarded: set[str] = set()       # CTR/INV with guards/ensures (code)
    verified: set[str] = set()      # CTR/INV with verifies (test)

    for ann in annotations:
        # Dangling: references an ID that isn't defined.
        if ann.target not in defined:
            report.dangling.append(ann.to_dict())
            continue
        rule = link_types.get(ann.verb)
        if rule is None:
            report.invalid_links.append({**ann.to_dict(), "reason": f"unknown verb {ann.verb}"})
            continue
        # Validate source/target node types against the TIM schema.
        if ann.source_kind not in rule["from"]:
            report.invalid_links.append(
                {**ann.to_dict(), "reason": f"{ann.verb} cannot originate from {ann.source_kind}"}
            )
            continue
        if defined[ann.target] not in rule["to"]:
            report.invalid_links.append(
                {**ann.to_dict(), "reason": f"{ann.verb} cannot target {defined[ann.target]}"}
            )
            continue
        if ann.verb == "realizes":
            # Only a realizes from a *defined* contract/invariant counts; a stray
            # realizes with no declaring contract above it doesn't clear the orphan.
            if ann.source_id and ann.source_id in defined:
                realized.add(ann.target)
            else:
                report.invalid_links.append(
                    {**ann.to_dict(), "reason": "realizes has no declaring contract/invariant source"}
                )
        elif ann.verb in ("guards", "ensures"):
            guarded.add(ann.target)
        elif ann.verb == "verifies":
            verified.add(ann.target)

    contracts = [i for i, k in defined.items() if k in ("CTR", "INV")]
    business_rules = [i for i, k in defined.items() if k == "BR"]

    report.orphan_business_rules = sorted(b for b in business_rules if b not in realized)
    report.uncovered_contracts = sorted(c for c in contracts if c not in guarded)
    report.untested_contracts = sorted(c for c in contracts if c not in verified)

    if not annotations:
        report.warnings.append("no @cw-trace annotations found; reporting coverage as absent")
    if not defined:
        report.warnings.append("no contract/invariant/BR IDs defined in epic artifacts")
    return report


def check(
    epic_dir: str | Path,
    source_root: str | Path | None = None,
    *,
    schema: dict | None = None,
    changed_since: str | None = None,
) -> TraceReport:
    schema = schema or load_schema()
    defined = extract_defined_ids(epic_dir)
    # Contract->BR realizes links live in the epic docs; code/test links in source.
    annotations = scan_epic_annotations(epic_dir)
    if source_root:
        only_files = None
        if changed_since:
            # Ticket-scoped speed-up ONLY — never used by /close-epic's coverage
            # gate, which must see the whole repo to be authoritative.
            only_files = changed_paths(source_root, changed_since, predicate=_file_predicate)
        annotations += scan_source(source_root, only_files=only_files)
    return build_report(defined, annotations, schema)


def render_markdown(report: TraceReport) -> str:
    lines = ["# Traceability Audit", "", f"Defined IDs: {report.counts['defined']}", ""]
    lines.append(f"- Soundness (orphans/dangling/invalid): {'OK' if report.soundness_ok else 'FINDINGS'}")
    lines.append(f"- Coverage (uncovered/untested): {'OK' if report.coverage_ok else 'FINDINGS'}")
    for label, items in (
        ("Orphan business rules", report.orphan_business_rules),
        ("Uncovered contracts (no code guard)", report.uncovered_contracts),
        ("Untested contracts (no test)", report.untested_contracts),
    ):
        if items:
            lines += ["", f"## {label}", ""] + [f"- {i}" for i in items]
    if report.dangling:
        lines += ["", "## Dangling annotations", ""]
        lines += [f"- {d['file']}:{d['line']} {d['verb']} {d['target']} (undefined)" for d in report.dangling]
    if report.invalid_links:
        lines += ["", "## Invalid links", ""]
        lines += [f"- {d['file']}:{d['line']} {d['reason']}" for d in report.invalid_links]
    if report.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in report.warnings]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Traceability graph checker (TIM/DbC)")
    parser.add_argument(
        "epic_dir", nargs="?", default=None,
        help="docs/epics/<slug> directory with contract/invariant IDs; not required with --scanner-version",
    )
    parser.add_argument("--source", help="Repo root to scan for @cw-trace annotations")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--gate", choices=["soundness", "coverage"], help="Fail (exit 1) on this gate's findings")
    parser.add_argument(
        "--changed-since",
        metavar="REF",
        help="Scope the --source scan to files changed since REF (via git diff + the "
        "content-addressed manifest) instead of the whole tree. Ticket-scoped speed-up "
        "ONLY — /close-epic's coverage gate NEVER uses this; whole-repo remains the default.",
    )
    parser.add_argument(
        "--scanner-version",
        action="store_true",
        help="Print the hash-derived scanner version (source hash of this module + its "
        "chief_wiggum deps) and exit",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    if args.scanner_version:
        print(_scanner_version())
        return 0

    if not args.epic_dir:
        print("Error: epic_dir is required unless --scanner-version is given", file=sys.stderr)
        return 2

    try:
        schema = load_schema(Path(args.schema))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error: cannot load TIM schema: {exc}", file=sys.stderr)
        return 2

    # A missing epic dir is a usage error (a typo), not graceful absence.
    if not Path(args.epic_dir).exists():
        print(f"Error: epic dir not found: {args.epic_dir}", file=sys.stderr)
        return 2

    report = check(args.epic_dir, args.source, schema=schema, changed_since=args.changed_since)

    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_markdown(report))

    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        import os
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from factory_log import emit_gate
        caught = (len(report.orphan_business_rules) + len(report.uncovered_contracts)
                  + len(report.untested_contracts) + len(report.dangling)
                  + len(report.invalid_links))
        emit_gate("check_traceability", "fail" if caught else "pass",
                  caught=caught, repo=_repo_from_epic_dir(args.epic_dir))
    except Exception:
        pass

    if args.gate == "soundness" and not report.soundness_ok:
        return 1
    if args.gate == "coverage" and not report.coverage_ok:
        return 1
    return 0


def _repo_from_epic_dir(epic_dir: str) -> str:
    """Best-effort repo name from an epic dir (<repo>/docs/epics/<slug>)."""
    import os
    parts = os.path.abspath(epic_dir).split(os.sep)
    if "docs" in parts and parts.index("docs") > 0:
        return parts[parts.index("docs") - 1]
    return os.path.basename(os.path.abspath(epic_dir))


if __name__ == "__main__":
    sys.exit(main())
