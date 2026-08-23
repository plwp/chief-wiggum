"""Tests for scripts/check_unresolved.py."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_unresolved  # noqa: E402


def test_clean_artifacts_produce_no_findings(tmp_path):
    (tmp_path / "contracts.json").write_text(json.dumps({
        "entities": [{"name": "Order", "description": "An order placed by a customer"}],
    }))
    (tmp_path / "invariants.md").write_text("INV-001: every order has a customer_id\n")
    assert check_unresolved.scan([tmp_path]) == []


def test_json_marker_found_with_location_and_provenance(tmp_path):
    model = {
        "entities": [{
            "name": "Metric",
            "derived_from": [{"type": "ticket", "ref": "#42"}],
            "fields": [{
                "name": "total",
                "notes": "TBD: confirm column name against the dbt model",
            }],
        }],
    }
    (tmp_path / "contracts.json").write_text(json.dumps(model))
    findings = check_unresolved.scan([tmp_path])
    assert len(findings) == 1
    f = findings[0]
    assert f.marker == "TBD"
    assert "entities[0].fields[0].notes" in f.location
    assert f.tickets == ["#42"]


def test_markdown_marker_found_with_line_number(tmp_path):
    (tmp_path / "adr.md").write_text("# ADR\n\nUNRESOLVED: which region hosts prod? (#7)\n")
    findings = check_unresolved.scan([tmp_path])
    assert len(findings) == 1
    assert findings[0].location == "line 3"
    assert findings[0].tickets == ["#7"]


def test_lowercase_placeholder_prose_does_not_trip(tmp_path):
    model = {"pages": {"/": {"components": {
        "search": {"type": "input", "description": "search box with placeholder text 'find a video'"},
    }}}}
    (tmp_path / "ui-spec.json").write_text(json.dumps(model))
    (tmp_path / "notes.md").write_text("the input shows placeholder copy until focus\n")
    assert check_unresolved.scan([tmp_path]) == []


def test_blocked_tickets_aggregation(tmp_path):
    model = {
        "entities": [{
            "name": "A",
            "derived_from": [{"type": "ticket", "ref": "#10"}],
            "description": "TBD: confirm source",
            "fields": [{"name": "x", "notes": "PLACEHOLDER until schema introspection"}],
        }],
    }
    (tmp_path / "contracts.json").write_text(json.dumps(model))
    findings = check_unresolved.scan([tmp_path])
    assert check_unresolved.blocked_tickets(findings) == {"#10": 2}


def test_cli_exit_codes(tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "contracts.json").write_text(json.dumps({"entities": []}))

    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "adr.md").write_text("TBD: confirm\n")

    script = SCRIPTS / "check_unresolved.py"
    ok = subprocess.run([sys.executable, str(script), str(clean)], capture_output=True, text=True)
    assert ok.returncode == 0
    assert "OK" in ok.stdout

    bad = subprocess.run([sys.executable, str(script), str(dirty), "--format", "json"],
                         capture_output=True, text=True)
    assert bad.returncode == 1
    payload = json.loads(bad.stdout)
    assert payload["count"] == 1

    missing = subprocess.run([sys.executable, str(script), str(tmp_path / "nope")],
                             capture_output=True, text=True)
    assert missing.returncode == 2


# --- broken instrument: the scanner saw nothing (chief-wiggum#289) -----------
#
# This is the gate that stops dependent work being built on a guess. An
# artifact it could not parse contributed zero findings, printed "OK: no
# unresolved markers found", exited 0, and was logged to factory telemetry as
# a pass — the marker-bearing file it never read notwithstanding.


def _run(*args):
    return subprocess.run([sys.executable, str(SCRIPTS / "check_unresolved.py"), *args],
                          capture_output=True, text=True)


def test_unparseable_json_artifact_is_error_not_ok(tmp_path):
    (tmp_path / "contracts.json").write_text('{"entities": [ TBD ')
    report = check_unresolved.scan_report([tmp_path])
    assert report.outcome == "error"
    assert report.unparsed
    assert "contracts.json" in report.unparsed[0]["file"]


def test_unparseable_artifact_exits_nonzero(tmp_path):
    (tmp_path / "contracts.json").write_text('{"entities": [ TBD ')
    result = _run(str(tmp_path), "--format", "json")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "error"
    assert payload["measured"]["files_scanned"] == 0


def test_clean_scan_reports_the_measured_denominator(tmp_path):
    (tmp_path / "contracts.json").write_text(json.dumps({"entities": []}))
    (tmp_path / "adr.md").write_text("# ADR\n\nall confirmed\n")
    report = check_unresolved.scan_report([tmp_path])
    assert report.outcome == "pass"
    assert report.measured["files_scanned"] == 2


def test_no_scannable_artifacts_is_inapplicable_not_a_pass(tmp_path):
    """An epic dir holding no .md/.json yet has nothing to scan. Honest
    absence — but it must not print the same OK a measured clean scan does."""
    (tmp_path / "notes.txt").write_text("TBD: not an artifact this gate reads\n")
    report = check_unresolved.scan_report([tmp_path])
    assert report.outcome == "inapplicable"
    assert report.measured["files_scanned"] == 0


def test_inapplicable_says_so_and_still_exits_zero(tmp_path):
    (tmp_path / "notes.txt").write_text("nothing scannable\n")
    result = _run(str(tmp_path))
    assert result.returncode == 0
    assert "INAPPLICABLE" in result.stdout + result.stderr


def test_markers_found_is_findings(tmp_path):
    (tmp_path / "adr.md").write_text("TBD: confirm\n")
    report = check_unresolved.scan_report([tmp_path])
    assert report.outcome == "findings"
    assert report.measured["files_scanned"] == 1


def test_text_output_prints_the_denominator(tmp_path):
    (tmp_path / "contracts.json").write_text(json.dumps({"entities": []}))
    result = _run(str(tmp_path))
    assert "1 file(s) scanned" in result.stdout


def test_scan_stays_a_plain_finding_list(tmp_path):
    """Backward compatibility: existing callers unpack a list, not a report."""
    (tmp_path / "adr.md").write_text("TBD: confirm\n")
    assert isinstance(check_unresolved.scan([tmp_path]), list)


# --- UNVERIFIED as a marker, in marker position only (chief-wiggum#350) -------
#
# Authors were already writing `**UNVERIFIED: ...**` in shipped epic artifacts
# believing it gated dependent work. It did not: the token was not in the
# marker set, so those unknowns propagated silently. Adding it bare would be
# worse than not adding it -- UNVERIFIED is also ordinary domain vocabulary
# (a FactStatus enum member, a state-machine state), and a bare-word alias
# measured 82% false on a 314-file corpus of real artifacts. So it counts only
# where it INTRODUCES a claim.

import pytest  # noqa: E402


@pytest.mark.parametrize("line", [
    "**UNVERIFIED: the live webhook endpoints' signing secret**",
    "**UNVERIFIED whether the registered endpoints were updated to match**",
    "UNVERIFIED - nobody has called this route",
    "UNVERIFIED \u2014 nobody has called this route",
    "UNVERIFIED that the envelope is shaped this way",
    "UNVERIFIED which account hosts the prod stream",
])
def test_unverified_in_marker_position_is_a_marker(tmp_path, line):
    (tmp_path / "contracts.md").write_text(line + "\n")
    findings = check_unresolved.scan([tmp_path])
    assert len(findings) == 1
    assert findings[0].marker == "UNVERIFIED"


@pytest.mark.parametrize("line", [
    '    UNVERIFIED = "unverified"     # Extracted but not cross-referenced',
    "status: FactStatus = FactStatus.UNVERIFIED",
    "GIVEN a fact with status=UNVERIFIED",
    "| UNVERIFIED | VERIFIED | `verify(id)` | Corroborating fact exists |",
    "The new fact starts as UNVERIFIED regardless of the original's status.",
    "AND the anecdotal fact's status remains UNVERIFIED",
])
def test_unverified_as_domain_vocabulary_is_not_a_marker(tmp_path, line):
    """Every one of these is a real line from a shipped epic artifact
    (a shipped recon-engine epic in a target repo). Flagging them would make the gate
    noisy on correct work, which is how an operator learns to --force."""
    (tmp_path / "state-machines.md").write_text(line + "\n")
    assert check_unresolved.scan([tmp_path]) == []


def test_unverified_marker_is_found_in_json_artifacts_too(tmp_path):
    (tmp_path / "contracts.json").write_text(json.dumps({
        "entities": [{"name": "Charge", "description": "UNVERIFIED: real envelope shape"}],
    }))
    findings = check_unresolved.scan([tmp_path])
    assert len(findings) == 1
    assert findings[0].marker == "UNVERIFIED"


def test_lowercase_unverified_is_not_a_marker(tmp_path):
    (tmp_path / "notes.md").write_text("this is unverified: we should check it\n")
    assert check_unresolved.scan([tmp_path]) == []


def test_the_original_markers_still_report_their_own_token(tmp_path):
    """The pattern became a two-branch alternation; the reported marker must
    still be the token that actually matched, not the first branch."""
    (tmp_path / "a.md").write_text("TBD: one\nUNRESOLVED: two\nPLACEHOLDER: three\n")
    assert [f.marker for f in check_unresolved.scan([tmp_path])] == [
        "TBD", "UNRESOLVED", "PLACEHOLDER"]


def test_an_unverified_marker_blocks_the_gate(tmp_path):
    (tmp_path / "contracts.md").write_text(
        "**UNVERIFIED: the live webhook secret** blocks #42\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_unresolved.py"), str(tmp_path)],
        capture_output=True, text=True)
    assert result.returncode == 1
    assert "UNVERIFIED" in result.stdout
