from pathlib import Path

import env


def test_slugify_normalizes_epic_names():
    assert env.slugify("Epic: Order Lifecycle!") == "epic-order-lifecycle"
    assert env.slugify("  ---  ") == "epic"


def test_find_home_prefers_valid_environment_path(monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("CHIEF_WIGGUM_HOME", str(repo_root))

    assert env.find_home() == repo_root


def test_shell_quote_handles_single_quotes():
    assert env.shell_quote("owner's repo") == "'owner'\"'\"'s repo'"
