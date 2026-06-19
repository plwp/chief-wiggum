"""Tests for the parallel provider quorum runner (P0-4)."""

from __future__ import annotations

import json

import providers
from providers import Provider, Role, RolePlan, run_role_quorum, validate_output


def _provider(name: str) -> Provider:
    return Provider(name=name, type="tool", enabled=True, tool=name)


def _plan(required: list[str], optional: list[str]) -> RolePlan:
    role = Role(name="reviewer", required=tuple(required), optional=tuple(optional))
    return RolePlan(
        role=role,
        required=tuple(_provider(n) for n in required),
        optional=tuple(_provider(n) for n in optional),
        missing_required=(),
        skipped_optional=(),
    )


SUBSTANTIVE = "This is a substantive review with several findings to report."


# --- output validation ------------------------------------------------------


def test_validate_output_rejects_short_and_failure_markers():
    assert validate_output(None) == "no output"
    assert validate_output("tiny", min_bytes=20).startswith("output too short")
    assert "Timeout:" in validate_output("Timeout: provider did not respond in 600s")
    assert "Error:" in validate_output("Error: calling codex failed")
    assert validate_output(SUBSTANTIVE) is None


# --- required / optional semantics ------------------------------------------


def test_required_provider_failure_fails_quorum(tmp_path):
    def execute(provider):
        raise RuntimeError("boom")

    manifest = run_role_quorum(_plan(["codex"], []), execute, tmp_path)
    assert manifest.ok is False
    assert manifest.failed_required == ["codex"]
    assert manifest.results[0].status == "failed"
    assert "boom" in manifest.results[0].error


def test_optional_provider_failure_does_not_fail_quorum(tmp_path):
    def execute(provider):
        if provider.name == "gemini":
            raise RuntimeError("optional down")
        return SUBSTANTIVE

    manifest = run_role_quorum(_plan(["codex"], ["gemini"]), execute, tmp_path)
    assert manifest.ok is True
    statuses = {r.name: r.status for r in manifest.results}
    assert statuses == {"codex": "ok", "gemini": "failed"}


def test_retry_succeeds_on_second_attempt(tmp_path):
    calls = {"codex": 0}

    def execute(provider):
        calls[provider.name] += 1
        if calls[provider.name] < 2:
            raise RuntimeError("transient")
        return SUBSTANTIVE

    manifest = run_role_quorum(_plan(["codex"], []), execute, tmp_path, max_attempts=2)
    result = manifest.results[0]
    assert result.status == "ok"
    assert result.attempts == 2


def test_optional_provider_is_not_retried(tmp_path):
    calls = {"gemini": 0}

    def execute(provider):
        calls[provider.name] += 1
        raise RuntimeError("down")

    run_role_quorum(_plan(["codex"], ["gemini"]), lambda p: SUBSTANTIVE if p.name == "codex" else execute(p), tmp_path, max_attempts=3)
    assert calls["gemini"] == 1


def test_timeout_output_marker_is_treated_as_failure(tmp_path):
    def execute(provider):
        return "Timeout: codex did not respond within 600s"

    manifest = run_role_quorum(_plan(["codex"], []), execute, tmp_path, max_attempts=1)
    assert manifest.ok is False
    assert "failure marker" in manifest.results[0].error


def test_too_short_output_is_failure(tmp_path):
    manifest = run_role_quorum(_plan(["codex"], []), lambda p: "ok", tmp_path, min_bytes=50, max_attempts=1)
    assert manifest.ok is False
    assert "too short" in manifest.results[0].error


# --- manifest content + files -----------------------------------------------


def test_manifest_written_and_serializable(tmp_path):
    manifest = run_role_quorum(_plan(["codex"], ["gemini"]), lambda p: SUBSTANTIVE, tmp_path)
    manifest_path = tmp_path / "reviewer-manifest.json"
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text())
    assert data["role"] == "reviewer"
    assert data["ok"] is True
    assert {r["name"] for r in data["results"]} == {"codex", "gemini"}
    # Response files written per provider.
    assert (tmp_path / "reviewer-codex.md").read_text() == SUBSTANTIVE
    assert (tmp_path / "reviewer-gemini.md").read_text() == SUBSTANTIVE


def test_results_are_deterministically_ordered(tmp_path):
    plan = _plan(["codex", "gemini"], ["claude-interactive"])
    manifest = run_role_quorum(plan, lambda p: SUBSTANTIVE, tmp_path)
    assert [r.name for r in manifest.results] == ["codex", "gemini", "claude-interactive"]


def test_failure_clears_stale_success_file(tmp_path):
    # An earlier run left reviewer-codex.md; this run fails -> stale must be gone.
    (tmp_path / "reviewer-codex.md").write_text("stale success from a previous run")

    def execute(provider):
        raise RuntimeError("down")

    manifest = run_role_quorum(_plan(["codex"], []), execute, tmp_path, max_attempts=1)
    assert not (tmp_path / "reviewer-codex.md").exists()
    assert (tmp_path / "reviewer-codex.error.md").exists()
    assert manifest.results[0].error_path == str(tmp_path / "reviewer-codex.error.md")


def test_success_clears_stale_error_file(tmp_path):
    (tmp_path / "reviewer-codex.error.md").write_text("stale error")
    manifest = run_role_quorum(_plan(["codex"], []), lambda p: SUBSTANTIVE, tmp_path)
    assert not (tmp_path / "reviewer-codex.error.md").exists()
    assert manifest.results[0].error_path is None


def test_duplicate_provider_across_required_and_optional_runs_once(tmp_path):
    plan = _plan(["codex"], ["codex"])  # overlap
    runs: list[str] = []
    run_role_quorum(plan, lambda p: runs.append(p.name) or SUBSTANTIVE, tmp_path)
    assert runs.count("codex") == 1


def test_validate_config_rejects_duplicate_role_references():
    config = {
        "providers": {"codex": {"type": "tool", "tool": "codex"}},
        "roles": {"reviewer": {"required": ["codex"], "optional": ["codex"]}},
    }
    errors = providers.validate_config(config)
    assert any("more than once" in e for e in errors)


def test_disabled_provider_absent_from_plan_is_not_run(tmp_path):
    # A disabled optional never enters the plan, so it is never executed.
    config = {
        "providers": {
            "codex": {"type": "tool", "tool": "codex", "enabled": True},
            "gemini": {"type": "tool", "tool": "gemini", "enabled": False},
        },
        "roles": {"reviewer": {"required": ["codex"], "optional": ["gemini"]}},
    }
    plan = providers.plan_role("reviewer", config)
    assert "gemini" in plan.skipped_optional

    ran: list[str] = []

    def execute(provider):
        ran.append(provider.name)
        return SUBSTANTIVE

    manifest = run_role_quorum(plan, execute, tmp_path)
    assert ran == ["codex"]
    assert manifest.ok is True
