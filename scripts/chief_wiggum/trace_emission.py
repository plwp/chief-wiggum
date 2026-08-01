"""``@cw-trace`` annotation emission (#162): the per-file parsing that finds
every ``@cw-trace <verb> <ID>...`` annotation in one file's text.

This is the exact logic that used to live inline in ``check_traceability.py``
(the emission half of the emission/claim split from #160) — moved here so it
can sit BEHIND the ``scripts/emitters/`` per-language interface, alongside
``chief_wiggum.write_emission`` (the single-writer emission family). It shares
the ID grammar with ``chief_wiggum.trace_ids`` rather than duplicating it.
``check_traceability.py`` re-exports every name below unchanged, so existing
imports (``check_traceability.emit_source_annotations``,
``check_traceability.Annotation``, ``check_traceability.canonical_id``, ...)
keep working — this is a pure move, not a behavior change (golden parity; see
``tests/test_traceability_golden.py`` and ``docs/languages.md``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

# canonical_id's home is chief_wiggum.trace_ids (#181: shared with the
# definition-hashing module, so definition-hash keys and annotation targets
# join on the same canonical form) — imported and re-exported here so
# emission-side consumers get it from the single home.
from chief_wiggum.trace_ids import ID_RE, TRACE_RE, canonical_id  # noqa: F401

# Directory names whose files are verification probes (k6/chaos experiments)
# rather than product code — see classify_source_kind.
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


def kind_of(node_id: str) -> str:
    return node_id.split("-", 1)[0].upper()


def parse_annotations(text: str) -> list[tuple[str, list[str]]]:
    """Parse ``@cw-trace`` tags from text into (verb, [ids]) pairs."""
    out: list[tuple[str, list[str]]] = []
    for m in TRACE_RE.finditer(text):
        verb = m.group("verb").lower()
        ids = [canonical_id(i.group(0)) for i in ID_RE.finditer(m.group("ids"))]
        if ids:
            out.append((verb, ids))
    return out


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


def emit_source_annotations(rel: str, text: str, suffix: str) -> list[Annotation]:
    """Per-file EMISSION: every ``@cw-trace`` annotation in one source/test/
    verification file's ``text``, classified by this file's source kind. Pure
    function of file content — no knowledge of the defined-ID set (that join
    happens in ``check_traceability.build_report``, at query/report time).
    This is the function every trace-annotation emitter (language-specific or
    generic) under ``scripts/emitters/`` delegates to."""
    source_kind = classify_source_kind(rel, suffix)
    annotations: list[Annotation] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for verb, ids in parse_annotations(line):
            for target in ids:
                annotations.append(Annotation(verb, target, rel, i, source_kind))
    return annotations
