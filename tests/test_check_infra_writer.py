"""Tests for the infra single-writer / terraform-drift checker (#165).

Subprocess (``terraform plan``) is always mocked via the ``runner`` seam —
these tests never shell out to a real terraform binary.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date

import check_infra_writer as ciw


def _fake_proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=ciw.TERRAFORM_PLAN_ARGS, returncode=returncode, stdout=stdout, stderr=stderr)


def _write_config(tmp_path, entries):
    config = tmp_path / "infra-invariants.json"
    config.write_text(json.dumps(entries))
    return config


def _basic_invariant(root_dir, **overrides) -> dict:
    entry = {
        "id": "INV-infra-001",
        "controls_field": "infra.env-secrets",
        "sanctioned_writers": ["terraform"],
        "terraform_root": str(root_dir),
    }
    entry.update(overrides)
    return entry


# --- invariant declaration parsing -------------------------------------------


def test_parse_valid_invariant():
    invs, malformed = ciw._parse_invariants([
        {
            "id": "INV-infra-001",
            "controls_field": "infra.env-secrets",
            "sanctioned_writers": ["terraform"],
            "terraform_root": "infra/",
            "schedule_note": "nightly cron",
        }
    ])
    assert malformed == []
    assert len(invs) == 1
    inv = invs[0]
    assert inv.id == "INV-infra-001"
    assert inv.controls_field == "infra.env-secrets"
    assert inv.sanctioned_writers == ["terraform"]
    assert inv.terraform_root == "infra/"
    assert inv.schedule_note == "nightly cron"


def test_parse_invalid_id_is_malformed():
    invs, malformed = ciw._parse_invariants([
        {"id": "not-an-id", "controls_field": "infra.x", "sanctioned_writers": ["terraform"], "terraform_root": "infra/"}
    ])
    assert invs == []
    assert malformed and "invalid or missing id" in malformed[0]["reason"]


def test_parse_missing_fields_is_malformed():
    invs, malformed = ciw._parse_invariants([{"id": "INV-infra-001"}])
    assert invs == []
    assert malformed
    reason = malformed[0]["reason"]
    assert "controls_field" in reason
    assert "sanctioned_writers" in reason
    assert "terraform_root" in reason


def test_parse_non_object_entry_is_malformed():
    invs, malformed = ciw._parse_invariants(["oops"])
    assert invs == []
    assert malformed and malformed[0]["index"] == 0


# --- exit-code mapping (0/1/2) ------------------------------------------------


def test_exit_code_0_is_clean(tmp_path):
    root = tmp_path / "infra"
    root.mkdir()
    config = _write_config(tmp_path, [_basic_invariant(root)])
    report = ciw.check(
        config,
        runner=lambda r: _fake_proc(0, stdout="No changes."),
        journal_path=tmp_path / "journal.jsonl",
        available=True,
    )
    assert report.gate_ok
    assert len(report.checked) == 1
    assert report.checked[0]["status"] == "clean"
    assert report.drift == []
    assert not (tmp_path / "journal.jsonl").exists()


def test_exit_code_2_is_drift(tmp_path):
    root = tmp_path / "infra"
    root.mkdir()
    config = _write_config(tmp_path, [_basic_invariant(root)])
    report = ciw.check(
        config,
        runner=lambda r: _fake_proc(2, stdout="~ update in place\n" * 3),
        journal_path=tmp_path / "journal.jsonl",
        available=True,
    )
    assert not report.gate_ok
    assert len(report.drift) == 1
    assert report.drift[0]["invariant_id"] == "INV-infra-001"
    assert report.drift[0]["status"] == "drift"
    assert report.exempted == []
    assert report.errors == []


def test_exit_code_1_is_error_not_drift(tmp_path):
    root = tmp_path / "infra"
    root.mkdir()
    config = _write_config(tmp_path, [_basic_invariant(root)])
    report = ciw.check(
        config,
        runner=lambda r: _fake_proc(1, stderr="Error: no valid credential source found"),
        journal_path=tmp_path / "journal.jsonl",
        available=True,
    )
    # A terraform error must not be conflated with drift, and does not gate-fail.
    assert report.gate_ok
    assert report.drift == []
    assert len(report.errors) == 1
    assert report.errors[0]["status"] == "error"
    assert "credential" in report.errors[0]["reason"]


def test_unexpected_exit_code_is_error(tmp_path):
    root = tmp_path / "infra"
    root.mkdir()
    config = _write_config(tmp_path, [_basic_invariant(root)])
    report = ciw.check(
        config,
        runner=lambda r: _fake_proc(3),
        journal_path=tmp_path / "journal.jsonl",
        available=True,
    )
    assert report.gate_ok
    assert report.drift == []
    assert len(report.errors) == 1
    assert "unexpected terraform exit code 3" in report.errors[0]["reason"]


def test_terraform_root_missing_is_error(tmp_path):
    config = _write_config(tmp_path, [_basic_invariant(tmp_path / "does-not-exist")])
    report = ciw.check(
        config,
        runner=lambda r: _fake_proc(0),
        journal_path=tmp_path / "journal.jsonl",
        available=True,
    )
    assert report.gate_ok  # errors don't gate, only drift/expired exemptions do
    assert len(report.errors) == 1
    assert "not found" in report.errors[0]["reason"]


# --- journaling: drift is an event, not just a state --------------------------


def test_drift_appends_journal_record(tmp_path):
    root = tmp_path / "infra"
    root.mkdir()
    config = _write_config(tmp_path, [_basic_invariant(root)])
    journal = tmp_path / "journal.jsonl"
    plan_output = "\n".join(f"line {i}" for i in range(60))  # more than 40 lines
    ciw.check(config, runner=lambda r: _fake_proc(2, stdout=plan_output), journal_path=journal, available=True)

    assert journal.exists()
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert len(records) == 1
    rec = records[0]
    assert rec["invariant"] == "INV-infra-001"
    assert rec["root"] == str(root)
    assert len(rec["plan_summary_first_40_lines"]) == 40
    assert rec["plan_summary_first_40_lines"][0] == "line 0"
    assert "ts" in rec


def test_journal_is_append_only_across_runs(tmp_path):
    root = tmp_path / "infra"
    root.mkdir()
    config = _write_config(tmp_path, [_basic_invariant(root)])
    journal = tmp_path / "journal.jsonl"

    # First run: drift.
    ciw.check(config, runner=lambda r: _fake_proc(2, stdout="drift #1"), journal_path=journal, available=True)
    # Second run: clean (convergence) — must NOT erase the earlier drift record.
    ciw.check(config, runner=lambda r: _fake_proc(0), journal_path=journal, available=True)

    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert len(records) == 1  # only the drift run journaled; the clean run added nothing
    assert records[0]["plan_summary_first_40_lines"] == ["drift #1"]


# --- exemption lifecycle: active / expired -----------------------------------


def _write_exemption(exemptions_dir, name="exempt.json", **overrides) -> None:
    exemptions_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "scope": "infra.env-secrets",
        "reason": "planned migration",
        "expiry": "2099-01-01",
        "approver": "pat",
        "incident_ref": "INC-42",
    }
    record.update(overrides)
    (exemptions_dir / name).write_text(json.dumps(record))


def test_active_exemption_downgrades_drift(tmp_path):
    root = tmp_path / "infra"
    root.mkdir()
    exemptions_dir = tmp_path / "exemptions"
    _write_exemption(exemptions_dir, expiry="2099-01-01")
    config = _write_config(tmp_path, [_basic_invariant(root)])

    report = ciw.check(
        config,
        exemptions_dir=exemptions_dir,
        runner=lambda r: _fake_proc(2, stdout="drift"),
        journal_path=tmp_path / "journal.jsonl",
        today=date(2026, 7, 19),
        available=True,
    )
    assert report.gate_ok  # exempted drift does not gate-fail
    assert report.drift == []
    assert len(report.exempted) == 1
    assert report.exempted[0]["exemption"]["incident_ref"] == "INC-42"
    # Still journaled even though exempted — break-glass doesn't erase the event.
    journal_records = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert len(journal_records) == 1


def test_expired_exemption_does_not_downgrade_and_is_itself_a_finding(tmp_path):
    root = tmp_path / "infra"
    root.mkdir()
    exemptions_dir = tmp_path / "exemptions"
    _write_exemption(exemptions_dir, expiry="2020-01-01")
    config = _write_config(tmp_path, [_basic_invariant(root)])

    report = ciw.check(
        config,
        exemptions_dir=exemptions_dir,
        runner=lambda r: _fake_proc(2, stdout="drift"),
        journal_path=tmp_path / "journal.jsonl",
        today=date(2026, 7, 19),
        available=True,
    )
    assert not report.gate_ok
    assert len(report.drift) == 1  # expired exemption does NOT downgrade
    assert report.exempted == []
    assert len(report.expired_exemptions) == 1
    assert report.expired_exemptions[0]["scope"] == "infra.env-secrets"


def test_exemption_scope_mismatch_does_not_apply(tmp_path):
    root = tmp_path / "infra"
    root.mkdir()
    exemptions_dir = tmp_path / "exemptions"
    _write_exemption(exemptions_dir, scope="infra.other-scope", expiry="2099-01-01")
    config = _write_config(tmp_path, [_basic_invariant(root)])

    report = ciw.check(
        config,
        exemptions_dir=exemptions_dir,
        runner=lambda r: _fake_proc(2, stdout="drift"),
        journal_path=tmp_path / "journal.jsonl",
        today=date(2026, 7, 19),
        available=True,
    )
    assert not report.gate_ok
    assert len(report.drift) == 1
    assert report.exempted == []


def test_malformed_exemption_is_reported():
    exemptions_dir_content = {"scope": "x"}  # missing required fields
    # exercised via load_exemptions directly for a focused unit test
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "bad.json"
        p.write_text(json.dumps(exemptions_dir_content))
        exemptions, malformed = ciw.load_exemptions(Path(d))
        assert exemptions == []
        assert malformed and "missing field" in malformed[0]["reason"]


def test_missing_exemptions_dir_degrades_gracefully(tmp_path):
    exemptions, malformed = ciw.load_exemptions(tmp_path / "nope")
    assert exemptions == []
    assert malformed == []


# --- terraform availability degradation --------------------------------------


def test_terraform_unavailable_degrades_gracefully(tmp_path):
    root = tmp_path / "infra"
    root.mkdir()
    config = _write_config(tmp_path, [_basic_invariant(root)])

    def _boom(r):
        raise AssertionError("runner must not be called when terraform is unavailable")

    report = ciw.check(config, runner=_boom, available=False)
    assert report.available is False
    assert report.checked == []
    assert report.drift == []
    assert any("not installed" in w for w in report.warnings)


def test_missing_config_degrades_gracefully(tmp_path):
    report = ciw.check(tmp_path / "no-such-config.json", available=True)
    assert report.gate_ok
    assert report.checked == []
    assert any("not found" in w or "no infra invariants" in w for w in report.warnings)


# --- authority line always present --------------------------------------------


def test_authority_line_present_in_report_and_text(tmp_path):
    report = ciw.check(tmp_path / "nope.json", available=True)
    assert ciw.AUTHORITY in report.authority
    rendered = ciw.render_text(report)
    assert ciw.AUTHORITY in rendered


# --- CLI: gate behavior --------------------------------------------------------


def test_cli_report_only_exits_0_on_drift(tmp_path, monkeypatch, capsys):
    root = tmp_path / "infra"
    root.mkdir()
    config = _write_config(tmp_path, [_basic_invariant(root)])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ciw, "terraform_available", lambda: True)
    monkeypatch.setattr(ciw, "_default_runner", lambda r: _fake_proc(2, stdout="drift"))

    rc = ciw.main(["--config", str(config)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Drift" in out


def test_cli_gate_exits_1_on_unexempted_drift(tmp_path, monkeypatch, capsys):
    root = tmp_path / "infra"
    root.mkdir()
    config = _write_config(tmp_path, [_basic_invariant(root)])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ciw, "terraform_available", lambda: True)
    monkeypatch.setattr(ciw, "_default_runner", lambda r: _fake_proc(2, stdout="drift"))

    rc = ciw.main(["--config", str(config), "--gate"])
    assert rc == 1


def test_cli_gate_passes_on_clean_plan(tmp_path, monkeypatch, capsys):
    root = tmp_path / "infra"
    root.mkdir()
    config = _write_config(tmp_path, [_basic_invariant(root)])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ciw, "terraform_available", lambda: True)
    monkeypatch.setattr(ciw, "_default_runner", lambda r: _fake_proc(0))

    rc = ciw.main(["--config", str(config), "--gate"])
    assert rc == 0


def test_cli_json_format(tmp_path, monkeypatch, capsys):
    root = tmp_path / "infra"
    root.mkdir()
    config = _write_config(tmp_path, [_basic_invariant(root)])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ciw, "terraform_available", lambda: True)
    monkeypatch.setattr(ciw, "_default_runner", lambda r: _fake_proc(0))

    rc = ciw.main(["--config", str(config), "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["available"] is True
    assert payload["gate_ok"] is True
    assert payload["authority"] == ciw.AUTHORITY


def test_cli_terraform_missing_exits_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ciw, "terraform_available", lambda: False)
    rc = ciw.main(["--config", str(tmp_path / "irrelevant.json"), "--gate"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "NOT AVAILABLE" in out


# --- ID validation guarded import ---------------------------------------------


def test_valid_inv_id_accepts_shared_shape():
    assert ciw._valid_inv_id("INV-infra-001")
    assert ciw._valid_inv_id("INV-INFRA-001")
    assert not ciw._valid_inv_id("INV-infra-1")
    assert not ciw._valid_inv_id("not-an-id")
