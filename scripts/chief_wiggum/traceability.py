"""Traceability matrix parser and updater (P2-13).

`/architect` writes a ``traceability.md`` markdown table mapping each ticket's
acceptance criteria to the tests that cover them. ``/implement`` flips a row to
``covered`` when it writes the test and ``/close-epic`` audits coverage — but
those updates are described as manual markdown edits. This parses, updates, and
audits the table with tested code.

Table columns (from /architect):
    Ticket | Acceptance Criterion | Unit Test | Integration Test | E2E Test | Status
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

STATUSES = ("pending", "covered", "passing", "failing", "missing")

_COLUMN_KEYS = {
    "ticket": "ticket",
    "ac": "ac",
    "acceptance criterion": "ac",
    "acceptance criteria": "ac",
    "unit test": "unit_test",
    "integration test": "integration_test",
    "e2e test": "e2e_test",
    "status": "status",
}
_REQUIRED_COLUMNS = ("ticket", "ac", "status")


@dataclass
class TraceRow:
    ticket: int | None
    ac: str
    unit_test: str = ""
    integration_test: str = ""
    e2e_test: str = ""
    status: str = "pending"

    @property
    def has_test(self) -> bool:
        return any(
            t and t not in ("—", "-", "")
            for t in (self.unit_test, self.integration_test, self.e2e_test)
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TraceMatrix:
    rows: list[TraceRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Inclusive line span of the parsed table in the source (for in-place edits).
    table_start: int | None = None
    table_end: int | None = None

    def to_dict(self) -> dict:
        return {"rows": [r.to_dict() for r in self.rows], "warnings": list(self.warnings)}


def _split_cells(line: str) -> list[str]:
    """Split a markdown table row on unescaped pipes, trimming the outer ones."""
    cells: list[str] = []
    buf = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == "|":
            buf += "|"  # escaped pipe -> literal
            i += 2
            continue
        if ch == "|":
            cells.append(buf)
            buf = ""
        else:
            buf += ch
        i += 1
    cells.append(buf)
    # A leading/trailing pipe produces empty first/last cells — drop them.
    if cells and cells[0].strip() == "":
        cells = cells[1:]
    if cells and cells[-1].strip() == "":
        cells = cells[:-1]
    return [c.strip() for c in cells]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(set(c) <= set("-: ") and "-" in c for c in cells)


def _cell(cells: list[str], col_index: dict[str, int], key: str) -> str:
    idx = col_index.get(key)
    return cells[idx] if idx is not None and idx < len(cells) else ""


def _parse_ticket(value: str) -> int | None:
    v = value.strip().lstrip("#").strip()
    try:
        return int(v)
    except ValueError:
        return None


def _header_candidates(lines: list[str]) -> list[tuple[int, dict[str, int]]]:
    """Find (index, column-map) for each ``| header |`` line followed by a separator."""
    candidates: list[tuple[int, dict[str, int]]] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = _split_cells(line)
        col_index = {
            _COLUMN_KEYS[c.lower()]: j for j, c in enumerate(cells) if c.lower() in _COLUMN_KEYS
        }
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if nxt.startswith("|") and _is_separator(_split_cells(nxt)):
            candidates.append((i, col_index))
    return candidates


def parse_matrix(markdown: str) -> TraceMatrix:
    """Parse the traceability table in ``markdown`` into a :class:`TraceMatrix`.

    Detects the real matrix robustly: scans for a ``| header |`` line followed by
    a separator, preferring the first one that has all required columns (so an
    unrelated earlier table doesn't shadow it). Tracks the table's line span so a
    later update can rewrite *only* the table, preserving surrounding prose.
    """
    matrix = TraceMatrix()
    lines = markdown.splitlines()
    candidates = _header_candidates(lines)
    if not candidates:
        matrix.warnings.append("no traceability table found")
        return matrix

    chosen = next(
        (c for c in candidates if set(_REQUIRED_COLUMNS) <= set(c[1])),
        candidates[0],
    )
    header_idx, col_index = chosen
    for required in _REQUIRED_COLUMNS:
        if required not in col_index:
            matrix.warnings.append(f"missing required column: {required}")

    matrix.table_start = header_idx
    matrix.table_end = header_idx + 1  # separator line
    i = header_idx + 2
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith("|"):
            break
        cells = _split_cells(line)
        if _is_separator(cells):
            matrix.table_end = i
            i += 1
            continue
        status = _cell(cells, col_index, "status").lower() or "pending"
        if status not in STATUSES:
            matrix.warnings.append(
                f"unknown status {status!r} for ticket {_cell(cells, col_index, 'ticket')!r}"
            )
        matrix.rows.append(
            TraceRow(
                ticket=_parse_ticket(_cell(cells, col_index, "ticket")),
                ac=_cell(cells, col_index, "ac"),
                unit_test=_cell(cells, col_index, "unit_test"),
                integration_test=_cell(cells, col_index, "integration_test"),
                e2e_test=_cell(cells, col_index, "e2e_test"),
                status=status,
            )
        )
        matrix.table_end = i
        i += 1
    return matrix


def replace_table(original: str, matrix: TraceMatrix) -> str:
    """Rewrite only the table span in ``original``, preserving surrounding prose."""
    if matrix.table_start is None or matrix.table_end is None:
        return original
    lines = original.splitlines()
    rendered = render_markdown(matrix).rstrip("\n").splitlines()
    new_lines = lines[: matrix.table_start] + rendered + lines[matrix.table_end + 1 :]
    text = "\n".join(new_lines)
    return text + "\n" if original.endswith("\n") else text


def update_status(
    matrix: TraceMatrix,
    *,
    ticket: int,
    status: str,
    ac_contains: str | None = None,
    test_contains: str | None = None,
) -> int:
    """Set ``status`` on matching rows; return the number updated.

    Matches rows by ticket, optionally narrowed by an acceptance-criterion
    substring and/or a test-reference substring.
    """
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status!r} (expected one of {', '.join(STATUSES)})")
    updated = 0
    for row in matrix.rows:
        if row.ticket != ticket:
            continue
        if ac_contains and ac_contains.lower() not in row.ac.lower():
            continue
        if test_contains:
            joined = " ".join((row.unit_test, row.integration_test, row.e2e_test)).lower()
            if test_contains.lower() not in joined:
                continue
        row.status = status
        updated += 1
    return updated


def audit(matrix: TraceMatrix) -> dict:
    """Summarize coverage: counts per status, gaps, and ticket rollup."""
    counts = dict.fromkeys(STATUSES, 0)
    gaps: list[dict] = []
    covered = 0
    for row in matrix.rows:
        counts[row.status] = counts.get(row.status, 0) + 1
        # Genuinely covered: a covered/passing status backed by a real test ref.
        if row.status in ("covered", "passing") and row.has_test:
            covered += 1
        if not row.has_test or row.status in ("missing", "failing"):
            gaps.append({"ticket": row.ticket, "ac": row.ac, "status": row.status})
    total = len(matrix.rows)
    return {
        "total": total,
        "counts": counts,
        "covered": covered,
        "coverage_pct": round(100 * covered / total, 1) if total else 0.0,
        "gaps": gaps,
        "warnings": list(matrix.warnings),
    }


def render_markdown(matrix: TraceMatrix) -> str:
    header = "| Ticket | Acceptance Criterion | Unit Test | Integration Test | E2E Test | Status |"
    sep = "|--------|---------------------|-----------|-----------------|----------|--------|"
    lines = [header, sep]
    for r in matrix.rows:
        ticket = f"#{r.ticket}" if r.ticket is not None else ""
        cells = [ticket, r.ac, r.unit_test or "—", r.integration_test or "—", r.e2e_test or "—", r.status]
        escaped = [c.replace("|", "\\|") for c in cells]
        lines.append("| " + " | ".join(escaped) + " |")
    return "\n".join(lines) + "\n"
