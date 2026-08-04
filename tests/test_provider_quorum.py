"""Tests for the parallel provider quorum runner (P0-4)."""

from __future__ import annotations

import json
from dataclasses import dataclass

import providers
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

    text_only = {"deepseek", "kimi", "glm", "qwen", "minimax"}
    for name in text_only:
        assert providers_by_name[name].reads_repo is False, name
    for name in ("codex", "gemini", "gemini-vertex", "claude", "claude-interactive", "opus"):
        assert providers_by_name[name].reads_repo is True, name

    for name in ("design_critic", "kill-review", "divergence"):
        assert roles[name].requires_repo_read is False, name
    for name in ("explorer", "implementer", "reviewer", "architecture_critic", "risky_diff_review"):
        assert roles[name].requires_repo_read is True, name

    # The two roles gemini-vertex is REQUIRED on both need repo reading —
    # exactly the roles chief-wiggum#319's diff-scoped retrieval targets.
    for role_name in ("reviewer", "risky_diff_review"):
        assert "gemini-vertex" in roles[role_name].required
        assert roles[role_name].requires_repo_read is True


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

    text_only = {"deepseek", "kimi", "glm", "qwen", "minimax"}
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
