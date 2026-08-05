import time

import check_deps
import factory_log


def test_core_profile_does_not_require_ai_or_browser_tools():
    profiles = ["core"]

    assert check_deps.is_required("cmds", "gh", profiles)
    assert check_deps.is_required("cmds", "git", profiles)
    assert not check_deps.is_required("cmds", "claude", profiles)
    assert not check_deps.is_required("cmds", "codex", profiles)
    assert not check_deps.is_required("cmds", "gemini", profiles)
    assert not check_deps.is_required("pkgs", "browser-use", profiles)


def test_base_profile_is_backward_compatible_alias_for_core():
    assert check_deps.is_required("cmds", "gh", ["base"])
    assert not check_deps.is_required("cmds", "codex", ["base"])


def test_implement_profile_requires_browser_validation_dependencies():
    workflows = ["implement"]

    assert check_deps.is_required("pkgs", "browser-use", workflows)
    assert check_deps.is_required("pkgs", "playwright", workflows)
    assert check_deps.is_required("pkgs", "langchain-anthropic", workflows)
    assert check_deps.is_required("secrets", "ANTHROPIC_API_KEY", workflows)
    assert check_deps.is_required("cmds", "gh", workflows)
    assert not check_deps.is_required("cmds", "claude", workflows)


def test_provider_profiles_require_specific_cli_tools():
    assert check_deps.is_required("cmds", "codex", ["codex"])
    assert check_deps.is_required("cmds", "gemini", ["gemini"])
    assert check_deps.is_required("cmds", "claude", ["claude-code"])
    assert check_deps.is_required("cmds", "claude", ["claude-interactive"])
    assert check_deps.is_required("cmds", "tmux", ["claude-interactive"])


def test_selected_profiles_default_to_core_and_append_providers():
    assert check_deps.selected_profiles([], []) == ["core"]
    assert check_deps.selected_profiles(["transcription"], ["gemini"]) == ["transcription", "gemini"]


def test_vertex_profile_requires_vertex_packages_and_project():
    workflows = ["vertex"]

    assert check_deps.is_required("pkgs", "langchain-google-vertexai", workflows)
    assert check_deps.is_required("pkgs", "google-cloud-aiplatform", workflows)
    assert check_deps.is_required("secrets", "GOOGLE_CLOUD_PROJECT", workflows)


# --- profile recommendation (P2-16) -----------------------------------------

REVIEWER_CONFIG = {
    "roles": {
        "reviewer": {"required": ["codex", "gemini"], "optional": ["claude-interactive"]},
        "design_critic": {"required": ["gemini"], "optional": ["codex", "claude"]},
    }
}


def test_role_profiles_maps_providers_to_profiles():
    assert check_deps.role_profiles("reviewer", REVIEWER_CONFIG) == {
        "codex", "gemini", "claude-interactive"
    }


def test_role_profiles_maps_claude_and_vertex():
    config = {"roles": {"r": {"required": ["claude", "gemini-vertex"], "optional": []}}}
    assert check_deps.role_profiles("r", config) == {"claude-code", "vertex"}


def test_role_profiles_unknown_role_is_empty():
    assert check_deps.role_profiles("nope", REVIEWER_CONFIG) == set()


def test_recommend_for_implement_includes_browser_validation():
    assert "browser-validation" in check_deps.recommend_profiles(workflows=["implement"])
    assert "core" in check_deps.recommend_profiles(workflows=["implement"])


def test_recommend_strips_leading_slash():
    assert check_deps.recommend_profiles(workflows=["/transcribe"]) == ["transcription"]


def test_recommend_combines_workflow_and_role():
    profiles = check_deps.recommend_profiles(
        workflows=["architect"], roles=["reviewer"], config=REVIEWER_CONFIG
    )
    assert set(profiles) == {"core", "codex", "gemini", "claude-interactive"}


def test_recommend_defaults_to_core():
    assert check_deps.recommend_profiles() == ["core"]


def test_unknown_workflow_defaults_to_core():
    assert check_deps.recommend_profiles(workflows=["mystery"]) == ["core"]


def test_recommend_workflows_include_direct_provider_usage():
    # Workflows that call codex/gemini directly must surface those profiles.
    assert set(check_deps.recommend_profiles(workflows=["implement"])) >= {
        "core", "browser-validation", "codex", "gemini"
    }
    assert "gemini" in check_deps.recommend_profiles(workflows=["stitch-audit"])
    # /design runs Playwright for screenshots.
    assert "browser-validation" in check_deps.recommend_profiles(workflows=["design"])


def test_keep_going_workflow_is_mapped():
    assert check_deps.recommend_profiles(workflows=["keep-going"]) == ["core"]


def test_go_lsp_profile_requires_gopls():
    assert check_deps.is_required("cmds", "gopls", ["go-lsp"])
    assert check_deps.is_required("cmds", "go", ["go-lsp"])
    assert not check_deps.is_required("cmds", "gopls", ["core"])


def test_python_lsp_profile_requires_pyright():
    assert check_deps.is_required("cmds", "pyright-langserver", ["python-lsp"])


# --- language support matrix consumption (#162) -----------------------------


def test_language_tier1_profile_expands_to_built_languages_dep_profiles():
    """config/languages.json is the source of truth: go/python contribute
    go-lsp/python-lsp; language-tier-1 requires exactly their tools."""
    assert check_deps.is_required("cmds", "gopls", ["language-tier-1"])
    assert check_deps.is_required("cmds", "go", ["language-tier-1"])
    assert check_deps.is_required("cmds", "pyright-langserver", ["language-tier-1"])


def test_language_tier1_profile_is_listed():
    assert "language-tier-1" in check_deps.WORKFLOW_REQUIREMENTS


def test_list_languages_cli_prints_matrix(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["check_deps.py", "--list-languages"])
    check_deps.main()
    out = capsys.readouterr().out
    assert "go" in out and "python" in out and "typescript" in out and "rust" in out
    assert "designed" in out  # rust's tier/status shows up
    assert "docs/languages.md" in out


# --- telemetry capture profile + check_capture probe (chief-wiggum#345 AC1) --


def test_telemetry_profile_requires_capture_items():
    assert check_deps.is_required("capture", "factory-ledger", ["telemetry"])
    assert check_deps.is_required("capture", "claude-transcripts", ["telemetry"])


def test_implement_and_reflect_recommend_telemetry():
    assert "telemetry" in check_deps.recommend_profiles(workflows=["implement"])
    assert "telemetry" in check_deps.recommend_profiles(workflows=["reflect"])


def test_existing_profiles_still_resolve_the_new_capture_kind():
    """required_items's kind-lookup must not KeyError when a profile has no
    "capture" key -- only the new "telemetry" profile has one; every other
    profile must resolve to an empty set, not crash, once `required_items`
    (or every profile dict) accounts for the new "capture" kind."""
    for profile in check_deps.WORKFLOW_REQUIREMENTS:
        if profile == "telemetry":
            continue
        assert check_deps.required_items("capture", [profile]) == set()


def _reset_dep_counts():
    check_deps.pass_count = 0
    check_deps.fail_count = 0
    check_deps.warn_count = 0


def test_check_capture_absent_transcript_root_warns_never_fails(monkeypatch, tmp_path):
    """Absent on a non-Claude harness is INAPPLICABLE, not a failure -- a check
    that fails closed on a harness it doesn't apply to teaches operators to
    ignore it."""
    _reset_dep_counts()
    monkeypatch.setattr(factory_log, "DEFAULT_TRANSCRIPT_ROOT", tmp_path / "does-not-exist")
    check_deps.check_capture("claude-transcripts", required=True)
    assert check_deps.fail_count == 0
    assert check_deps.warn_count == 1


def test_check_capture_zero_claude_code_records_is_missing(monkeypatch, tmp_path, capsys):
    """An empty Claude layer is exactly the silent failure this checker
    exists to catch: everything installs, every workflow runs, and the
    ledger's Claude layer stays empty because no ingest ever happened.

    Must not depend on the real machine's ~/.claude/projects existing (true
    on a dev Mac, false on CI) -- point the probe at a fake-but-present
    transcript root, same hermetic discipline as the autouse CW_FACTORY_LOG
    isolation."""
    _reset_dep_counts()
    monkeypatch.setattr(factory_log, "DEFAULT_TRANSCRIPT_ROOT", tmp_path)
    check_deps.check_capture("factory-ledger", required=True)
    assert check_deps.fail_count == 1
    assert "ingest-claude-transcripts" in capsys.readouterr().out


def test_check_capture_factory_ledger_degrades_when_no_transcript_root(monkeypatch, tmp_path):
    """A harness with no ~/.claude/projects at all can never satisfy this
    probe by running the ingest -- it is INAPPLICABLE, not missing. Must
    downgrade to warn, never fail, the same way claude-transcripts already
    does (chief-wiggum#345 reviewer finding: factory-ledger was unsatisfiable
    on a non-Claude harness while its sibling correctly degraded)."""
    _reset_dep_counts()
    monkeypatch.setattr(factory_log, "DEFAULT_TRANSCRIPT_ROOT", tmp_path / "does-not-exist")
    check_deps.check_capture("factory-ledger", required=True)
    assert check_deps.fail_count == 0
    assert check_deps.warn_count == 1


def test_check_capture_factory_ledger_still_fails_when_root_present_but_never_ingested(
        monkeypatch, tmp_path):
    """Distinct from the inapplicable case above: a REAL Claude Code harness
    (transcript root present) with zero ingested records is still an
    actionable MISSING -- the operator can and should run the ingest."""
    _reset_dep_counts()
    monkeypatch.setattr(factory_log, "DEFAULT_TRANSCRIPT_ROOT", tmp_path)  # exists (tmp_path itself)
    check_deps.check_capture("factory-ledger", required=True)
    assert check_deps.fail_count == 1
    assert check_deps.warn_count == 0


def test_check_capture_recent_records_is_ok(monkeypatch, tmp_path, capsys):
    """Same hermetic requirement as the MISSING case above: point at a
    fake-but-present transcript root rather than relying on the real
    machine's ~/.claude/projects (absent on CI)."""
    _reset_dep_counts()
    monkeypatch.setattr(factory_log, "DEFAULT_TRANSCRIPT_ROOT", tmp_path)
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    factory_log._append({"ts": time.time(), "event": factory_log.CLAUDE_CODE,
                         "request_id": "r1", "tokens_in": 100, "tokens_out": 50})
    check_deps.check_capture("factory-ledger", required=True)
    assert check_deps.fail_count == 0
    assert "[OK]" in capsys.readouterr().out
