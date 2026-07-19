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
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The ID grammar and verb set are shared with ratchet.py and the TIM schema —
# a kind added in one place but not the others is silently dropped, so all
# three build from chief_wiggum.trace_ids (cross-checked in tests).
# Shared with check_single_writer.py: the hash-derived --scanner-version and
# the git-native manifest helper behind --changed-since (#160). walk_source_files
# prunes submodules/nested git checkouts from the FULL scan so both scan modes
# agree on the file universe (the manifest never surfaces submodule blobs).
from chief_wiggum.hashing import hash_epic_definitions, scanner_version  # noqa: E402
from chief_wiggum.manifest import ManifestError, changed_paths, walk_source_files  # noqa: E402
from chief_wiggum.trace_ids import DEFINE_RE, ID_RE, TRACE_RE, canonical_id  # noqa: E402

# Suspect-link propagation (#169): a link is SUSPECT when the ID it was
# verified against has a definition hash that no longer matches the hash
# recorded in docs/quality/trace-links.json at the time the link last passed
# a gate. JUSTIFIED waivers (docs/epics/<slug>/justifications/*.json) let an
# uncovered/untested contract satisfy coverage with a committed, ticket-backed
# reason instead of a false "guards"/"verifies" annotation. See
# chief_wiggum.trace_links and docs/traceability.md.
from chief_wiggum.trace_links import (  # noqa: E402
    SIDECAR_RELPATH,
    build_sidecar,
    find_suspect_links,
    is_justification_path,
    load_justifications,
    load_sidecar,
    write_sidecar,
)

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
    # Suspect propagation (#169): links recorded in docs/quality/trace-links.json
    # whose target's definition hash has since changed. Report-only — does NOT
    # affect soundness_ok/coverage_ok (see docs/gate-rollout.md). Distinct from
    # dangling (target gone) and uncovered/untested (no link at all): here a
    # link DOES exist, its claim is just stale.
    suspect_links: list[dict] = field(default_factory=list)
    suspect_contracts: list[str] = field(default_factory=list)
    # JUSTIFIED waivers (#169): an uncovered/untested contract with a valid,
    # non-expired, ticket-backed justification record is moved out of
    # uncovered_contracts/untested_contracts and reported here instead — a
    # third status, neither a clean pass nor a silent gap.
    justified_contracts: list[dict] = field(default_factory=list)
    expired_justifications: list[dict] = field(default_factory=list)
    invalid_justifications: list[dict] = field(default_factory=list)
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
            "suspect_links": len(self.suspect_links),
            "justified_contracts": len(self.justified_contracts),
            "expired_justifications": len(self.expired_justifications),
            "invalid_justifications": len(self.invalid_justifications),
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
            "suspect_links": self.suspect_links,
            "suspect_contracts": self.suspect_contracts,
            "justified_contracts": self.justified_contracts,
            "expired_justifications": self.expired_justifications,
            "invalid_justifications": self.invalid_justifications,
            "warnings": self.warnings,
        }


def load_schema(path: Path = DEFAULT_SCHEMA) -> dict:
    return json.loads(Path(path).read_text())


def kind_of(node_id: str) -> str:
    return node_id.split("-", 1)[0].upper()


# canonical_id's home is chief_wiggum.trace_ids (shared with the hashing
# module, so definition-hash keys and annotation targets join on the same
# canonical form — PR #181 review); imported above, re-exported here for
# existing callers.


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
    """Collect IDs *declared* in the epic's prose + model artifacts.

    The ``justifications/`` subtree (waiver records, #169) is excluded: a
    waiver's own ``"id"`` field names the CTR/INV it waives and must never be
    misread as a new stable-ID declaration.
    """
    root = Path(epic_dir)
    defined: dict[str, str] = {}
    if not root.exists():
        return defined
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".md", ".json") or not path.is_file():
            continue
        if is_justification_path(root, path):
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        for m in DEFINE_RE.finditer(text):
            node_id = canonical_id(m.group(1))
            defined[node_id] = kind_of(node_id)
    return defined


def _collect_coverage_requirements(node, out: dict[str, list[str]]) -> None:
    if isinstance(node, dict):
        cid = node.get("id")
        reqs = node.get("coverage_requires")
        if isinstance(cid, str) and ID_RE.fullmatch(cid) and isinstance(reqs, list) and reqs:
            out[canonical_id(cid)] = [str(r) for r in reqs]
        for v in node.values():
            _collect_coverage_requirements(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_coverage_requirements(v, out)


def extract_coverage_requirements(epic_dir: str | Path) -> dict[str, list[str]]:
    """Per-contract coverage-requirement alternatives (LOBSTER pattern, #169).

    A JSON model entry may declare ``"coverage_requires": ["unit-test", "probe"]``
    alongside its ``"id"``: the contract is tested only by a ``verifies``
    annotation whose ``source_kind`` is ONE of the listed alternatives (an "A
    OR B" requirement), instead of the default "any verifying kind counts".
    Absent for a given ID, behavior is unchanged. Degrades gracefully on a
    missing epic dir or unparsable JSON (skipped, not raised).
    """
    root = Path(epic_dir)
    out: dict[str, list[str]] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*.json")):
        if is_justification_path(root, path):
            continue
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        _collect_coverage_requirements(doc, out)
    return out


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
        if is_justification_path(root, path):
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
        # walk_source_files prunes submodules/nested git checkouts, keeping the
        # full scan's file universe identical to the manifest's (--changed-since).
        candidates = walk_source_files(root)
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
    *,
    coverage_requirements: dict[str, list[str]] | None = None,
) -> TraceReport:
    """Build the trace report. ``coverage_requirements`` (#169, optional) maps a
    contract ID to a list of ``source_kind`` alternatives (e.g.
    ``["test", "probe"]``) — when present, a contract is tested only by a
    ``verifies`` link whose kind is one of those alternatives ("A OR B");
    absent, any verifying kind counts (unchanged prior behavior)."""
    report = TraceReport(defined=dict(defined))
    link_types = schema.get("link_types", {})
    coverage_requirements = coverage_requirements or {}

    realized: set[str] = set()      # BR ids with an incoming realizes
    guarded: set[str] = set()       # CTR/INV with guards/ensures (code)
    verified_kinds: dict[str, set[str]] = {}  # CTR/INV -> set of verifying source_kinds

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
            verified_kinds.setdefault(ann.target, set()).add(ann.source_kind)

    contracts = [i for i, k in defined.items() if k in ("CTR", "INV")]
    business_rules = [i for i, k in defined.items() if k == "BR"]

    def _tested(cid: str) -> bool:
        kinds = verified_kinds.get(cid, set())
        required = coverage_requirements.get(cid)
        if required:
            return bool(kinds & set(required))
        return bool(kinds)

    report.orphan_business_rules = sorted(b for b in business_rules if b not in realized)
    report.uncovered_contracts = sorted(c for c in contracts if c not in guarded)
    report.untested_contracts = sorted(c for c in contracts if not _tested(c))

    if not annotations:
        report.warnings.append("no @cw-trace annotations found; reporting coverage as absent")
    if not defined:
        report.warnings.append("no contract/invariant/BR IDs defined in epic artifacts")
    return report


def apply_justifications(
    report: TraceReport,
    justifications: dict,
    invalid: list[dict],
    *,
    today: date | None = None,
) -> None:
    """Apply JUSTIFIED waivers (#169) to an already-built ``report``, in place.

    A valid (ticket-backed, non-expired) justification for a currently
    uncovered/untested contract moves it OUT of those lists and into
    ``justified_contracts`` — satisfying coverage without a fake guard/verify
    annotation. An expired justification is reported but does NOT satisfy
    coverage. A justification referencing an ID that isn't even defined, or
    that had no gap to waive in the first place, is not silently accepted.
    """
    today = today or date.today()
    report.invalid_justifications = list(invalid)
    justified: list[dict] = []
    expired: list[dict] = []
    for cid in sorted(justifications):
        j = justifications[cid]
        if cid not in report.defined:
            report.invalid_justifications.append(
                {"source": j.source, "reason": f"references undefined id {cid}"}
            )
            continue
        if j.is_expired(today):
            expired.append(j.to_dict())
            continue
        moved = False
        if cid in report.uncovered_contracts:
            report.uncovered_contracts.remove(cid)
            moved = True
        if cid in report.untested_contracts:
            report.untested_contracts.remove(cid)
            moved = True
        if moved:
            justified.append(j.to_dict())
    report.justified_contracts = justified
    report.expired_justifications = expired


def check(
    epic_dir: str | Path,
    source_root: str | Path | None = None,
    *,
    schema: dict | None = None,
    changed_since: str | None = None,
    links_path: str | Path | None = None,
    today: date | None = None,
) -> TraceReport:
    """Build the trace report. ``links_path`` (#169, optional), when given, is
    the ``docs/quality/trace-links.json`` sidecar to compare current contract
    definition hashes against for suspect-link detection — omitted, no sidecar
    is read and ``suspect_links`` stays empty (nothing to compare against yet,
    e.g. the very first validation). ``today`` (optional) overrides the clock
    used for justification-expiry checks; defaults to the real today."""
    schema = schema or load_schema()
    defined = extract_defined_ids(epic_dir)
    coverage_requirements = extract_coverage_requirements(epic_dir)
    # Contract->BR realizes links live in the epic docs; code/test links in source.
    annotations = scan_epic_annotations(epic_dir)
    if source_root:
        only_files = None
        if changed_since:
            # Ticket-scoped speed-up ONLY — never used by /close-epic's coverage
            # gate, which must see the whole repo to be authoritative.
            only_files = changed_paths(source_root, changed_since, predicate=_file_predicate)
        annotations += scan_source(source_root, only_files=only_files)
    report = build_report(defined, annotations, schema, coverage_requirements=coverage_requirements)

    if links_path is not None:
        current_hashes = hash_epic_definitions(Path(epic_dir))
        sidecar = load_sidecar(links_path)
        report.suspect_links = find_suspect_links(sidecar, current_hashes)
        report.suspect_contracts = sorted({link["target"] for link in report.suspect_links})

    justifications, invalid_justifications = load_justifications(epic_dir)
    apply_justifications(report, justifications, invalid_justifications, today=today)
    return report


def write_links_sidecar(
    epic_dir: str | Path,
    source_root: str | Path | None,
    path: str | Path,
) -> dict:
    """Write the ``docs/quality/trace-links.json`` sidecar (#169) from the
    CURRENT scan: every ``@cw-trace`` link's definition hash, at the moment
    this is called. Not hand-maintained — called by ``/architect``/
    ``/close-epic`` only once their respective gate has passed (see ``main``'s
    ``--write-links``), so a stale/failing state never gets recorded as
    validated.

    Always a FULL source scan, by construction: the sidecar is the global
    record of validated links, and rewriting it from a ``--changed-since``
    partial scan would silently drop every validated link in unchanged files
    (they'd then never be able to go suspect). ``main`` rejects the
    ``--write-links --changed-since`` combination as a usage error (PR #181
    review)."""
    annotations = scan_epic_annotations(epic_dir)
    if source_root:
        annotations += scan_source(source_root)
    current_hashes = hash_epic_definitions(Path(epic_dir))
    body = build_sidecar(annotations, current_hashes, scanner_version=_scanner_version())
    write_sidecar(path, body)
    return body


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
    if report.suspect_links:
        lines += ["", "## Suspect links (definition changed since verified)", ""]
        lines += [
            f"- {d['file']}:{d['line']} {d['verb']} {d['target']} "
            f"(definition hash changed since this link was validated)"
            for d in report.suspect_links
        ]
    if report.justified_contracts:
        lines += ["", "## Justified (waived, ticket-tracked)", ""]
        lines += [
            f"- {j['id']} — {j['reason']} (ticket {j['ticket']}, approver {j['approver']}, "
            f"expires {j['expiry']})"
            for j in report.justified_contracts
        ]
    if report.expired_justifications:
        lines += ["", "## Expired justifications (no longer satisfy coverage)", ""]
        lines += [
            f"- {j['id']} — expired {j['expiry']} (ticket {j['ticket']})"
            for j in report.expired_justifications
        ]
    if report.invalid_justifications:
        lines += ["", "## Invalid justifications", ""]
        lines += [f"- {d['source']}: {d['reason']}" for d in report.invalid_justifications]
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
    parser.add_argument(
        "--links",
        metavar="PATH",
        help="Path to the trace-links.json sidecar (#169) used for suspect-link detection. "
        f"Defaults to <--source or cwd>/{SIDECAR_RELPATH}.",
    )
    parser.add_argument(
        "--write-links",
        action="store_true",
        help="(Re)write the trace-links.json sidecar from a FULL scan's current link/definition "
        "hashes — but ONLY when the requested --gate passes (or no --gate was given). A failing "
        "gate leaves the sidecar untouched, so a stale/broken state is never recorded as "
        "validated. Incompatible with --changed-since (a partial scan would drop validated "
        "links for unchanged files). Not hand-maintained; see docs/traceability.md.",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    if args.scanner_version:
        print(_scanner_version())
        return 0

    if not args.epic_dir:
        print("Error: epic_dir is required unless --scanner-version is given", file=sys.stderr)
        return 2

    if args.write_links and args.changed_since:
        print(
            "Error: --write-links cannot be combined with --changed-since — the sidecar is the "
            "global record of validated links and must be written from a FULL scan; a partial "
            "scan would silently drop validated links for unchanged files",
            file=sys.stderr,
        )
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

    links_path = Path(args.links) if args.links else Path(args.source or ".") / SIDECAR_RELPATH

    try:
        report = check(
            args.epic_dir, args.source, schema=schema, changed_since=args.changed_since,
            links_path=links_path,
        )
    except ManifestError as exc:
        # Bad --changed-since ref, non-git --source, missing HEAD, no git binary:
        # a usage error, reported concisely — never a traceback.
        print(f"Error: --changed-since manifest failed: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_markdown(report))

    if args.write_links:
        gate_passed = (
            args.gate is None
            or (args.gate == "soundness" and report.soundness_ok)
            or (args.gate == "coverage" and report.coverage_ok)
        )
        if gate_passed:
            write_links_sidecar(args.epic_dir, args.source, links_path)
        else:
            print(
                f"check_traceability: --write-links skipped — --gate {args.gate} did not pass "
                "(sidecar left untouched)",
                file=sys.stderr,
            )

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
