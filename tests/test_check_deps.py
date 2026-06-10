import check_deps


def test_base_profile_does_not_require_browser_use():
    assert not check_deps.is_required("pkgs", "browser-use", ["base"])


def test_implement_profile_requires_browser_validation_dependencies():
    workflows = ["implement"]

    assert check_deps.is_required("pkgs", "browser-use", workflows)
    assert check_deps.is_required("pkgs", "playwright", workflows)
    assert check_deps.is_required("pkgs", "langchain-anthropic", workflows)
    assert check_deps.is_required("secrets", "ANTHROPIC_API_KEY", workflows)


def test_vertex_profile_requires_vertex_packages_and_project():
    workflows = ["vertex"]

    assert check_deps.is_required("pkgs", "langchain-google-vertexai", workflows)
    assert check_deps.is_required("pkgs", "google-cloud-aiplatform", workflows)
    assert check_deps.is_required("secrets", "GOOGLE_CLOUD_PROJECT", workflows)
