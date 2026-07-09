#!/usr/bin/env python3
"""Single-writer / mutator-inventory checker.

Some invariants declare a **single write path** / **single source of truth** on a
specific field or state: exactly one sanctioned code path may mutate it. Prose and
the existing traceability/ratchet checks cannot catch a *second* writer — they
verify contract↔code↔test *links* and the pass-set, not "who writes this field".

Real incident this catches: an epic declared ``INV-BIL-001`` ("single atomic
Stripe→plan write") and the reconcile feature honoured it — but a pre-existing
admin control (``ChangePlan``) was a SECOND writer of the same
``provider.stripe_plan`` field, and nothing flagged it.

This checker:

1. Parses an epic's artifacts (structured ``state-machines.json`` invariants AND
   prose ``invariants.md``) for invariants carrying single-write-path metadata:
   the controlled field(s) and the sanctioned writer(s).
2. Scans the target repo for ALL writers of each controlled field — Go/general
   assignments (``x.Plan =``), struct-literal sets (``Plan: v``), and Mongo bson
   mutations (``$set``/``{Key: "plan"``, ``"plan":`` in an update) and SQL
   ``UPDATE ... SET plan``.
3. Flags any writer NOT in the sanctioned set as a violation.

Convention (mirrors ``@cw-trace``; see ``docs/single-writer.md``):

- **Structured** — a ``state-machines.json`` ``invariant`` object gains two
  optional arrays::

      { "id": "INV-bil-001",
        "description": "single atomic Stripe→plan write",
        "controls_field": ["provider.plan", "provider.stripe_plan"],
        "sanctioned_writers": ["ReconcileStripe", "internal/billing/reconcile.go"] }

- **Prose** — an ``invariants.md`` invariant gains a namespaced tag on/near its
  ``**INV-...**`` line::

      **INV-bil-001**: single atomic Stripe→plan write
      <!-- @cw-writes INV-bil-001 controls_field=provider.plan,provider.stripe_plan
           sanctioned_writers=ReconcileStripe,internal/billing/reconcile.go -->

A ``sanctioned_writer`` is either a **symbol** (a function/method name, matched
against the nearest enclosing ``func`` above a write) or a **file path** (matched
as a suffix of the writer's file). A field path ``provider.stripe_plan`` matches
writes to its leaf token (``stripe_plan`` / ``StripePlan``) — see ``field_tokens``.

Backward-compatible: invariants without the metadata are skipped (degrade
gracefully), exactly like ``check_traceability.py`` when IDs are absent.

Gates (mirrors ``check_traceability.py``):
    --gate soundness  -> /architect: fail on malformed metadata; surface writers.
    --gate coverage   -> /close-epic: hard-fail on any unsanctioned writer.

Exit codes: 0 = ok, 1 = gate violation, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Same INV- shape as check_traceability.py (case-insensitive slug segment).
INV_ID_RE = re.compile(r"\bINV-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3}(?![A-Za-z0-9-])", re.IGNORECASE)

# Prose metadata tag, mirroring the @cw-trace LOBSTER-style namespaced tag.
# `@cw-writes <INV-ID> controls_field=a,b sanctioned_writers=x,y`  (order-free).
WRITES_TAG_RE = re.compile(
    r"@cw-writes\s+(?P<id>INV-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3})(?P<attrs>(?:\s+\w+=[^\s]+)+)",
    re.IGNORECASE,
)
ATTR_RE = re.compile(r"(\w+)=([^\s]+)")

# Prose invariant declaration (bold label), same as check_traceability's DEFINE_RE
# but scoped to INV- and capturing the description for reporting.
INV_DEFINE_RE = re.compile(r"\*\*\s*(INV-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3})\s*\*\*\s*:?\s*(.*)")

SOURCE_EXTS = {".go", ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".rb", ".rs"}
SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", "vendor", "dist", "build"}

# A file is test infrastructure (not a sanctioned/unsanctioned production writer)
# — same heuristic as check_traceability.py. Test writes of a controlled field
# are fixtures, not a competing production write path, so they don't violate.
def _is_test_path(rel: str) -> bool:
    low = rel.lower()
    return "test" in low or "spec" in low or any(p == "e2e" for p in Path(low).parts)


def canonical_id(node_id: str) -> str:
    kind, _, rest = node_id.partition("-")
    return f"{kind.upper()}-{rest.lower()}"


@dataclass
class SingleWriterInvariant:
    """An invariant that declares a single write path on one or more fields."""

    id: str
    description: str
    controls_field: list[str]
    sanctioned_writers: list[str]
    source: str  # where the metadata was declared (file:line or file)

    def to_dict(self) -> dict:
        return asdict(self)

    def field_tokens(self) -> set[str]:
        """Leaf identifiers that a write to a controlled field would use.

        ``provider.stripe_plan`` -> {``stripe_plan``, ``stripeplan``, ``StripePlan``}.
        We compare case-insensitively on the token, plus a camelCase form, so Go
        (``StripePlan``), snake bson (``stripe_plan``), and JSON tags all match.
        """
        tokens: set[str] = set()
        for fpath in self.controls_field:
            leaf = fpath.split(".")[-1].strip()
            if not leaf:
                continue
            tokens.add(leaf.lower())
            # snake_case -> CamelCase (stripe_plan -> stripeplan for compaction)
            tokens.add(leaf.replace("_", "").lower())
        return tokens


@dataclass
class Writer:
    invariant_id: str
    field: str
    file: str
    line: int
    text: str
    symbol: str | None  # nearest enclosing function/method, if resolvable
    sanctioned: bool
    is_test: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SingleWriterReport:
    invariants: list[dict] = field(default_factory=list)
    writers: list[dict] = field(default_factory=list)      # all production writers found
    violations: list[dict] = field(default_factory=list)   # unsanctioned writers
    malformed: list[dict] = field(default_factory=list)     # bad metadata (soundness)
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict:
        return {
            "invariants": len(self.invariants),
            "writers": len(self.writers),
            "violations": len(self.violations),
            "malformed": len(self.malformed),
        }

    @property
    def soundness_ok(self) -> bool:
        # Design-time: metadata must be well-formed. Existing writers are surfaced,
        # not failed on (the fix may be part of the epic being architected).
        return not self.malformed

    @property
    def coverage_ok(self) -> bool:
        # Close-time: no unsanctioned writer may exist.
        return not self.violations and not self.malformed

    def to_dict(self) -> dict:
        return {
            "counts": self.counts,
            "soundness_ok": self.soundness_ok,
            "coverage_ok": self.coverage_ok,
            "invariants": self.invariants,
            "writers": self.writers,
            "violations": self.violations,
            "malformed": self.malformed,
            "warnings": self.warnings,
        }


# --- parsing invariants -----------------------------------------------------


def _parse_attrs(attr_str: str) -> tuple[list[str], list[str]]:
    controls: list[str] = []
    writers: list[str] = []
    for key, val in ATTR_RE.findall(attr_str):
        items = [v for v in val.split(",") if v]
        if key.lower() == "controls_field":
            controls.extend(items)
        elif key.lower() == "sanctioned_writers":
            writers.extend(items)
    return controls, writers


def parse_prose_invariants(text: str, rel: str) -> tuple[list[SingleWriterInvariant], list[dict]]:
    """Extract single-write-path invariants from a prose ``invariants.md``.

    Returns (invariants, malformed). A ``@cw-writes`` tag with a controls_field but
    no sanctioned_writers (or vice-versa) is malformed — the metadata is incomplete.
    Descriptions are pulled from the nearest ``**INV-...**`` bold label if present.
    """
    invariants: list[SingleWriterInvariant] = []
    malformed: list[dict] = []
    lines = text.splitlines()
    # Map canonical INV id -> description from bold labels.
    descriptions: dict[str, str] = {}
    for line in lines:
        m = INV_DEFINE_RE.search(line)
        if m:
            descriptions[canonical_id(m.group(1))] = m.group(2).strip()
    for i, line in enumerate(lines, start=1):
        for tag in WRITES_TAG_RE.finditer(line):
            inv_id = canonical_id(tag.group("id"))
            controls, writers = _parse_attrs(tag.group("attrs"))
            loc = f"{rel}:{i}"
            if not controls or not writers:
                malformed.append({
                    "id": inv_id,
                    "source": loc,
                    "reason": "@cw-writes tag must set both controls_field and sanctioned_writers",
                })
                continue
            invariants.append(SingleWriterInvariant(
                id=inv_id,
                description=descriptions.get(inv_id, ""),
                controls_field=controls,
                sanctioned_writers=writers,
                source=loc,
            ))
    return invariants, malformed


def parse_structured_invariants(data: dict, rel: str) -> tuple[list[SingleWriterInvariant], list[dict]]:
    """Extract single-write-path invariants from a state-machines.json model."""
    invariants: list[SingleWriterInvariant] = []
    malformed: list[dict] = []
    for inv in data.get("invariants", []) or []:
        if not isinstance(inv, dict):
            continue
        controls = inv.get("controls_field")
        writers = inv.get("sanctioned_writers")
        if controls is None and writers is None:
            continue  # not a single-write-path invariant — skip (backward compatible)
        inv_id = canonical_id(str(inv.get("id", "INV-unknown-000")))
        if not controls or not writers:
            malformed.append({
                "id": inv_id,
                "source": rel,
                "reason": "invariant sets one of controls_field/sanctioned_writers but not both",
            })
            continue
        if not isinstance(controls, list) or not isinstance(writers, list):
            malformed.append({
                "id": inv_id,
                "source": rel,
                "reason": "controls_field and sanctioned_writers must be arrays of strings",
            })
            continue
        invariants.append(SingleWriterInvariant(
            id=inv_id,
            description=str(inv.get("description", "")),
            controls_field=[str(c) for c in controls],
            sanctioned_writers=[str(w) for w in writers],
            source=rel,
        ))
    return invariants, malformed


def collect_invariants(epic_dir: str | Path) -> tuple[list[SingleWriterInvariant], list[dict]]:
    root = Path(epic_dir)
    invariants: list[SingleWriterInvariant] = []
    malformed: list[dict] = []
    if not root.exists():
        return invariants, malformed
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        try:
            if path.suffix == ".json":
                data = json.loads(path.read_text())
                if isinstance(data, dict):
                    invs, bad = parse_structured_invariants(data, rel)
                    invariants += invs
                    malformed += bad
            elif path.suffix == ".md":
                invs, bad = parse_prose_invariants(path.read_text(), rel)
                invariants += invs
                malformed += bad
        except (OSError, json.JSONDecodeError):
            continue
    return invariants, malformed


# --- scanning the repo for writers ------------------------------------------


def _writer_patterns(token: str) -> list[re.Pattern]:
    """Build write-detection regexes for a controlled field's leaf ``token``.

    ``token`` is already lowercased+de-underscored (e.g. ``stripeplan``). We match
    the identifier case-insensitively and tolerant of a single underscore between
    word chars, so ``StripePlan``, ``stripe_plan``, and ``StripePlan`` all hit.
    """
    # Rebuild a flexible identifier: optional underscores between characters.
    ident = re.escape(token)
    # Also accept the snake form: insert optional underscores is overkill; instead
    # match either the compacted token or the original snake token. We pass both in.
    pats: list[re.Pattern] = []
    # 1. Assignment: `something.Plan =` / `.stripe_plan =` (not ==, not :=... actually
    #    := is a Go declaration+assignment which IS a write, so allow it).
    pats.append(re.compile(rf"\.{ident}\s*:?=[^=]", re.IGNORECASE))
    # 2. Struct-literal / map set: `Plan: value` or `"plan": value` or `Key: "plan"`.
    pats.append(re.compile(rf"""(^|[\s,{{(])['"]?{ident}['"]?\s*:\s*""", re.IGNORECASE))
    # 3. bson/Mongo update key referencing the field literally in a set expression.
    pats.append(re.compile(rf"""['"]{ident}['"]""", re.IGNORECASE))
    # 4. SQL UPDATE ... SET plan =
    pats.append(re.compile(rf"\bSET\b[^;]*\b{ident}\s*=", re.IGNORECASE))
    return pats


# A bson $set / Mongo update / SQL UPDATE context marker — a bare `"plan":` in a
# non-mutating context (e.g. a JSON response DTO field) shouldn't count. We only
# treat pattern #3 (quoted-literal) as a write when the surrounding lines look
# like a mutation. Assignment (#1) and struct-literal (#2) are writes on their own.
MUTATION_CONTEXT_RE = re.compile(
    r"\$set|UpdateOne|UpdateMany|UpdateByID|FindOneAndUpdate|bson\.[ME]|SET\b|UPDATE\b",
    re.IGNORECASE,
)

GO_FUNC_RE = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)")
PY_FUNC_RE = re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)")
TS_FUNC_RE = re.compile(r"(?:function\s+([A-Za-z_$][\w$]*)|([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?\()")


def _enclosing_symbol(lines: list[str], idx: int) -> str | None:
    """Nearest function/method name declared at or above line index ``idx``."""
    for j in range(idx, -1, -1):
        line = lines[j]
        for pat in (GO_FUNC_RE, PY_FUNC_RE):
            m = pat.match(line)
            if m:
                return m.group(1)
        m = TS_FUNC_RE.search(line)
        if m:
            return m.group(1) or m.group(2)
    return None


def _distinct_field_forms(inv: SingleWriterInvariant) -> list[tuple[str, str]]:
    """(original controlled-field path, leaf-token) pairs, both snake and compact."""
    forms: list[tuple[str, str]] = []
    seen: set[str] = set()
    for fpath in inv.controls_field:
        leaf = fpath.split(".")[-1].strip()
        for tok in (leaf.lower(), leaf.replace("_", "").lower()):
            if tok and tok not in seen:
                seen.add(tok)
                forms.append((fpath, tok))
    return forms


def scan_writers(
    source_root: str | Path,
    invariants: list[SingleWriterInvariant],
) -> list[Writer]:
    """Find every writer of every controlled field across the repo."""
    root = Path(source_root)
    writers: list[Writer] = []
    if not root.exists() or not invariants:
        return writers

    # Precompute per-invariant field patterns.
    inv_patterns: list[tuple[SingleWriterInvariant, list[tuple[str, str, list[re.Pattern]]]]] = []
    for inv in invariants:
        field_pats: list[tuple[str, str, list[re.Pattern]]] = []
        for fpath, tok in _distinct_field_forms(inv):
            field_pats.append((fpath, tok, _writer_patterns(tok)))
        inv_patterns.append((inv, field_pats))

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SOURCE_EXTS:
            continue
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        rel = str(path.relative_to(root))
        is_test = _is_test_path(rel)
        for i, line in enumerate(lines):
            for inv, field_pats in inv_patterns:
                for fpath, _tok, pats in field_pats:
                    hit = False
                    for pi, pat in enumerate(pats):
                        if not pat.search(line):
                            continue
                        # Pattern #3 (bare quoted literal, index 2) only counts as a
                        # write inside a mutation context; otherwise skip (DTO field).
                        if pi == 2 and not (
                            MUTATION_CONTEXT_RE.search(line)
                            or (i > 0 and MUTATION_CONTEXT_RE.search(lines[i - 1]))
                            or (i > 1 and MUTATION_CONTEXT_RE.search(lines[i - 2]))
                        ):
                            continue
                        hit = True
                        break
                    if not hit:
                        continue
                    symbol = _enclosing_symbol(lines, i)
                    sanctioned = is_test or _is_sanctioned(inv, rel, symbol)
                    writers.append(Writer(
                        invariant_id=inv.id,
                        field=fpath,
                        file=rel,
                        line=i + 1,
                        text=line.strip()[:200],
                        symbol=symbol,
                        sanctioned=sanctioned,
                        is_test=is_test,
                    ))
                    break  # one write record per (line, invariant)
    return writers


def _is_sanctioned(inv: SingleWriterInvariant, rel: str, symbol: str | None) -> bool:
    """A writer is sanctioned if its enclosing symbol OR its file matches an entry
    in ``sanctioned_writers``. File entries match as a path suffix (so a repo-root
    relative ``internal/billing/reconcile.go`` matches regardless of scan cwd)."""
    rel_norm = rel.replace("\\", "/")
    for entry in inv.sanctioned_writers:
        e = entry.strip()
        if not e:
            continue
        if "/" in e or e.endswith((".go", ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".rb", ".rs")):
            # Treat as a file path (or glob-ish suffix).
            if rel_norm == e or rel_norm.endswith("/" + e) or rel_norm.endswith(e):
                return True
        else:
            # Treat as a symbol name (function/method), case-insensitive.
            if symbol and symbol.lower() == e.lower():
                return True
    return False


# --- top-level check --------------------------------------------------------


def check(epic_dir: str | Path, source_root: str | Path | None = None) -> SingleWriterReport:
    report = SingleWriterReport()
    invariants, malformed = collect_invariants(epic_dir)
    report.invariants = [inv.to_dict() for inv in invariants]
    report.malformed = malformed

    if not invariants:
        report.warnings.append(
            "no single-write-path invariants found (no controls_field/sanctioned_writers "
            "metadata); nothing to check"
        )
        return report

    if source_root:
        writers = scan_writers(source_root, invariants)
        report.writers = [w.to_dict() for w in writers]
        report.violations = [w.to_dict() for w in writers if not w.sanctioned]
        # Surface any invariant whose controlled field has NO writer at all — the
        # sanctioned path may be missing/misnamed (a soft warning, not a violation).
        written_ids = {w.invariant_id for w in writers}
        for inv in invariants:
            if inv.id not in written_ids:
                report.warnings.append(
                    f"{inv.id}: no writer found for {inv.controls_field} — "
                    f"sanctioned writer(s) {inv.sanctioned_writers} may be missing or misnamed"
                )
    else:
        report.warnings.append("no --source given; parsed invariant metadata only (no repo scan)")

    return report


# --- rendering / CLI --------------------------------------------------------


def render_text(report: SingleWriterReport) -> str:
    c = report.counts
    lines = [
        "# Single-Writer Audit",
        "",
        f"Single-write-path invariants: {c['invariants']}",
        f"Writers found: {c['writers']}  |  Violations: {c['violations']}  |  Malformed metadata: {c['malformed']}",
        "",
        f"- Soundness (metadata well-formed): {'OK' if report.soundness_ok else 'FINDINGS'}",
        f"- Coverage (no unsanctioned writer): {'OK' if report.coverage_ok else 'FINDINGS'}",
    ]
    if report.malformed:
        lines += ["", "## Malformed metadata", ""]
        lines += [f"- {m['id']} ({m['source']}): {m['reason']}" for m in report.malformed]
    if report.violations:
        lines += ["", "## Unsanctioned writers (single-write-path violations)", ""]
        for v in report.violations:
            sym = f" in {v['symbol']}()" if v.get("symbol") else ""
            lines.append(
                f"- {v['invariant_id']} field `{v['field']}` written at "
                f"{v['file']}:{v['line']}{sym}"
            )
            lines.append(f"    {v['text']}")
    if report.writers and not report.violations:
        lines += ["", "## Sanctioned writers", ""]
        for w in report.writers:
            sym = f" in {w['symbol']}()" if w.get("symbol") else ""
            tag = " [test]" if w.get("is_test") else ""
            lines.append(f"- {w['invariant_id']} `{w['field']}` at {w['file']}:{w['line']}{sym}{tag}")
    if report.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in report.warnings]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Single-writer / mutator-inventory checker for single-write-path invariants"
    )
    parser.add_argument("epic_dir", help="docs/epics/<slug> directory (or CW_TMP at architect time)")
    parser.add_argument("--source", help="Repo root to scan for writers of controlled fields")
    parser.add_argument(
        "--gate",
        choices=["soundness", "coverage"],
        help="Fail (exit 1) on this gate's findings (soundness=malformed metadata; "
        "coverage=unsanctioned writers)",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    if not Path(args.epic_dir).exists():
        print(f"Error: epic dir not found: {args.epic_dir}", file=sys.stderr)
        return 2

    report = check(args.epic_dir, args.source)

    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_text(report))

    if args.gate == "soundness" and not report.soundness_ok:
        return 1
    if args.gate == "coverage" and not report.coverage_ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
