"""Tests for /status — the live-derived one-screen target state (#213)."""

from __future__ import annotations

import json
from pathlib import Path

import artifacts
import pytest
import status

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    """Isolated ~/.chief-wiggum — tests must never touch the real home dir."""
    d = tmp_path / "cw-user"
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(d))
    return d


def _make_target(tmp_path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    return target


# --- embedded / missing pieces ---------------------------------------------------


def test_embedded_target_with_nothing(user_dir, tmp_path):
    """A bare embedded target: whole-repo scope, no gates, no ratchet, no
    patterns, no debt inventory — every absence named, nothing crashes."""
    target = _make_target(tmp_path)
    st = status.gather(target)
    assert st["resolver"]["mode"] == "embedded"
    assert st["resolver"]["meta_root"] == str(target / "docs")
    assert "whole repo" in st["resolver"]["scope"]
    assert st["gates"] == []
    assert st["ratchet"] == {"configured": False}
    assert st["patterns"] == []
    assert st["debt"] is None

    text = status.render_text(st)
    assert "Footprint: embedded" in text
    assert "no gate-validation records" in text
    assert "no ratchet config" in text
    assert "(none adopted)" in text
    assert "no inventory" in text


def test_sidecar_target_routes_meta_and_scope(user_dir, tmp_path):
    """A sidecar election moves the meta root out of the target; scope.json in
    the sidecar meta root feeds the scope summary and everything stays derived."""
    target = _make_target(tmp_path)
    artifacts.elect(target, "sidecar", backing="local")
    resolver = artifacts.Resolver.resolve(target)
    resolver.meta_root.mkdir(parents=True, exist_ok=True)
    (resolver.meta_root / "scope.json").write_text(
        json.dumps({"include": ["internal/**"], "exclude": ["internal/legacy/**"]})
    )
    st = status.gather(target)
    assert st["resolver"]["mode"] == "sidecar"
    assert str(user_dir) in st["resolver"]["meta_root"]
    assert "include: internal/**" in st["resolver"]["scope"]
    assert "exclude: internal/legacy/**" in st["resolver"]["scope"]
    # No CW files in the target tree.
    assert not (target / "docs").exists()


def test_ratchet_counts_come_from_the_scorecard(user_dir, tmp_path):
    target = _make_target(tmp_path)
    q = target / "docs" / "quality"
    q.mkdir(parents=True)
    (q / "ratchet.json").write_text(json.dumps({"suites": []}))
    (q / "ratchet-scorecard.json").write_text(json.dumps({
        "pass_set": ["s::t1", "s::t2", "s::t3"],
        "contract_hashes": {"CTR-a-001": "h1", "INV-a-002": "h2"},
        "verifier_test_hashes": {"tests/test_a.py::test_x": "vh"},
    }))
    st = status.gather(target)
    assert st["ratchet"] == {
        "configured": True, "scorecard": True,
        "pass_set": 3, "contracts": 2, "verifier_hashes": 1,
    }
    assert "pass-set: 3 case(s)" in status.render_text(st)


def test_stale_scorecard_warns_in_git_target(user_dir, tmp_path):
    """F12: a scorecard stamped against an older HEAD gets a staleness
    warning line (Resolver.check_stale); a fresh one does not."""
    import subprocess

    target = _make_target(tmp_path)

    def _git(*args):
        subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    (target / "README.md").write_text("hi\n")
    _git("add", ".")
    _git("commit", "-q", "-m", "init")
    head = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    q = target / "docs" / "quality"
    q.mkdir(parents=True)
    (q / "ratchet.json").write_text(json.dumps({"suites": []}))
    (q / "ratchet-scorecard.json").write_text(json.dumps({
        "pass_set": [], "contract_hashes": {}, "verifier_test_hashes": {},
        "target_sha": head,
    }))
    st = status.gather(target)
    assert "stale" not in st["ratchet"]  # fresh: no warning

    (target / "new.txt").write_text("x\n")
    _git("add", ".")
    _git("commit", "-q", "-m", "more")
    st = status.gather(target)
    assert "stale" in st["ratchet"] and head in st["ratchet"]["stale"]
    assert "WARNING" in status.render_text(st)


def test_non_git_target_scorecard_never_warns_stale(user_dir, tmp_path):
    """Staleness is a git concept: a non-git target has no HEAD to be stale
    against — no warning, exactly the pre-F12 rendering."""
    target = _make_target(tmp_path)
    q = target / "docs" / "quality"
    q.mkdir(parents=True)
    (q / "ratchet.json").write_text(json.dumps({"suites": []}))
    (q / "ratchet-scorecard.json").write_text(json.dumps({"pass_set": []}))
    st = status.gather(target)
    assert "stale" not in st["ratchet"]


def test_ratchet_config_without_scorecard(user_dir, tmp_path):
    target = _make_target(tmp_path)
    q = target / "docs" / "quality"
    q.mkdir(parents=True)
    (q / "ratchet.json").write_text(json.dumps({"suites": []}))
    st = status.gather(target)
    assert st["ratchet"] == {"configured": True, "scorecard": False}
    assert "no scorecard" in status.render_text(st)


def test_adopted_patterns_via_resolver(user_dir, tmp_path):
    target = _make_target(tmp_path)
    p = target / "docs" / "patterns"
    p.mkdir(parents=True)
    (p / "adopted.json").write_text(json.dumps({
        "patterns": {
            "tiered-subscription": {"version": "1", "applied_at": "2026-07-21T00:00:00Z"},
            "referral-invite-loop": {"version": "2", "applied_at": "2026-08-01T00:00:00Z"},
        }
    }))
    st = status.gather(target)
    assert [x["id"] for x in st["patterns"]] == ["referral-invite-loop", "tiered-subscription"]
    assert "tiered-subscription v1" in status.render_text(st)


def test_debt_counts_by_severity(user_dir, tmp_path):
    target = _make_target(tmp_path)
    q = target / "docs" / "quality"
    q.mkdir(parents=True)
    (q / "debt.json").write_text(json.dumps({
        "items": [
            {"id": "DEBT-001", "severity": "high"},
            {"id": "DEBT-002", "severity": "high"},
            {"id": "DEBT-003", "severity": "low"},
            {"id": "DEBT-004"},
        ]
    }))
    st = status.gather(target)
    assert st["debt"] == {"high": 2, "low": 1, "unknown": 1}
    assert "high: 2" in status.render_text(st)


def test_debt_inventory_present_but_empty(user_dir, tmp_path):
    target = _make_target(tmp_path)
    q = target / "docs" / "quality"
    q.mkdir(parents=True)
    (q / "debt.json").write_text(json.dumps({"items": []}))
    st = status.gather(target)
    assert st["debt"] == {}
    assert "zero items" in status.render_text(st)


# --- gate ledger -----------------------------------------------------------------


def test_gate_ledger_flags_unreadable_record_as_missing(user_dir, tmp_path):
    target = _make_target(tmp_path)
    v = target / "docs" / "quality" / "validation"
    v.mkdir(parents=True)
    (v / "broken_gate.json").write_text("{not json")
    st = status.gather(target)
    assert st["gates"] == [{
        "gate": "broken_gate", "verdict": "missing",
        "wired": False, "last_authority_action": None,
    }]


def test_gate_ledger_against_this_repos_real_validation_dir(user_dir):
    """The real ledger: every shipped validation record must read as passing
    (a failing one here means a scanner-version went stale — re-author it),
    and the wired state comes from the real journal's gate-authority events."""
    st = status.gather(ROOT)
    gates = {g["gate"]: g for g in st["gates"]}
    assert len(gates) == 7, f"expected the 7 shipped records, got {sorted(gates)}"
    failing = [n for n, g in gates.items() if g["verdict"] != "passing"]
    assert not failing, f"stale/failing validation record(s): {failing}"
    # ratchet was journaled-wired in the real journal (chief-wiggum#198 era).
    assert gates["ratchet"]["wired"] is True


# --- CLI -------------------------------------------------------------------------


def test_cli_json_format(user_dir, tmp_path, capsys):
    target = _make_target(tmp_path)
    rc = status.main(["--repo", str(target), "--format", "json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"resolver", "gates", "ratchet", "patterns", "debt",
                         "not_measured", "partial_coverage", "crashed_engines",
                         "adoption"}


def test_cli_missing_target_is_usage_error(user_dir, tmp_path, capsys):
    rc = status.main(["--repo", str(tmp_path / "nope")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_cli_never_writes_anything(user_dir, tmp_path, capsys):
    """/status is read-only: a run leaves the target byte-identical."""
    target = _make_target(tmp_path)
    (target / "docs").mkdir()
    before = sorted(p for p in target.rglob("*"))
    rc = status.main(["--repo", str(target)])
    assert rc == 0
    capsys.readouterr()
    assert sorted(p for p in target.rglob("*")) == before
    # ...and the isolated user dir gained nothing either (no election written).
    assert not (Path(str(user_dir)) / "meta").exists() or not any(
        (Path(str(user_dir)) / "meta").rglob("election.json")
    )
