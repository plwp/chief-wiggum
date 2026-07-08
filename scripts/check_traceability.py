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
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_SCHEMA = Path(__file__).resolve().parents[1] / "templates" / "formal-models" / "tim-schema.json"

# An ID ends at the 3-digit suffix and must not run into more id chars
# (so CTR-order-001oops is NOT a valid CTR-order-001).
# The epic slug segment is case-insensitive (CTR-order-001 and CTR-ADM-001 are both
# valid); matching is normalised to lowercase at ingestion so links can't miss on case.
ID_RE = re.compile(r"\b(BR|CTR|INV)-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3}(?![A-Za-z0-9-])")
TRACE_RE = re.compile(
    r"@cw-trace\s+(?P<verb>realizes|guards|ensures|verifies)\s+"
    r"(?P<ids>(?:(?:BR|CTR|INV)-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3}(?![A-Za-z0-9-])[\s,]*)+)",
    re.IGNORECASE,
)
# Where a defined ID is *declared*: a markdown heading `### CTR-...`, a bold
# label `**CTR-...**`, or a JSON `"id": "CTR-..."` field.
DEFINE_RE = re.compile(
    r"(?:^#{1,6}\s+|\*\*\s*|[\"']id[\"']\s*:\s*[\"'])((?:BR|CTR|INV)-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3})",
    re.MULTILINE,
)

# Code/test annotations live in code/test files — not markdown. Prose docs
# (including this checker's own examples and the epic's realizes lines) are
# handled only by scan_epic_annotations, so they aren't double-counted.
SOURCE_EXTS = {".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".java", ".rb", ".rs"}


@dataclass
class Annotation:
    verb: str
    target: str
    file: str
    line: int
    source_kind: str  # "code" | "test" | "CTR" | "INV"
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


def scan_epic_annotations(epic_dir: str | Path) -> list[Annotation]:
    """Scan the epic docs for ``@cw-trace realizes`` (and other) annotations.

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
            lines = path.read_text().splitlines()
        except OSError:
            continue
        rel = str(path.relative_to(root))
        # A realizes annotation is attributed to the nearest contract/invariant
        # *declared above it* in the same file, so it is tied to a real source.
        nearest_contract: str | None = None
        for i, line in enumerate(lines, start=1):
            for dm in DEFINE_RE.finditer(line):
                if kind_of(dm.group(1)) in ("CTR", "INV"):
                    nearest_contract = canonical_id(dm.group(1))
            for verb, ids in parse_annotations(line):
                src_kind = kind_of(nearest_contract) if nearest_contract else "CTR"
                for target in ids:
                    annotations.append(
                        Annotation(verb, target, rel, i, src_kind, source_id=nearest_contract)
                    )
    return annotations


def scan_source(source_root: str | Path) -> list[Annotation]:
    """Scan source/test files for ``@cw-trace`` annotations."""
    root = Path(source_root)
    annotations: list[Annotation] = []
    if not root.exists():
        return annotations
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SOURCE_EXTS:
            continue
        if any(part in {".git", "node_modules", "__pycache__", ".venv"} for part in path.parts):
            continue
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        rel = str(path.relative_to(root))
        is_test = "test" in rel.lower() or "spec" in rel.lower()
        for i, line in enumerate(lines, start=1):
            for verb, ids in parse_annotations(line):
                for target in ids:
                    annotations.append(
                        Annotation(verb, target, rel, i, "test" if is_test else "code")
                    )
    return annotations


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
) -> TraceReport:
    schema = schema or load_schema()
    defined = extract_defined_ids(epic_dir)
    # Contract->BR realizes links live in the epic docs; code/test links in source.
    annotations = scan_epic_annotations(epic_dir)
    if source_root:
        annotations += scan_source(source_root)
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
    parser.add_argument("epic_dir", help="docs/epics/<slug> directory with contract/invariant IDs")
    parser.add_argument("--source", help="Repo root to scan for @cw-trace annotations")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--gate", choices=["soundness", "coverage"], help="Fail (exit 1) on this gate's findings")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    try:
        schema = load_schema(Path(args.schema))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error: cannot load TIM schema: {exc}", file=sys.stderr)
        return 2

    # A missing epic dir is a usage error (a typo), not graceful absence.
    if not Path(args.epic_dir).exists():
        print(f"Error: epic dir not found: {args.epic_dir}", file=sys.stderr)
        return 2

    report = check(args.epic_dir, args.source, schema=schema)

    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_markdown(report))

    if args.gate == "soundness" and not report.soundness_ok:
        return 1
    if args.gate == "coverage" and not report.coverage_ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
