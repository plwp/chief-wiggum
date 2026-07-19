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


def _basic_invariant(terraform_root: str = "infra", **overrides) -> dict:
    entry = {
        "id": "INV-infra-001",
        "controls_field": "infra.env-secrets",
        "sanctioned_writers": ["terraform"],
        "terraform_root": terraform_root,
    }
    entry.update(overrides)
    return entry


def _setup_repo(tmp_path, entries=None):
    """A minimal 'repo': config at tmp_path, terraform root at tmp_path/infra."""
    (tmp_path / "infra").mkdir()
    return _write_config(tmp_path, entries if entries is not None else [_basic_invariant()])


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
    config = _setup_repo(tmp_path)
    report = ciw.check(
        config,
        repo_root=tmp_path,
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
    config = _setup_repo(tmp_path)
    report = ciw.check(
        config,
        repo_root=tmp_path,
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


def test_exit_code_1_is_error_not_drift_and_fails_gate(tmp_path):
    config = _setup_repo(tmp_path)
    report = ciw.check(
        config,
        repo_root=tmp_path,
        runner=lambda r: _fake_proc(1, stderr="Error: no valid credential source found"),
        journal_path=tmp_path / "journal.jsonl",
        available=True,
    )
    # A terraform error must not be conflated with drift, but the gate could not
    # evaluate the invariant — so it must FAIL the gate, not silently pass.
    assert not report.gate_ok
    assert report.drift == []
    assert len(report.errors) == 1
    assert report.errors[0]["status"] == "error"
    assert "credential" in report.errors[0]["reason"]


def test_unexpected_exit_code_is_error_and_fails_gate(tmp_path):
    config = _setup_repo(tmp_path)
    report = ciw.check(
        config,
        repo_root=tmp_path,
        runner=lambda r: _fake_proc(3),
        journal_path=tmp_path / "journal.jsonl",
        available=True,
    )
    assert not report.gate_ok
    assert report.drift == []
    assert len(report.errors) == 1
    assert "unexpected terraform exit code 3" in report.errors[0]["reason"]


def test_terraform_root_missing_is_error_and_fails_gate(tmp_path):
    config = _write_config(tmp_path, [_basic_invariant("does-not-exist")])
    report = ciw.check(
        config,
        repo_root=tmp_path,
        runner=lambda r: _fake_proc(0),
        journal_path=tmp_path / "journal.jsonl",
        available=True,
    )
    assert not report.gate_ok  # could not evaluate -> gate fails
    assert len(report.errors) == 1
    assert "not found" in report.errors[0]["reason"]


def test_malformed_declaration_fails_gate(tmp_path):
    config = _write_config(tmp_path, [{"id": "not-an-id"}])
    report = ciw.check(config, repo_root=tmp_path, runner=lambda r: _fake_proc(0), available=True)
    assert not report.gate_ok
    assert report.malformed


def test_unparseable_config_is_malformed_and_fails_gate(tmp_path):
    config = tmp_path / "infra-invariants.json"
    config.write_text("{not json")
    report = ciw.check(config, repo_root=tmp_path, available=True)
    assert not report.gate_ok
    assert report.malformed and "cannot parse config" in report.malformed[0]["reason"]


def test_non_array_config_is_malformed_and_fails_gate(tmp_path):
    config = tmp_path / "infra-invariants.json"
    config.write_text(json.dumps({"id": "INV-infra-001"}))
    report = ciw.check(config, repo_root=tmp_path, available=True)
    assert not report.gate_ok
    assert report.malformed and "JSON array" in report.malformed[0]["reason"]


def test_empty_config_array_is_ok(tmp_path):
    config = _write_config(tmp_path, [])
    report = ciw.check(config, repo_root=tmp_path, available=True)
    assert report.gate_ok
    assert any("no invariants" in w for w in report.warnings)


# --- terraform_root resolution: repo-rooted, never CWD -------------------------


def test_terraform_root_resolves_from_repo_root_not_cwd(tmp_path, monkeypatch):
    """The declared root must resolve against the repo root (derived from the
    config file's location), regardless of the directory the check runs from."""
    config = _setup_repo(tmp_path)
    elsewhere = tmp_path / "unrelated-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    seen_roots = []

    def runner(root):
        seen_roots.append(root)
        return _fake_proc(0)

    # No explicit repo_root: derived from the config's location (tmp_path).
    report = ciw.check(config, runner=runner, journal_path=tmp_path / "j.jsonl", available=True)
    assert report.gate_ok
    assert len(report.checked) == 1
    assert seen_roots == [(tmp_path / "infra").resolve()]


def test_absolute_terraform_root_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    config = _write_config(tmp_path, [_basic_invariant(str(outside))])
    report = ciw.check(config, repo_root=tmp_path, runner=lambda r: _fake_proc(0), available=True)
    assert not report.gate_ok
    assert len(report.errors) == 1
    assert "escapes repo root" in report.errors[0]["reason"]


def test_dotdot_terraform_root_is_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "sibling").mkdir()
    config = _write_config(repo, [_basic_invariant("../sibling")])
    report = ciw.check(config, repo_root=repo, runner=lambda r: _fake_proc(0), available=True)
    assert not report.gate_ok
    assert len(report.errors) == 1
    assert "escapes repo root" in report.errors[0]["reason"]


def test_default_journal_lands_in_repo_root(tmp_path, monkeypatch):
    config = _setup_repo(tmp_path)
    elsewhere = tmp_path / "unrelated-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    ciw.check(config, repo_root=tmp_path, runner=lambda r: _fake_proc(2, stdout="drift"), available=True)

    assert (tmp_path / "docs" / "quality" / "infra-drift.jsonl").exists()
    assert not (elsewhere / "docs").exists()  # nothing written relative to CWD


# --- journaling: drift is an event, not just a state --------------------------


def test_drift_appends_journal_record(tmp_path):
    config = _setup_repo(tmp_path)
    journal = tmp_path / "journal.jsonl"
    plan_output = "\n".join(f"line {i}" for i in range(60))  # more than 40 lines
    ciw.check(
        config, repo_root=tmp_path,
        runner=lambda r: _fake_proc(2, stdout=plan_output),
        journal_path=journal, available=True,
    )

    assert journal.exists()
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert len(records) == 1
    rec = records[0]
    assert rec["invariant"] == "INV-infra-001"
    assert rec["root"] == "infra"
    assert len(rec["plan_summary_first_40_lines"]) == 40
    assert rec["plan_summary_first_40_lines"][0] == "line 0"
    assert "ts" in rec


def test_journal_is_append_only_across_runs(tmp_path):
    config = _setup_repo(tmp_path)
    journal = tmp_path / "journal.jsonl"

    # First run: drift.
    ciw.check(config, repo_root=tmp_path, runner=lambda r: _fake_proc(2, stdout="drift #1"),
              journal_path=journal, available=True)
    # Second run: clean (convergence) — must NOT erase the earlier drift record.
    ciw.check(config, repo_root=tmp_path, runner=lambda r: _fake_proc(0),
              journal_path=journal, available=True)

    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert len(records) == 1  # only the drift run journaled; the clean run added nothing
    assert records[0]["plan_summary_first_40_lines"] == ["drift #1"]


def test_unwritable_journal_still_reports_drift(tmp_path):
    """A journal-write failure must not crash report-only mode or swallow the
    drift finding: the drift is recorded FIRST, and the failed write becomes an
    explicit (gate-failing) error finding."""
    config = _setup_repo(tmp_path)
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the journal's parent dir should be")
    journal = blocker / "journal.jsonl"  # parent is a file -> mkdir/open raises OSError

    report = ciw.check(
        config, repo_root=tmp_path,
        runner=lambda r: _fake_proc(2, stdout="drift"),
        journal_path=journal, available=True,
    )
    assert len(report.drift) == 1  # the finding survived the failed journal write
    assert len(report.errors) == 1
    assert "journal write failed" in report.errors[0]["reason"]
    assert not report.gate_ok
    # Report-only mode still renders the full report without raising.
    rendered = ciw.render_text(report)
    assert "Drift" in rendered and "journal write failed" in rendered


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
    config = _setup_repo(tmp_path)
    exemptions_dir = tmp_path / "exemptions"
    _write_exemption(exemptions_dir, expiry="2099-01-01")

    report = ciw.check(
        config,
        repo_root=tmp_path,
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
    config = _setup_repo(tmp_path)
    exemptions_dir = tmp_path / "exemptions"
    _write_exemption(exemptions_dir, expiry="2020-01-01")

    report = ciw.check(
        config,
        repo_root=tmp_path,
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
    config = _setup_repo(tmp_path)
    exemptions_dir = tmp_path / "exemptions"
    _write_exemption(exemptions_dir, scope="infra.other-scope", expiry="2099-01-01")

    report = ciw.check(
        config,
        repo_root=tmp_path,
        exemptions_dir=exemptions_dir,
        runner=lambda r: _fake_proc(2, stdout="drift"),
        journal_path=tmp_path / "journal.jsonl",
        today=date(2026, 7, 19),
        available=True,
    )
    assert not report.gate_ok
    assert len(report.drift) == 1
    assert report.exempted == []


def test_malformed_exemption_is_reported_and_fails_gate(tmp_path):
    config = _setup_repo(tmp_path)
    exemptions_dir = tmp_path / "exemptions"
    exemptions_dir.mkdir()
    (exemptions_dir / "bad.json").write_text(json.dumps({"scope": "x"}))  # missing fields

    report = ciw.check(
        config,
        repo_root=tmp_path,
        exemptions_dir=exemptions_dir,
        runner=lambda r: _fake_proc(0),
        journal_path=tmp_path / "journal.jsonl",
        available=True,
    )
    assert not report.gate_ok
    assert report.malformed and "missing field" in report.malformed[0]["reason"]


def test_missing_exemptions_dir_degrades_gracefully(tmp_path):
    exemptions, malformed = ciw.load_exemptions(tmp_path / "nope")
    assert exemptions == []
    assert malformed == []


# --- terraform availability degradation --------------------------------------


def test_terraform_unavailable_degrades_gracefully(tmp_path):
    config = _setup_repo(tmp_path)

    def _boom(r):
        raise AssertionError("runner must not be called when terraform is unavailable")

    report = ciw.check(config, repo_root=tmp_path, runner=_boom, available=False)
    assert report.available is False
    assert report.gate_ok  # the ONE graceful-degradation exception
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


def _cli_setup(tmp_path, monkeypatch, returncode: int, entries=None, stdout: str = "", stderr: str = ""):
    config = _setup_repo(tmp_path, entries)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ciw, "terraform_available", lambda: True)
    monkeypatch.setattr(ciw, "_default_runner", lambda r: _fake_proc(returncode, stdout=stdout, stderr=stderr))
    return config


def test_cli_report_only_exits_0_on_drift(tmp_path, monkeypatch, capsys):
    config = _cli_setup(tmp_path, monkeypatch, 2, stdout="drift")
    rc = ciw.main(["--config", str(config), "--repo", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Drift" in out


def test_cli_gate_exits_1_on_unexempted_drift(tmp_path, monkeypatch, capsys):
    config = _cli_setup(tmp_path, monkeypatch, 2, stdout="drift")
    rc = ciw.main(["--config", str(config), "--repo", str(tmp_path), "--gate"])
    assert rc == 1


def test_cli_gate_exits_1_on_terraform_error(tmp_path, monkeypatch, capsys):
    config = _cli_setup(tmp_path, monkeypatch, 1, stderr="Error: backend init failed")
    rc = ciw.main(["--config", str(config), "--repo", str(tmp_path), "--gate"])
    assert rc == 1
    # ...but report-only still exits 0 on the same error.
    rc = ciw.main(["--config", str(config), "--repo", str(tmp_path)])
    assert rc == 0


def test_cli_gate_exits_1_on_malformed_config(tmp_path, monkeypatch, capsys):
    config = _cli_setup(tmp_path, monkeypatch, 0, entries=[{"id": "not-an-id"}])
    rc = ciw.main(["--config", str(config), "--repo", str(tmp_path), "--gate"])
    assert rc == 1


def test_cli_gate_passes_on_clean_plan(tmp_path, monkeypatch, capsys):
    config = _cli_setup(tmp_path, monkeypatch, 0)
    rc = ciw.main(["--config", str(config), "--repo", str(tmp_path), "--gate"])
    assert rc == 0


def test_cli_json_format(tmp_path, monkeypatch, capsys):
    config = _cli_setup(tmp_path, monkeypatch, 0)
    rc = ciw.main(["--config", str(config), "--repo", str(tmp_path), "--format", "json"])
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
