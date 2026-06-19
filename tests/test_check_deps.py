import check_deps


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
