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
