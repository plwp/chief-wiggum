"""Tests for the traceability matrix parser/updater (P2-13)."""

from __future__ import annotations

import json

import pytest
import traceability as cli
from chief_wiggum import traceability as tr

TABLE = """\
Some intro prose.

| Ticket | Acceptance Criterion | Unit Test | Integration Test | E2E Test | Status |
|--------|---------------------|-----------|-----------------|----------|--------|
| #42 | GET /health returns 200 | api_test.go:TestHealth | — | — | pending |
| #42 | Order model has all fields | model_test.go:TestOrderFields | — | — | covered |
| #43 | Create order returns 201 | api_test.go:TestCreateOrder | IT-1 | — | passing |
| #44 | Admin sees orders | — | — | — | missing |

More prose after.
"""


# --- parsing ----------------------------------------------------------------


def test_parse_normal_table():
    m = tr.parse_matrix(TABLE)
    assert len(m.rows) == 4
    assert m.rows[0].ticket == 42
    assert m.rows[0].ac == "GET /health returns 200"
    assert m.rows[2].status == "passing"
    assert m.warnings == []


def test_parse_stops_at_blank_after_table():
    m = tr.parse_matrix(TABLE)
    # "More prose" must not be parsed as a row.
    assert all(r.ac != "More prose after." for r in m.rows)


def test_parse_escaped_pipes():
    table = (
        "| Ticket | Acceptance Criterion | Unit Test | Integration Test | E2E Test | Status |\n"
        "|---|---|---|---|---|---|\n"
        "| #7 | value a \\| value b | t | — | — | pending |\n"
    )
    m = tr.parse_matrix(table)
    assert m.rows[0].ac == "value a | value b"


def test_parse_missing_columns_warns():
    table = "| Ticket | Status |\n|---|---|\n| #1 | pending |\n"
    m = tr.parse_matrix(table)
    assert any("missing required column: ac" in w for w in m.warnings)


def test_parse_no_table_warns():
    m = tr.parse_matrix("no table here\n")
    assert any("no traceability table" in w for w in m.warnings)


def test_parse_accepts_ac_header():
    table = "| Ticket | AC | Unit Test | Status |\n|---|---|---|---|\n| #1 | do x | t | covered |\n"
    m = tr.parse_matrix(table)
    assert m.rows[0].ac == "do x"
    assert "missing required column: ac" not in " ".join(m.warnings)


def test_parse_skips_unrelated_earlier_table():
    text = (
        "| Name | Value |\n|---|---|\n| foo | bar |\n\n"
        "| Ticket | Acceptance Criterion | Status |\n|---|---|---|\n| #5 | real AC | pending |\n"
    )
    m = tr.parse_matrix(text)
    assert len(m.rows) == 1
    assert m.rows[0].ac == "real AC"


def test_parse_unknown_status_warns():
    table = (
        "| Ticket | Acceptance Criterion | Status |\n|---|---|---|\n| #1 | x | bogus |\n"
    )
    m = tr.parse_matrix(table)
    assert any("unknown status" in w for w in m.warnings)


def test_duplicate_acs_kept_as_separate_rows():
    table = (
        "| Ticket | Acceptance Criterion | Status |\n|---|---|---|\n"
        "| #1 | same AC | pending |\n| #1 | same AC | covered |\n"
    )
    m = tr.parse_matrix(table)
    assert len(m.rows) == 2
    assert [r.status for r in m.rows] == ["pending", "covered"]


# --- updates ----------------------------------------------------------------


def test_update_all_rows_for_ticket():
    m = tr.parse_matrix(TABLE)
    n = tr.update_status(m, ticket=42, status="passing")
    assert n == 2
    assert all(r.status == "passing" for r in m.rows if r.ticket == 42)


def test_update_narrowed_by_ac():
    m = tr.parse_matrix(TABLE)
    n = tr.update_status(m, ticket=42, status="failing", ac_contains="health")
    assert n == 1
    assert m.rows[0].status == "failing"
    assert m.rows[1].status == "covered"  # unchanged


def test_update_narrowed_by_test_ref():
    m = tr.parse_matrix(TABLE)
    n = tr.update_status(m, ticket=43, status="passing", test_contains="IT-1")
    assert n == 1


def test_update_invalid_status_raises():
    m = tr.parse_matrix(TABLE)
    with pytest.raises(ValueError):
        tr.update_status(m, ticket=42, status="bogus")


# --- audit + render ---------------------------------------------------------


def test_audit_counts_and_gaps():
    m = tr.parse_matrix(TABLE)
    a = tr.audit(m)
    assert a["total"] == 4
    assert a["covered"] == 2  # one covered + one passing
    # #44 missing + no test -> gap.
    assert any(g["ticket"] == 44 for g in a["gaps"])


def test_render_roundtrips_through_parser():
    m = tr.parse_matrix(TABLE)
    rendered = tr.render_markdown(m)
    m2 = tr.parse_matrix(rendered)
    assert [r.to_dict() for r in m.rows] == [r.to_dict() for r in m2.rows]


def test_render_escapes_pipes():
    m = tr.TraceMatrix(rows=[tr.TraceRow(ticket=1, ac="a | b", status="pending")])
    rendered = tr.render_markdown(m)
    assert "a \\| b" in rendered
    assert tr.parse_matrix(rendered).rows[0].ac == "a | b"


# --- CLI --------------------------------------------------------------------


def test_cli_audit(tmp_path, capsys):
    f = tmp_path / "traceability.md"
    f.write_text(TABLE)
    rc = cli.main(["audit", str(f)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["total"] == 4


def test_cli_update_in_place(tmp_path, capsys):
    f = tmp_path / "traceability.md"
    f.write_text(TABLE)
    rc = cli.main(["update", str(f), "--ticket", "44", "--status", "covered"])
    assert rc == 0
    m = tr.parse_matrix(f.read_text())
    assert [r.status for r in m.rows if r.ticket == 44] == ["covered"]


def test_update_preserves_surrounding_prose(tmp_path):
    f = tmp_path / "traceability.md"
    f.write_text(TABLE)
    cli.main(["update", str(f), "--ticket", "42", "--status", "passing"])
    out = f.read_text()
    assert "Some intro prose." in out
    assert "More prose after." in out


def test_audit_does_not_count_covered_without_test():
    table = (
        "| Ticket | Acceptance Criterion | Unit Test | Status |\n|---|---|---|---|\n"
        "| #1 | x | — | covered |\n"
    )
    m = tr.parse_matrix(table)
    a = tr.audit(m)
    assert a["covered"] == 0  # covered status but no test ref
    assert a["gaps"]  # still flagged as a gap


# --- chief-wiggum#342: zero-match update must never rewrite the file --------

CANONICAL_TABLE_413 = """\
| Ticket | Acceptance Criterion | Unit Test | Integration Test | E2E Test | Status |
|--------|---------------------|-----------|-----------------|----------|--------|
| #413 | Dunning ladder fires | dunning_test.go:TestLadder | — | — | pending |
"""

# Shape observed live in chief-wiggum#342: an epic table that keys tickets
# into a heading, not a `Ticket` column — `AC`/`Status` are recognized, but
# `Contracts / invariants` and `Planned tests` are NOT, so this must never be
# treated as canonical (those two columns would be silently dropped by
# render_markdown's fixed 6-column schema).
NON_CANONICAL_TABLE = """\
## #413 — 5/6 Dunning ladder

| AC | Contracts / invariants | Planned tests | Status |
|----|------------------------|----------------|--------|
| #413-AC1 — ladder fires on day 7 | CTR-fh-020 | unit: TestLadder | pending |
"""


def test_update_file_zero_matches_returns_none():
    text, n, warnings = tr.update_file(NON_CANONICAL_TABLE, ticket=413, status="covered")
    assert text is None
    assert n == 0


def test_cli_update_non_canonical_table_leaves_file_byte_identical(tmp_path, capsys):
    f = tmp_path / "traceability.md"
    f.write_text(NON_CANONICAL_TABLE)
    before = f.read_text()
    rc = cli.main(["update", str(f), "--ticket", "413", "--status", "covered"])
    after = f.read_text()
    assert rc == 2
    assert after == before  # byte-identical — nothing written
    err = capsys.readouterr().err
    assert "nothing written" in err


def test_cli_update_non_matching_ticket_in_canonical_table_is_also_a_no_op(tmp_path, capsys):
    f = tmp_path / "traceability.md"
    f.write_text(CANONICAL_TABLE_413)
    before = f.read_text()
    rc = cli.main(["update", str(f), "--ticket", "999", "--status", "covered"])
    after = f.read_text()
    assert rc == 2
    assert after == before
    assert "nothing written" in capsys.readouterr().err


def test_cli_update_canonical_table_matching_ticket_updates_rows(tmp_path, capsys):
    f = tmp_path / "traceability.md"
    f.write_text(CANONICAL_TABLE_413)
    rc = cli.main(["update", str(f), "--ticket", "413", "--status", "covered"])
    assert rc == 0
    m = tr.parse_matrix(f.read_text())
    assert m.rows[0].status == "covered"
    assert "OK: updated 1 row(s)" in capsys.readouterr().out


def test_mixed_file_only_canonical_table_changes_non_canonical_round_trips(tmp_path, capsys):
    mixed = NON_CANONICAL_TABLE + "\n" + CANONICAL_TABLE_413
    f = tmp_path / "traceability.md"
    f.write_text(mixed)
    rc = cli.main(["update", str(f), "--ticket", "413", "--status", "covered"])
    after = f.read_text()
    assert rc == 0
    # The non-canonical table's exact original text survives verbatim.
    assert NON_CANONICAL_TABLE.rstrip("\n") in after
    # The canonical table's matching row was updated.
    assert "| #413 | Dunning ladder fires | dunning_test.go:TestLadder | — | — | covered |" in after
    # And the non-canonical row's own status cell was NOT touched.
    assert "#413-AC1 — ladder fires on day 7 | CTR-fh-020 | unit: TestLadder | pending |" in after


def test_cli_dry_run_never_writes_on_a_match(tmp_path, capsys):
    f = tmp_path / "traceability.md"
    f.write_text(CANONICAL_TABLE_413)
    before = f.read_text()
    rc = cli.main(["update", str(f), "--ticket", "413", "--status", "covered", "--dry-run"])
    after = f.read_text()
    assert rc == 0
    assert after == before  # dry run never writes
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "would update 1 row(s)" in out


def test_cli_dry_run_on_zero_matches_still_reports_and_never_writes(tmp_path, capsys):
    f = tmp_path / "traceability.md"
    f.write_text(NON_CANONICAL_TABLE)
    before = f.read_text()
    rc = cli.main(["update", str(f), "--ticket", "413", "--status", "covered", "--dry-run"])
    after = f.read_text()
    assert rc == 2
    assert after == before
    assert "DRY RUN" in capsys.readouterr().err


def test_update_file_only_rerenders_tables_with_a_match():
    """A second canonical table with NO matching row must also round-trip
    byte-identical — only the table that actually changed is touched."""
    other_ticket_table = (
        "| Ticket | Acceptance Criterion | Unit Test | Integration Test | E2E Test | Status |\n"
        "|--------|---------------------|-----------|-----------------|----------|--------|\n"
        "| #77 | unrelated | — | — | — | pending |\n"
    )
    text = CANONICAL_TABLE_413 + "\n" + other_ticket_table
    new_text, n, _warnings = tr.update_file(text, ticket=413, status="covered")
    assert n == 1
    assert other_ticket_table.rstrip("\n") in new_text


def test_update_file_cwd_independent_prose_preserved():
    doc = "Intro.\n\n" + CANONICAL_TABLE_413 + "\nOutro.\n"
    new_text, n, _ = tr.update_file(doc, ticket=413, status="covered")
    assert n == 1
    assert "Intro." in new_text
    assert "Outro." in new_text


def test_update_file_invalid_status_raises():
    with pytest.raises(ValueError):
        tr.update_file(CANONICAL_TABLE_413, ticket=413, status="bogus")
