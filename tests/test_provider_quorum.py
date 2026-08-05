"""Tests for the parallel provider quorum runner (P0-4)."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass

import providers
import pytest
from providers import (
    BLIND_PROVIDER_MARGIN,
    BlindnessReport,
    ImageBlindnessReport,
    Provider,
    ProviderResult,
    Role,
    RolePlan,
    detect_blind_providers,
    detect_image_blind_providers,
    estimate_prompt_tokens,
    run_role_quorum,
    validate_output,
)


def _provider(name: str, *, reads_repo: bool = True, accepts_images: bool = True) -> Provider:
    return Provider(
        name=name, type="tool", enabled=True, tool=name,
        reads_repo=reads_repo, accepts_images=accepts_images,
    )


def _plan(required: list[str], optional: list[str], *, requires_repo_read: bool = True) -> RolePlan:
    role = Role(
        name="reviewer", required=tuple(required), optional=tuple(optional),
        requires_repo_read=requires_repo_read,
    )
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


# --- usage threading (chief-wiggum#319) --------------------------------------


@dataclass
class _FakeUsage:
    """Duck-typed stand-in for consult_ai.Usage — providers.py never imports
    it, so a minimal object with the same attributes proves the threading
    works without pulling consult_ai into this test module."""

    tokens_in: int | None = None
    tokens_out: int | None = None
    usage_status: str = "provider-json"
    resolved_model: str | None = None


def test_execute_returning_usage_pair_populates_provider_result(tmp_path):
    def execute(provider):
        return SUBSTANTIVE, _FakeUsage(tokens_in=500_000, tokens_out=200, resolved_model="gpt-5.5")

    manifest = run_role_quorum(_plan(["codex"], []), execute, tmp_path)
    result = manifest.results[0]
    assert result.tokens_in == 500_000
    assert result.tokens_out == 200
    assert result.usage_status == "provider-json"
    assert result.resolved_model == "gpt-5.5"


def test_execute_returning_bare_string_leaves_usage_fields_none(tmp_path):
    # Back-compat: every pre-#319 execute callable (and every test above)
    # returns a bare string — must keep working, with usage simply absent.
    manifest = run_role_quorum(_plan(["codex"], []), lambda p: SUBSTANTIVE, tmp_path)
    result = manifest.results[0]
    assert result.tokens_in is None
    assert result.usage_status is None


def test_manifest_serializes_usage_fields(tmp_path):
    def execute(provider):
        return SUBSTANTIVE, _FakeUsage(tokens_in=1234, tokens_out=56, resolved_model="m")

    run_role_quorum(_plan(["codex"], []), execute, tmp_path)
    data = json.loads((tmp_path / "reviewer-manifest.json").read_text())
    assert data["results"][0]["tokens_in"] == 1234
    assert data["results"][0]["resolved_model"] == "m"


# --- blindness detection (chief-wiggum#319) ----------------------------------


def test_estimate_prompt_tokens_is_coarse_chars_over_four():
    assert estimate_prompt_tokens("a" * 400) == 100
    assert estimate_prompt_tokens("") == 1  # never zero — avoids a divide-by-zero downstream


def _result(name, *, tokens_in=None, status="ok", usage_status=None, required=True):
    return ProviderResult(
        name=name, required=required, status=status, tokens_in=tokens_in, usage_status=usage_status,
    )


def test_detect_blind_providers_flags_tokens_in_near_prompt_size():
    role = Role(name="reviewer", required=("gemini-vertex",), optional=(), requires_repo_read=True)
    providers_by_name = {"gemini-vertex": _provider("gemini-vertex")}
    results = [_result("gemini-vertex", tokens_in=1400, usage_status="sdk-metadata")]

    report = detect_blind_providers(role, providers_by_name, results, {"gemini-vertex": 1300})

    assert report.outcome == "findings"
    assert report.applicability == "applicable"
    assert len(report.findings) == 1
    assert report.findings[0].kind == "blind"
    assert report.findings[0].provider == "gemini-vertex"
    assert "did not read the repo" in report.findings[0].message


def test_detect_blind_providers_passes_when_tokens_in_far_exceeds_prompt():
    # The ticket's own evidence shape: codex ingests ~1M tokens against a
    # ~1.3k-token prompt — nowhere near the margin, a clean pass.
    role = Role(name="explorer", required=("codex",), optional=(), requires_repo_read=True)
    providers_by_name = {"codex": _provider("codex")}
    results = [_result("codex", tokens_in=1_000_000, usage_status="provider-json")]

    report = detect_blind_providers(role, providers_by_name, results, {"codex": 1300})

    assert report.outcome == "pass"
    assert report.findings == []
    assert report.providers_checked == 1


def test_detect_blind_providers_unmeasured_is_a_finding_not_a_pass():
    role = Role(name="reviewer", required=("gemini-vertex",), optional=(), requires_repo_read=True)
    providers_by_name = {"gemini-vertex": _provider("gemini-vertex")}
    results = [_result("gemini-vertex", tokens_in=None, usage_status="unavailable")]

    report = detect_blind_providers(role, providers_by_name, results, {"gemini-vertex": 1300})

    assert report.outcome == "findings"
    assert report.findings[0].kind == "unmeasured"
    assert "not the same as one measured and fine" in report.findings[0].message


def test_detect_blind_providers_already_surfaces_claude_interactives_invisible_cost():
    """chief-wiggum#331 item 3: the claude-interactive delegate has no
    usage-bearing transport (consult_ai.consult_claude_interactive ALWAYS
    returns usage_status='unavailable', per ADR-fh-05) — so whenever it
    succeeds inside a role that requires repo reading, detect_blind_providers
    (#319) already reports it as an "unmeasured" finding, not a quiet pass.
    This is #319's existing "unmeasured is a finding, not a pass" rule
    (see test_detect_blind_providers_unmeasured_is_a_finding_not_a_pass just
    above) applied to the SPECIFIC provider #331 is about — pinned here so a
    future change can't silently narrow that check to exclude the delegate."""
    role = Role(name="reviewer", required=("codex",), optional=("claude-interactive",), requires_repo_read=True)
    providers_by_name = {
        "codex": _provider("codex"),
        "claude-interactive": _provider("claude-interactive"),  # reads_repo=True (config default)
    }
    results = [
        _result("codex", tokens_in=900_000, usage_status="provider-json"),
        _result("claude-interactive", tokens_in=None, usage_status="unavailable", required=False),
    ]

    report = detect_blind_providers(
        role, providers_by_name, results,
        {"codex": 1300, "claude-interactive": 1300},
    )

    assert report.outcome == "findings"
    finding = next(f for f in report.findings if f.provider == "claude-interactive")
    assert finding.kind == "unmeasured"
    assert "cannot be measured is not the same as one measured and fine" in finding.message


def test_detect_blind_providers_inapplicable_when_role_does_not_require_repo_read():
    role = Role(name="design_critic", required=("gemini-vertex",), optional=(), requires_repo_read=False)
    providers_by_name = {"gemini-vertex": _provider("gemini-vertex")}
    # Even a textbook-blind measurement must not surface as a finding — this
    # role was never asked to read the repo.
    results = [_result("gemini-vertex", tokens_in=100, usage_status="sdk-metadata")]

    report = detect_blind_providers(role, providers_by_name, results, {"gemini-vertex": 1300})

    assert report.applicability == "inapplicable"
    assert report.outcome == "inapplicable"
    assert report.findings == []


def test_detect_blind_providers_skips_declared_text_only_providers():
    # A provider that already told the config it can't read files is not a
    # silent surprise when it answers from the prompt alone.
    role = Role(name="some-role", required=("deepseek",), optional=(), requires_repo_read=True)
    providers_by_name = {"deepseek": _provider("deepseek", reads_repo=False)}
    results = [_result("deepseek", tokens_in=50, usage_status="provider-json")]

    report = detect_blind_providers(role, providers_by_name, results, {"deepseek": 1300})

    assert report.outcome == "pass"
    assert report.providers_checked == 0


def test_detect_blind_providers_skips_failed_providers():
    # A failed provider is already visible as a quorum failure — don't double
    # report it as blind on top.
    role = Role(name="reviewer", required=("gemini-vertex",), optional=(), requires_repo_read=True)
    providers_by_name = {"gemini-vertex": _provider("gemini-vertex")}
    results = [_result("gemini-vertex", tokens_in=None, status="failed", usage_status=None)]

    report = detect_blind_providers(role, providers_by_name, results, {"gemini-vertex": 1300})

    assert report.findings == []


def test_blindness_report_serializes_the_four_state_outcome():
    report = BlindnessReport(role="reviewer", requires_repo_read=False)
    d = report.to_dict()
    assert d["applicability"] == "inapplicable"
    assert d["outcome"] == "inapplicable"
    assert d["findings"] == []


def test_blind_provider_margin_is_generous_but_not_unbounded():
    # A provider whose tokens_in is exactly at the margin boundary is still
    # "blind"; comfortably past it is a pass. Documents the constant's intent
    # rather than pinning an exact magic number elsewhere.
    assert BLIND_PROVIDER_MARGIN < 5  # nowhere near the ticket's 165:1 gap
    role = Role(name="reviewer", required=("gemini-vertex",), optional=(), requires_repo_read=True)
    providers_by_name = {"gemini-vertex": _provider("gemini-vertex")}
    at_boundary = [_result("gemini-vertex", tokens_in=int(1000 * BLIND_PROVIDER_MARGIN), usage_status="sdk-metadata")]
    past_boundary = [_result("gemini-vertex", tokens_in=int(1000 * BLIND_PROVIDER_MARGIN) + 1000, usage_status="sdk-metadata")]

    assert detect_blind_providers(role, providers_by_name, at_boundary, {"gemini-vertex": 1000}).outcome == "findings"
    assert detect_blind_providers(role, providers_by_name, past_boundary, {"gemini-vertex": 1000}).outcome == "pass"


def test_run_role_quorum_computes_blindness_when_prompt_is_supplied(tmp_path):
    role_prompt = "x" * 4000  # estimate_prompt_tokens -> 1000

    def execute(provider):
        if provider.name == "gemini-vertex":
            return SUBSTANTIVE, _FakeUsage(tokens_in=1000, usage_status="sdk-metadata")
        return SUBSTANTIVE, _FakeUsage(tokens_in=900_000, usage_status="provider-json")

    plan = _plan(["codex", "gemini-vertex"], [])
    manifest = run_role_quorum(plan, execute, tmp_path, prompt=role_prompt)

    assert manifest.blindness is not None
    assert manifest.blindness.outcome == "findings"
    assert {f.provider for f in manifest.blindness.findings} == {"gemini-vertex"}
    # blindness never fails the quorum on its own — it's a report, not a gate.
    assert manifest.ok is True

    data = json.loads((tmp_path / "reviewer-manifest.json").read_text())
    assert data["blindness"]["outcome"] == "findings"


def test_run_role_quorum_blindness_stays_none_without_prompt(tmp_path):
    # A caller that doesn't pass ``prompt`` gets the pre-#319 manifest shape
    # exactly — "didn't ask" must never look identical to "asked and clean".
    manifest = run_role_quorum(_plan(["codex"], []), lambda p: SUBSTANTIVE, tmp_path)
    assert manifest.blindness is None
    data = json.loads((tmp_path / "reviewer-manifest.json").read_text())
    assert "blindness" not in data


def test_shipped_config_declares_reads_repo_and_requires_repo_read():
    from pathlib import Path as _Path

    config = providers.load_config(_Path(__file__).resolve().parents[1] / "config" / "providers.json")
    providers_by_name = providers.providers_from_config(config)
    roles = providers.roles_from_config(config)

    text_only = {"deepseek", "deepseek-flash", "kimi", "glm", "qwen", "minimax"}
    for name in text_only:
        assert providers_by_name[name].reads_repo is False, name
    for name in ("codex", "gemini", "gemini-vertex", "claude", "claude-interactive", "opus"):
        assert providers_by_name[name].reads_repo is True, name

    for name in ("design_critic", "kill-review", "divergence"):
        assert roles[name].requires_repo_read is False, name
    for name in ("explorer", "implementer", "reviewer", "architecture_critic", "risky_diff_review"):
        assert roles[name].requires_repo_read is True, name

    # The two roles deepseek-flash is REQUIRED on both need repo reading.
    # deepseek-flash honestly declares reads_repo=False (openrouter call
    # path, no filesystem), so workflows must inline the diff for it —
    # same contract gemini-vertex had before the swap, minus its bespoke
    # retrieval. gemini-vertex remains required only on design_critic,
    # the one role that sends screenshots (openrouter providers are
    # accepts_images=False, so they cannot fill that seat).
    for role_name in ("reviewer", "risky_diff_review"):
        assert "deepseek-flash" in roles[role_name].required
        assert roles[role_name].requires_repo_read is True
    assert "gemini-vertex" in roles["design_critic"].required


def test_shipped_config_declares_needs_inline_diff_correctly():
    # chief-wiggum#332: only a provider with NO real agentic tool loop
    # (gemini-vertex's single synchronous SDK call; every openrouter-backed
    # provider) needs the diff spoon-fed as inline text — everything with
    # real filesystem access via cwd can be pointed at the diff file instead.
    from pathlib import Path as _Path

    config = providers.load_config(_Path(__file__).resolve().parents[1] / "config" / "providers.json")
    providers_by_name = providers.providers_from_config(config)

    needs_inline = {"gemini-vertex", "deepseek", "deepseek-flash", "kimi", "glm", "qwen", "minimax"}
    for name in needs_inline:
        assert providers_by_name[name].needs_inline_diff is True, name
    for name in ("codex", "gemini", "claude", "claude-interactive", "opus"):
        assert providers_by_name[name].needs_inline_diff is False, name


# --- chief-wiggum#321: image-shaped blindness detection ---------------------


def test_detect_image_blind_providers_inapplicable_when_role_never_sends_images():
    role = Role(name="reviewer", required=("codex",), optional=(), sends_images=False)
    providers_by_name = {"codex": _provider("codex")}

    report = detect_image_blind_providers(role, providers_by_name)

    assert report.outcome == "inapplicable"
    assert report.findings == []


def test_detect_image_blind_providers_flags_a_required_provider_that_cannot_accept_images():
    # Reproduces #321's exact defect: design_critic's ONLY required provider
    # is declared unable to receive images.
    role = Role(
        name="design_critic", required=("gemini-vertex",),
        optional=("codex",), sends_images=True,
    )
    providers_by_name = {
        "gemini-vertex": _provider("gemini-vertex", accepts_images=False),
        "codex": _provider("codex", accepts_images=True),
    }

    report = detect_image_blind_providers(role, providers_by_name)

    assert report.outcome == "findings"
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.provider == "gemini-vertex"
    assert finding.required is True
    assert "gemini-vertex" in finding.message
    assert "design_critic" in finding.message


def test_detect_image_blind_providers_flags_an_optional_provider_too():
    role = Role(
        name="design_critic", required=("gemini-vertex",),
        optional=("deepseek",), sends_images=True,
    )
    providers_by_name = {
        "gemini-vertex": _provider("gemini-vertex", accepts_images=True),
        "deepseek": _provider("deepseek", accepts_images=False),
    }

    report = detect_image_blind_providers(role, providers_by_name)

    assert report.outcome == "findings"
    assert report.findings[0].provider == "deepseek"
    assert report.findings[0].required is False


def test_detect_image_blind_providers_passes_when_every_provider_accepts_images():
    role = Role(
        name="design_critic", required=("gemini-vertex",),
        optional=("codex",), sends_images=True,
    )
    providers_by_name = {
        "gemini-vertex": _provider("gemini-vertex", accepts_images=True),
        "codex": _provider("codex", accepts_images=True),
    }

    report = detect_image_blind_providers(role, providers_by_name)

    assert report.outcome == "pass"
    assert report.findings == []
    assert report.providers_checked == 2


def test_image_blindness_report_serializes_the_four_state_outcome():
    report = ImageBlindnessReport(role="reviewer", sends_images=False)
    d = report.to_dict()
    assert d["applicability"] == "inapplicable"
    assert d["outcome"] == "inapplicable"
    assert d["findings"] == []


def test_run_role_quorum_computes_image_blindness_without_needing_a_prompt(tmp_path):
    # Unlike ``blindness`` (needs ``prompt`` for the token-floor estimate),
    # image_blindness is a static declaration check — always populated.
    role = Role(
        name="design_critic", required=("gemini-vertex",),
        optional=(), sends_images=True,
    )
    plan = RolePlan(
        role=role,
        required=(_provider("gemini-vertex", accepts_images=False),),
        optional=(),
        missing_required=(),
        skipped_optional=(),
    )

    manifest = run_role_quorum(plan, lambda p: SUBSTANTIVE, tmp_path)

    assert manifest.blindness is None  # no prompt was passed
    assert manifest.image_blindness is not None
    assert manifest.image_blindness.outcome == "findings"
    assert manifest.image_blindness.findings[0].provider == "gemini-vertex"

    data = json.loads((tmp_path / "design_critic-manifest.json").read_text())
    assert data["image_blindness"]["outcome"] == "findings"
    assert "blindness" not in data


def test_run_role_quorum_image_blindness_inapplicable_for_a_non_image_role(tmp_path):
    manifest = run_role_quorum(_plan(["codex"], []), lambda p: SUBSTANTIVE, tmp_path)
    assert manifest.image_blindness is not None
    assert manifest.image_blindness.outcome == "inapplicable"


def test_shipped_config_declares_accepts_images_and_sends_images():
    from pathlib import Path as _Path

    config = providers.load_config(_Path(__file__).resolve().parents[1] / "config" / "providers.json")
    providers_by_name = providers.providers_from_config(config)
    roles = providers.roles_from_config(config)

    text_only = {"deepseek", "deepseek-flash", "kimi", "glm", "qwen", "minimax"}
    for name in text_only:
        assert providers_by_name[name].accepts_images is False, name
    for name in ("codex", "gemini", "gemini-vertex", "claude", "claude-interactive", "opus"):
        assert providers_by_name[name].accepts_images is True, name

    assert roles["design_critic"].sends_images is True
    for name in roles:
        if name != "design_critic":
            assert roles[name].sends_images is False, name

    # The defect #321 fixes: design_critic's sole required provider must
    # accept images now that it declares sends_images=True.
    for name in roles["design_critic"].required:
        assert providers_by_name[name].accepts_images is True, name

    # And the structural check must actually pass on the shipped config —
    # not just "the fields exist", but "the fields are consistent".
    report = detect_image_blind_providers(roles["design_critic"], providers_by_name)
    assert report.outcome == "pass"


# --- quorum-level deadline (chief-wiggum#330) --------------------------------
#
# run_role_quorum's as_completed() used to have no timeout=, so a provider
# call that hangs (ignoring its own individual budget — a bug elsewhere, or
# simply a caller that never bounded it) could block the whole quorum call
# forever. quorum_timeout bounds the CALLER's wait: any future still
# unfinished at the deadline is reported as a failure rather than awaited.


def test_run_role_quorum_abandons_a_hung_provider_at_the_quorum_deadline(tmp_path):
    never = threading.Event()

    def execute(provider):
        if provider.name == "codex":
            return SUBSTANTIVE
        never.wait(30)  # simulates a provider call that ignores its own timeout
        return SUBSTANTIVE

    plan = _plan(["codex", "gemini-vertex"], [])
    start = time.monotonic()
    manifest = run_role_quorum(plan, execute, tmp_path, quorum_timeout=0.3, max_attempts=1)
    elapsed = time.monotonic() - start

    assert elapsed < 5, f"run_role_quorum did not return promptly ({elapsed}s)"
    statuses = {r.name: r.status for r in manifest.results}
    assert statuses["codex"] == "ok"
    assert statuses["gemini-vertex"] == "failed"
    gv_result = next(r for r in manifest.results if r.name == "gemini-vertex")
    assert "deadline" in gv_result.error
    never.set()  # let the background thread unblock so it doesn't linger


def test_run_role_quorum_default_deadline_never_fires_on_ordinary_fast_execution(tmp_path):
    # The default quorum_timeout is deliberately generous — every existing
    # (fast, mocked) caller must be completely unaffected by its existence.
    manifest = run_role_quorum(_plan(["codex"], ["gemini"]), lambda p: SUBSTANTIVE, tmp_path)
    assert manifest.ok is True
    assert all(r.status == "ok" for r in manifest.results)


def test_run_role_quorum_quorum_timeout_none_means_unbounded(tmp_path):
    # An explicit opt-out must still behave exactly like the pre-#330 code —
    # no artificial cap when a caller asks for none.
    manifest = run_role_quorum(
        _plan(["codex"], []), lambda p: SUBSTANTIVE, tmp_path, quorum_timeout=None,
    )
    assert manifest.ok is True


# --- classified, backed-off retries (chief-wiggum#330) -----------------------


def test_retry_backs_off_between_attempts(tmp_path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: sleeps.append(s))
    calls = {"codex": 0}

    def execute(provider):
        calls[provider.name] += 1
        if calls[provider.name] < 2:
            raise RuntimeError("transient")
        return SUBSTANTIVE

    manifest = run_role_quorum(_plan(["codex"], []), execute, tmp_path, max_attempts=2)

    assert manifest.results[0].status == "ok"
    assert len(sleeps) == 1  # one backoff, before the 2nd (successful) attempt
    assert sleeps[0] > 0


def test_retry_backoff_is_longer_after_a_rate_limit_than_a_plain_error(tmp_path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def execute(provider):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("HTTP 429: rate limit exceeded")
        return SUBSTANTIVE

    run_role_quorum(_plan(["codex"], []), execute, tmp_path, max_attempts=2)
    rate_limited_backoff = sleeps[0]

    sleeps.clear()
    calls["n"] = 0

    def execute_plain(provider):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection reset")
        return SUBSTANTIVE

    run_role_quorum(_plan(["codex"], []), execute_plain, tmp_path, max_attempts=2)
    plain_backoff = sleeps[0]

    assert rate_limited_backoff > plain_backoff


def test_classify_failure_recognizes_timeout_and_rate_limit_and_other():
    assert providers.classify_failure(TimeoutError("x")) == "timeout"
    assert providers.classify_failure(RuntimeError("HTTP 429 too many requests")) == "rate_limit"
    assert providers.classify_failure(RuntimeError("connection reset by peer")) == "other"


def test_classify_failure_recognizes_subprocess_timeout_expired():
    import subprocess

    exc = subprocess.TimeoutExpired(cmd=["x"], timeout=5)
    assert providers.classify_failure(exc) == "timeout"


def test_execute_opted_into_retry_context_receives_attempt_and_failure_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    received: list[tuple[int, str | None]] = []

    def execute(provider, attempt=1, previous_failure_kind=None):
        received.append((attempt, previous_failure_kind))
        if attempt == 1:
            raise TimeoutError("slow")
        return SUBSTANTIVE

    manifest = run_role_quorum(_plan(["codex"], []), execute, tmp_path, max_attempts=2)

    assert manifest.results[0].status == "ok"
    assert received == [(1, None), (2, "timeout")]


def test_execute_without_retry_context_is_unaffected(tmp_path, monkeypatch):
    # Backward compatibility: a plain 1-arg execute (every existing caller
    # and every other test in this file) must keep being called exactly as
    # before — no attempt/previous_failure_kind ever forced onto it.
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    calls = {"codex": 0}

    def execute(provider):
        calls[provider.name] += 1
        if calls[provider.name] < 2:
            raise RuntimeError("transient")
        return SUBSTANTIVE

    manifest = run_role_quorum(_plan(["codex"], []), execute, tmp_path, max_attempts=2)
    assert manifest.results[0].status == "ok"
    assert calls["codex"] == 2


def test_execute_accepting_var_keyword_args_is_treated_as_retry_context_aware(tmp_path, monkeypatch):
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    received = []

    def execute(provider, **kwargs):
        received.append(kwargs)
        return SUBSTANTIVE

    run_role_quorum(_plan(["codex"], []), execute, tmp_path, max_attempts=1)
    assert received[0].get("attempt") == 1


# --- MIN_PROMPT_BYTES enforced inside run_role_quorum (chief-wiggum#330) -----
#
# consult_ai.py's own CLI already refused a truncated/empty prompt before
# calling plan_role/run_role_quorum at all — but that guard only protects
# ITS entry path. scripts/run_review.py builds its own (much larger,
# assembled) prompt and calls providers.run_role_quorum directly, with no
# equivalent guard — so a truncated assembled prompt there could still burn
# a whole reviewer quorum's worth of provider calls (the #163 failure this
# generalizes). Moving the floor into run_role_quorum covers every entry
# path for free, and refuses BEFORE any provider task is submitted.


def test_run_role_quorum_refuses_a_too_short_prompt_before_any_provider_runs(tmp_path):
    called = []

    def execute(provider):
        called.append(provider.name)
        return SUBSTANTIVE

    with pytest.raises(providers.ShortPromptError):
        run_role_quorum(_plan(["codex"], []), execute, tmp_path, prompt="short")

    assert called == []  # refused before any provider was ever invoked


def test_run_role_quorum_accepts_a_prompt_at_the_floor(tmp_path):
    prompt = "x" * providers.MIN_PROMPT_BYTES
    manifest = run_role_quorum(_plan(["codex"], []), lambda p: SUBSTANTIVE, tmp_path, prompt=prompt)
    assert manifest.ok is True


def test_run_role_quorum_without_a_prompt_is_unaffected_by_the_floor(tmp_path):
    # prompt=None means "the caller didn't ask" (pre-existing semantics for
    # the blindness check too) — no floor to enforce.
    manifest = run_role_quorum(_plan(["codex"], []), lambda p: SUBSTANTIVE, tmp_path)
    assert manifest.ok is True


# --- per-provider prompt bodies (chief-wiggum#332: diff inlining) -----------
#
# run_role_quorum's `prompt` param may now be a dict keyed by provider name
# (in addition to a single shared string) — this is how review.py sends a
# SMALLER, pointer-shaped prompt to a filesystem-capable provider (codex,
# claude-interactive) and the full inline diff only to gemini-vertex,
# without breaking the MIN_PROMPT_BYTES floor or the #319 blindness
# token-floor estimate, both of which must key off the RIGHT variant per
# provider, not a single shared one.


def test_run_role_quorum_accepts_a_dict_prompt_for_the_floor_check(tmp_path):
    prompt = {
        "codex": "x" * providers.MIN_PROMPT_BYTES,
        "gemini-vertex": "y" * (providers.MIN_PROMPT_BYTES + 500),
    }
    manifest = run_role_quorum(_plan(["codex", "gemini-vertex"], []), lambda p: SUBSTANTIVE, tmp_path, prompt=prompt)
    assert manifest.ok is True


def test_run_role_quorum_dict_prompt_refuses_when_any_variant_is_too_short(tmp_path):
    prompt = {"codex": "x" * providers.MIN_PROMPT_BYTES, "gemini-vertex": "too short"}
    with pytest.raises(providers.ShortPromptError):
        run_role_quorum(_plan(["codex", "gemini-vertex"], []), lambda p: SUBSTANTIVE, tmp_path, prompt=prompt)


def test_run_role_quorum_dict_prompt_computes_per_provider_blindness_estimate(tmp_path):
    # codex's own prompt is small (a pointer, chief-wiggum#332) so its
    # measured tokens_in easily clears the blindness margin; gemini-vertex's
    # own prompt is the full inline diff, so its blindness estimate must be
    # based on ITS OWN (larger) prompt, not codex's smaller one — otherwise
    # gemini-vertex could be falsely flagged blind against too low a bar,
    # or codex could dodge a real blind reading against too high a bar.
    small_pointer = "p" * (providers.MIN_PROMPT_BYTES + 10)   # ~52 tokens
    large_inline = "d" * 20_000                                # ~5000 tokens
    prompt = {"codex": small_pointer, "gemini-vertex": large_inline}

    def execute(provider):
        if provider.name == "codex":
            # tokens_in far exceeds codex's OWN (small) prompt -> clearly read the repo.
            return SUBSTANTIVE, _FakeUsage(tokens_in=900_000, usage_status="provider-json")
        # tokens_in is close to gemini-vertex's OWN (large) prompt size -> blind.
        return SUBSTANTIVE, _FakeUsage(tokens_in=5_050, usage_status="sdk-metadata")

    plan = _plan(["codex", "gemini-vertex"], [])
    manifest = run_role_quorum(plan, execute, tmp_path, prompt=prompt)

    assert manifest.blindness is not None
    assert manifest.blindness.outcome == "findings"
    assert {f.provider for f in manifest.blindness.findings} == {"gemini-vertex"}
