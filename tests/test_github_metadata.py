"""Tests for the GitHub issue/milestone/dependency metadata client (P0-2)."""

from __future__ import annotations

import json
import subprocess

import pytest
from chief_wiggum import github

# --- dependency block parsing -----------------------------------------------

VALID_BLOCK = """\
Goal: ship the thing.

<!-- DEPENDENCIES
#42: []
#43: [#42]
#44: [#43, #42]
-->
"""


def test_parse_valid_dependency_block():
    meta = github.parse_dependency_block(VALID_BLOCK)
    assert meta.edges == {42: [], 43: [42], 44: [43, 42]}
    assert meta.has_block is True
    assert meta.warnings == []


def test_parse_missing_block_warns_not_raises():
    meta = github.parse_dependency_block("Just a goal, no block.")
    assert meta.edges == {}
    assert meta.has_block is False
    assert any("missing DEPENDENCIES block" in w for w in meta.warnings)


def test_parse_empty_description():
    meta = github.parse_dependency_block("")
    assert meta.edges == {}
    assert meta.has_block is False


def test_parse_malformed_lines_are_skipped_with_warning():
    block = """\
<!-- DEPENDENCIES
#42: []
this is not valid
#43: [#42]
#44: not-a-list
-->
"""
    meta = github.parse_dependency_block(block)
    assert meta.edges == {42: [], 43: [42]}
    assert sum("malformed dependency line" in w for w in meta.warnings) == 2


def test_parse_tolerates_missing_hash_and_whitespace():
    block = "<!-- DEPENDENCIES\n  42 : [ 41 , 40 ]\n-->"
    meta = github.parse_dependency_block(block)
    assert meta.edges == {42: [41, 40]}


def test_parse_warns_on_duplicate_and_self_dependency():
    block = "<!-- DEPENDENCIES\n#42: [#42]\n#42: [#41]\n-->"
    meta = github.parse_dependency_block(block)
    assert any("itself" in w for w in meta.warnings)
    assert any("duplicate" in w for w in meta.warnings)
    # self-edge stripped
    assert 42 not in meta.edges[42]


def test_parse_is_case_insensitive_on_marker():
    block = "<!-- dependencies\n#1: []\n-->"
    meta = github.parse_dependency_block(block)
    assert meta.edges == {1: []}


def test_format_dependency_block_roundtrips_through_parser():
    edges = {44: [43, 42], 42: [], 43: [42]}
    block = github.format_dependency_block(edges)
    # Stable, sorted output.
    assert block.splitlines()[1] == "#42: []"
    meta = github.parse_dependency_block(block)
    assert meta.edges == {42: [], 43: [42], 44: [42, 43]}
    assert meta.warnings == []


def test_format_dependency_block_dedupes_and_sorts_deps():
    block = github.format_dependency_block({1: [3, 2, 2]})
    assert "#1: [#2, #3]" in block


# --- JSON normalization -----------------------------------------------------


def test_issue_from_json_normalizes_labels_and_milestone():
    data = {
        "number": "42",
        "title": "Do it",
        "state": "open",
        "labels": [{"name": "bug"}, {"name": "p1"}],
        "milestone": {"title": "Epic: Name"},
        "body": "details",
    }
    issue = github.issue_from_json(data)
    assert issue.number == 42
    assert issue.state == "OPEN"
    assert issue.labels == ("bug", "p1")
    assert issue.milestone == "Epic: Name"


def test_issue_from_json_handles_null_milestone_and_body():
    issue = github.issue_from_json({"number": 7, "title": "x", "milestone": None, "body": None})
    assert issue.milestone is None
    assert issue.body == ""
    assert issue.labels == ()


def test_milestone_from_json_normalizes_counts():
    ms = github.milestone_from_json(
        {"title": "Epic", "description": None, "open_issues": 3, "closed_issues": 2}
    )
    assert ms.description == ""
    assert (ms.open_issues, ms.closed_issues) == (3, 2)


def test_pr_from_json():
    pr = github.pr_from_json(
        {"number": 5, "title": "feat", "state": "merged", "headRefName": "f", "baseRefName": "main"}
    )
    assert pr.state == "MERGED"
    assert pr.head_ref == "f"
    assert pr.base_ref == "main"


def test_dependency_metadata_to_dict_roundtrips_json():
    meta = github.parse_dependency_block(VALID_BLOCK)
    blob = json.dumps(meta.to_dict())
    restored = json.loads(blob)
    assert restored["edges"] == {"42": [], "43": [42], "44": [43, 42]}
    assert restored["has_block"] is True


# --- gh transport (mocked subprocess) ---------------------------------------


def _runner(stdout):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    return run


def test_list_issues_uses_runner_and_normalizes():
    payload = json.dumps(
        [{"number": 1, "title": "a", "state": "open", "labels": [{"name": "x"}]}]
    )
    issues = github.list_issues("acme/app", runner=_runner(payload))
    assert issues[0].number == 1
    assert issues[0].labels == ("x",)


def test_list_issues_handles_empty_output():
    assert github.list_issues("acme/app", runner=_runner("")) == []


def test_find_milestone_matches_title():
    payload = json.dumps(
        [
            {"title": "Other", "description": "x"},
            {"title": "Epic: Name", "description": "<!-- DEPENDENCIES\n#1: []\n-->"},
        ]
    )
    ms = github.find_milestone("acme/app", "Epic: Name", runner=_runner(payload))
    assert ms is not None and ms.title == "Epic: Name"


def test_dependency_graph_for_missing_milestone_warns():
    meta = github.dependency_graph("acme/app", "Nope", runner=_runner("[]"))
    assert meta.edges == {}
    assert any("not found" in w for w in meta.warnings)


def test_dependency_graph_parses_found_milestone():
    payload = json.dumps(
        [{"title": "Epic: Name", "description": VALID_BLOCK}]
    )
    meta = github.dependency_graph("acme/app", "Epic: Name", runner=_runner(payload))
    assert meta.edges == {42: [], 43: [42], 44: [43, 42]}


def test_run_gh_propagates_called_process_error():
    def failing(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="boom")

    with pytest.raises(subprocess.CalledProcessError):
        github.list_issues("acme/app", runner=failing)
