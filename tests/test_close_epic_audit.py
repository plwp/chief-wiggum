"""Tests for the close-epic audit orchestrator (P2-14)."""

from __future__ import annotations

import json

import close_epic_audit as cea

TRACE = """\
| Ticket | Acceptance Criterion | Unit Test | Status |
|---|---|---|---|
| #42 | health | api_test.go:TestHealth | passing |
| #43 | missing one | — | missing |
"""


def _epic(tmp_path, *, traceability=True, state_machine=False):
    epic = tmp_path / "epic"
    (epic / "models").mkdir(parents=True)
    if traceability:
        (epic / "traceability.md").write_text(TRACE)
    if state_machine:
        (epic / "models" / "state-machines.json").write_text('{"states": {}}')
    return epic


def _green_verify(repo):
    return {"ok": True, "steps": [{"command": ["make", "test"], "ok": True}]}


def _red_verify(repo):
    return {"ok": False, "steps": [{"command": ["make", "test"], "ok": False}]}


def _no_scan(targets):
    return []


def _no_blocked(findings):
    return {}


def _which_none(_tool):
    return None


def _audit(tmp_path, **kw):
    epic = kw.pop("epic", None) or _epic(tmp_path)
    defaults = dict(
        scanner=_no_scan, blocked_fn=_no_blocked, verify_fn=_green_verify, which=_which_none
    )
    defaults.update(kw)
    return cea.run_close_epic_audit(epic, tmp_path, **defaults)


# --- formal models present/absent -------------------------------------------


def test_no_formal_models_skips_transition_audit(tmp_path):
    m = _audit(tmp_path)
    assert m.transitions is None
    assert any("no formal state-machine model" in w for w in m.warnings)


def test_formal_models_present_runs_transition_audit(tmp_path):
    epic = _epic(tmp_path, state_machine=True)
    m = _audit(tmp_path, epic=epic, transition_fn=lambda t, sm: {"summary": "5/5 covered"})
    assert m.transitions == {"summary": "5/5 covered"}


# --- unresolved findings ----------------------------------------------------


def test_unresolved_blocked_tickets_block_close(tmp_path):
    def scan(targets):
        class F:
            __dict__ = {"text": "TBD x", "tickets": ["#43"]}
        return [F()]

    m = _audit(tmp_path, scanner=scan, blocked_fn=lambda f: {"#43": 1})
    assert m.blocked_tickets == [43]
    assert m.blocked is True


# --- stitch findings --------------------------------------------------------


def test_stitch_findings_recorded(tmp_path):
    m = _audit(tmp_path, stitch_fn=lambda t: {"count": 3})
    assert m.stitch == {"count": 3}


# --- mutation tooling -------------------------------------------------------


def test_missing_mutation_tooling_warns(tmp_path):
    m = _audit(tmp_path)
    assert m.mutation_tools_available == []
    assert any("mutation-testing tool" in w for w in m.warnings)


def test_mutation_tool_detected(tmp_path):
    m = _audit(tmp_path, which=lambda t: "/usr/bin/mutmut" if t == "mutmut" else None)
    assert m.mutation_tools_available == ["mutmut"]


# --- stop condition ---------------------------------------------------------


def test_failed_integration_tests_block_close(tmp_path):
    m = _audit(tmp_path, verify_fn=_red_verify)
    assert m.integration_ok is False
    assert m.blocked is True


def test_green_with_no_blockers_is_ready(tmp_path):
    m = _audit(tmp_path)
    assert m.integration_ok is True
    assert m.blocked is False


def test_missing_verification_is_not_a_pass(tmp_path):
    def boom(repo):
        raise RuntimeError("no runner")

    m = _audit(tmp_path, verify_fn=boom)
    assert m.integration_ok is False
    assert m.blocked is True


# --- traceability + report --------------------------------------------------


def test_traceability_audit_included(tmp_path):
    m = _audit(tmp_path)
    assert m.traceability is not None
    assert m.traceability["total"] == 2


def test_malformed_traceability_does_not_abort_audit(tmp_path, monkeypatch):
    m_in = _epic(tmp_path)

    def boom(_text):
        raise ValueError("bad table")

    monkeypatch.setattr(cea.tr, "parse_matrix", boom)
    m = _audit(tmp_path, epic=m_in)
    assert any("traceability audit failed" in w for w in m.warnings)
    # The rest of the audit still ran.
    assert m.verification is not None


def test_mutation_probe_error_is_isolated(tmp_path):
    def boom_which(_tool):
        raise OSError("which exploded")

    m = _audit(tmp_path, which=boom_which)
    assert any("mutation-tool probe failed" in w for w in m.warnings)
    assert m.verification is not None


def test_render_markdown(tmp_path):
    m = _audit(tmp_path)
    md = m.render_markdown()
    assert "# Close-Epic Audit" in md
    assert "Traceability" in md
    assert "ready to close" in md


def test_manifest_serializable(tmp_path):
    m = _audit(tmp_path)
    data = json.loads(json.dumps(m.to_dict()))
    assert data["blocked"] is False
    assert "integration_ok" in data


# --- CLI --------------------------------------------------------------------


def test_cli_writes_manifest_and_report(tmp_path, monkeypatch, capsys):
    epic = _epic(tmp_path)
    out = tmp_path / "out"
    precomputed = _audit(tmp_path, epic=epic)  # build once, before patching
    monkeypatch.setattr(cea, "run_close_epic_audit", lambda *a, **k: precomputed)
    rc = cea.main([
        "--epic-dir", str(epic), "--target-repo", str(tmp_path), "--output-dir", str(out),
    ])
    assert rc == 0
    assert (out / "close-epic-manifest.json").exists()
    assert (out / "close-epic-report.md").exists()
