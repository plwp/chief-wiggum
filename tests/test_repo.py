import pytest
import repo


def test_parse_owner_repo_accepts_issue_suffix():
    assert repo._parse_owner_repo("acme/widget-api#42") == ("acme", "widget-api")


@pytest.mark.parametrize(
    "value",
    [
        "acme",
        "acme/../repo",
        "acme/repo/extra",
        "acme/repo name",
        "/acme/repo",
    ],
)
def test_parse_owner_repo_rejects_unsafe_values(value):
    with pytest.raises(SystemExit):
        repo._parse_owner_repo(value)
