"""The tracker's mcp backend against a fake task service (chief-wiggum#498).

1. The same conformance suites the github and local backends pass.
2. Mirroring: GitHub first and read from, the service written second.
3. Claims: targeted, refusals surfaced, leases renewed and released.
4. Declared gaps: a tool the service has not built (a comment outside a run)
   skips the mirror loudly, never silently.
5. Keys: local#<n> tasks map to their issue in one place, and a service-only
   task is not visible to GitHub-reading skills.
6. End to end through the CLI and a stdio MCP server: claims, statuses and a
   decision recorded with no skill calling gh issue.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import tracker
import tracker_mcp
from chief_wiggum.task_service import (
    ServiceMapping,
    ServiceState,
    StdioTransport,
    TaskServiceError,
    result_payload,
)
from fake_task_service import FakeTaskService
from test_tracker import TestConformance  # noqa: F401 - re-run against mcp
from test_tracker_work_verbs import RecordingGh, TestWorkVerbConformance  # noqa: F401
from tracker import CAP_CLAIM, Decision, IssueDraft, Provenance, UnsupportedCapability
from tracker_mcp import ClaimRefused, McpTaskBackend, MirrorError, NotVisibleOnGitHub

TESTS = Path(__file__).resolve().parent
HUMAN = Provenance("human", 'maintainer, 2026-11-03: "make it a priority"')


@pytest.fixture(autouse=True)
def user_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))


def make_mcp(tmp_path, *, comment_task=False, mirror="strict", notices=None, agent="cw-agent"):
    gh = RecordingGh()
    service = FakeTaskService(caller="cw-agent", with_comment_task=comment_task)
    backend = McpTaskBackend(
        "acme/widget",
        transport=service,
        github=tracker.GithubBackend("acme/widget", runner=gh),
        config={"mirror": mirror, "agent": agent},
        state=ServiceState(tmp_path / "state.json"),
        notify=(notices.append if notices is not None else lambda _m: None),
    )
    return backend, gh, service


def _gh_comments(gh, ref):
    owner_repo, number = tracker.GithubBackend._parse_ident(tracker.parse_ref(ref)[1])
    return gh.comments.get((owner_repo, number), [])


def _only_task(service):
    [task] = service.db["tasks"].values()
    return task


# --- 1. conformance: the classes imported above run with these fixtures ------


@pytest.fixture
def backend_and_verify(tmp_path):
    backend, gh, _ = make_mcp(tmp_path)

    def verify_comment(ref, text):
        assert _gh_comments(gh, ref)[-1] == text

    return backend, verify_comment


@pytest.fixture
def backend_and_comments(tmp_path):
    backend, gh, _ = make_mcp(tmp_path)
    return backend, lambda ref: _gh_comments(gh, ref)


# --- 2. mirroring -------------------------------------------------------------


class TestMirroring:
    def test_create_carries_provenance_and_maps_the_minted_key(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T", provenance=HUMAN))
        assert ref == "gh:acme/widget#1"
        task = _only_task(service)
        assert task["key"] == "local#1"
        assert (task["provenance_class"], task["provenance_source"]) == ("human", HUMAN.source)
        assert task["done_when"] == "https://github.com/acme/widget/issues/1 is closed"
        assert backend.state.task(ref)["key"] == "local#1"

    def test_create_without_provenance_is_an_agent_proposal(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        task = _only_task(service)
        assert task["provenance_class"] == "agent"
        assert task["provenance_source"].startswith("cw-agent via tracker.py create, ")
        assert backend.state.task(ref)["provenance"]["class"] == "agent"

    def test_agent_task_is_claimable_at_once(self, tmp_path):
        """Provenance: human. The repo owner, 2026-11-03, relayed by the service
        owner: "No we don't need human approval on every single task"."""
        backend, _, _ = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T", provenance=Provenance("agent", "a, today", "a")))
        assert backend.claim(ref, "worker-1") is True

    def test_claim_status_complete_reach_the_service_as_holder(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T", provenance=HUMAN))
        assert backend.claim(ref, "worker-1")
        lease = backend.lease(ref)
        assert (lease.state, lease.holder) == ("active", "worker-1")
        assert lease.to_dag_lease_fields()["attempt_id"] == lease.run_id
        backend.set_status(ref, "in_progress", holder="worker-1", note="tests red")
        backend.complete(ref, "Merged.", holder="worker-1")
        task = _only_task(service)
        assert task["state"] == "done"
        assert task["notes"][0]["status"]["in_progress"] == "tests red"
        assert backend.get(ref).state == "closed"  # GitHub, the source of truth
        assert backend.lease(ref).state == "unclaimed"

    def test_decision_carries_provenance_to_both(self, tmp_path):
        backend, gh, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T", provenance=HUMAN))
        backend.log_decision(ref, Decision(
            what="GitHub stays the source of truth", decided_by="maintainer", provenance=HUMAN,
            channel="chat", relayed_by="cw-agent"))
        [decision] = service.db["decisions"]
        assert decision["provenance_class"] == "human"
        assert decision["provenance_source"] == HUMAN.source
        assert decision["decided_by"] == "human:maintainer"
        assert "relayed by cw-agent" in decision["channel"]
        assert tracker.parse_decision_block(_gh_comments(gh, ref)[0])["provenance_class"] == "human"

    def test_comment_with_run_is_a_run_note(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.claim(ref, "w")
        backend.comment(ref, "progress")
        assert _only_task(service)["notes"][0]["text"] == "progress"

    def test_comment_without_run_uses_comment_task_once_built(self, tmp_path):
        backend, _, service = make_mcp(tmp_path, comment_task=True)
        ref = backend.create(IssueDraft(title="T"))
        backend.comment(ref, "note")
        assert _only_task(service)["comments"] == [{"text": "note", "author": "cw-agent"}]

    def test_status_without_holding_is_a_mirror_error_after_github(self, tmp_path):
        backend, gh, _ = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        with pytest.raises(MirrorError, match="claim it first"):
            backend.set_status(ref, "blocked", holder="w", note="waiting on API")
        assert _gh_comments(gh, ref) == ["**Status: blocked** (w): waiting on API"]

    def test_status_by_a_non_holder_is_refused(self, tmp_path):
        backend, _, _ = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.claim(ref, "w1")
        with pytest.raises(MirrorError, match="held by 'w1'"):
            backend.set_status(ref, "blocked", holder="w2", note="x")

    def test_service_refusal_is_loud_in_strict_and_warned_in_best_effort(self, tmp_path):
        notices: list[str] = []
        for mode in ("strict", "best_effort"):
            backend, _, service = make_mcp(tmp_path / mode, mirror=mode, notices=notices)
            ref = backend.create(IssueDraft(title="T"))
            decision = Decision(what="x", decided_by="maintainer",
                                provenance=Provenance("human", "x"))
            object.__setattr__(decision.provenance, "source", " ")  # bypass our own guard
            if mode == "strict":
                with pytest.raises(MirrorError, match="MissingProvenance"):
                    backend.log_decision(ref, decision)
            else:
                backend.log_decision(ref, decision)
                assert any("MissingProvenance" in n for n in notices)
            assert service.db["decisions"] == []

    def test_reads_never_touch_the_service(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        service.calls.clear()
        backend.get(ref)
        backend.list()
        backend.read_context(ref)
        backend.members("E")
        assert service.calls == []

    def test_every_call_matches_the_service_schema(self, tmp_path):
        """The fake rejects missing or unexpected arguments (-32602)."""
        backend, _, service = make_mcp(tmp_path, comment_task=True)
        ref = backend.create(IssueDraft(title="T", provenance=HUMAN))
        backend.comment(ref, "before claim")
        backend.claim(ref, "w")
        backend.heartbeat(ref, "w")
        backend.comment(ref, "during")
        backend.set_status(ref, "blocked", holder="w", note="n")
        backend.log_decision(ref, Decision(what="x", decided_by="m", provenance=HUMAN))
        backend.complete(ref, "done", holder="w")
        assert [name for name, _ in service.calls] == [
            "create_task", "comment_task", "claim_task", "heartbeat", "heartbeat",
            "log_work", "log_work", "log_decision", "complete_task"]

    def test_bad_mirror_mode_refused(self, tmp_path):
        with pytest.raises(ValueError):
            make_mcp(tmp_path, mirror="sometimes")


# --- 3. claims --------------------------------------------------------------------


class TestClaims:
    def test_second_claimant_loses(self, tmp_path):
        backend, _, _ = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T", provenance=HUMAN))
        assert backend.claim(ref, "worker-1")
        backend.state.drop(ref)  # another machine: no local record of the run
        assert backend.claim(ref, "worker-2") is False

    def test_the_service_identity_claims(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.claim(ref, "wave-3")
        [args] = [a for name, a in service.calls if name == "claim_task"]
        assert args["agent"] == "cw-agent"
        assert args["task"] == _only_task(service)["task_id"]
        assert backend.lease(ref).holder == "wave-3"

    def test_other_refusals_raise_with_the_reason(self, tmp_path):
        backend, _, _ = make_mcp(tmp_path, agent="someone-else")
        ref = backend.create(IssueDraft(title="T"))
        with pytest.raises(ClaimRefused, match="ActorMismatch: someone-else"):
            backend.claim(ref, "w")

    def test_heartbeat_renews_and_release_frees_every_lease(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.claim(ref, "w")
        lease_ids = backend.state.run(ref)["lease_ids"]
        assert len(lease_ids) == 2
        backend.heartbeat(ref, "w")
        assert all(service.db["leases"][i]["beats"] == 1 for i in lease_ids)
        assert backend.release(ref, "w") is True
        assert all(service.db["leases"][i]["state"] == "released" for i in lease_ids)
        assert _only_task(service)["state"] == "ready"

    def test_heartbeat_needs_a_held_run(self, tmp_path):
        backend, _, _ = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        with pytest.raises(LookupError):
            backend.heartbeat(ref)

    def test_no_targeted_claim_means_no_claim(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        tools = service.tools()
        del tools["claim_task"]["properties"]["task"]
        service.tools = lambda: tools
        ref = backend.create(IssueDraft(title="T"))
        assert CAP_CLAIM not in backend.capabilities()
        with pytest.raises(UnsupportedCapability, match="targeted claim"):
            backend.claim(ref, "w")
        assert not any(name == "claim_task" for name, _ in service.calls)


# --- 4. declared gaps ------------------------------------------------------------


class TestDeclaredGaps:
    def test_only_comment_outside_a_run_is_missing(self, tmp_path):
        backend, _, _ = make_mcp(tmp_path)
        assert backend.service_gaps() == ["comment (comment_task)"]
        assert CAP_CLAIM in backend.capabilities()

    def test_comment_without_run_lands_on_github_with_a_notice(self, tmp_path):
        notices: list[str] = []
        backend, gh, service = make_mcp(tmp_path, notices=notices)
        ref = backend.create(IssueDraft(title="T"))
        backend.comment(ref, "hello")
        assert _gh_comments(gh, ref) == ["hello"]
        assert _only_task(service)["comments"] == []
        assert any("no comment_task yet" in n for n in notices)

    def test_unknown_task_is_skipped_with_a_notice(self, tmp_path):
        notices: list[str] = []
        backend, _, service = make_mcp(tmp_path, notices=notices)
        gh_ref = backend.github.create(IssueDraft(title="pre-adoption"))
        backend.log_decision(gh_ref, Decision(what="x", decided_by="m", provenance=HUMAN))
        assert service.db["decisions"] == []
        assert any("has no task acme/widget#1" in n for n in notices)

    def test_existing_service_task_is_found_by_key(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        gh_ref = backend.github.create(IssueDraft(title="pre"))
        service.add_service_task("pre", key="acme/widget#1")
        backend.log_decision(gh_ref, Decision(what="x", decided_by="m", provenance=HUMAN))
        assert len(service.db["decisions"]) == 1


# --- 5. keys ---------------------------------------------------------------------


class TestKeys:
    def test_service_only_task_is_not_visible_on_github(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        service.add_service_task("service-only")
        with pytest.raises(NotVisibleOnGitHub, match="local#1"):
            backend.get("task:local#1")

    def test_a_key_minted_for_our_issue_resolves_to_it(self, tmp_path):
        backend, _, _ = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        assert backend.get("task:local#1").ref == ref

    def test_github_shaped_key_resolves_without_a_record(self, tmp_path):
        backend, _, _ = make_mcp(tmp_path)
        ref = backend.github.create(IssueDraft(title="T"))
        assert backend.get("task:acme/widget#1").ref == ref

    def test_mapping_is_the_one_place(self):
        assert ServiceMapping.gh_ref_for_key("local#3") is None
        assert ServiceMapping.gh_ref_for_key("a/b#3") == "gh:a/b#3"
        assert ServiceMapping.default_key("a/b", 3) == "a/b#3"
        assert ServiceMapping().is_lost_claim("TaskHeld: agent-x")
        assert ServiceMapping().is_lost_claim("TaskNotReady: blocked")
        assert not ServiceMapping().is_lost_claim("CapacityFull")


# --- transport ------------------------------------------------------------------


class TestStdioTransport:
    def _transport(self, tmp_path):
        return StdioTransport([sys.executable, str(TESTS / "fake_task_service.py"),
                               "--state", str(tmp_path / "svc.json")])

    def test_round_trip_and_refusals(self, tmp_path):
        transport = self._transport(tmp_path)
        try:
            assert "create_task" in transport.tools()
            created = transport.call("create_task", {
                "title": "T", "actor": "worker", "priority": "P2", "done_when": "closed",
                "provenance_class": "human", "provenance_source": "maintainer"})
            assert created["task"]["key"] == "local#1"
            with pytest.raises(TaskServiceError, match="InvalidTask: done_when"):
                transport.call("create_task", {
                    "title": "T", "actor": "worker", "priority": "P2", "done_when": " ",
                    "provenance_class": "human", "provenance_source": "maintainer"})
            with pytest.raises(TaskServiceError, match="-32602"):
                transport.call("create_task", {"title": "T"})
        finally:
            transport.close()

    def test_error_results_raise_with_the_reason(self):
        with pytest.raises(TaskServiceError, match="TaskHeld: x"):
            result_payload({"isError": True, "structuredContent": {"reason": "TaskHeld: x"},
                            "content": [{"type": "text", "text": "refused"}]})
        with pytest.raises(TaskServiceError, match="nope"):
            result_payload({"isError": True, "content": [{"type": "text", "text": "nope"}]})

    def test_missing_command_is_refused(self):
        with pytest.raises(ValueError, match="mcp.command"):
            StdioTransport([])


# --- 6. end to end through the CLI ------------------------------------------------


def _mcp_target(tmp_path, *extra_args):
    target = tmp_path / "target"
    (target / "docs" / "cw").mkdir(parents=True)
    (target / "docs" / "cw" / "tracker.json").write_text(json.dumps({
        "backend": "mcp",
        "mcp": {"command": [sys.executable, str(TESTS / "fake_task_service.py"),
                            "--state", str(tmp_path / "svc.json"), *extra_args],
                "agent": "cw-agent", "repo": "acme/app"},
    }))
    return target


def test_skill_flow_end_to_end_through_the_cli(tmp_path, monkeypatch, capsys):
    """An /implement-wave-shaped run: create, claim, read, status, decision, complete."""
    gh = RecordingGh()
    monkeypatch.setattr(subprocess, "run", gh)
    target = _mcp_target(tmp_path)
    root = ["--repo-root", str(target)]

    def run(*argv):
        code = tracker.main([*root, *argv])
        return code, capsys.readouterr().out.strip()

    assert run("backend") == (0, "mcp")
    code, ref = run("create", "acme/app", "--title", "Add export",
                    "--provenance-class", "human", "--provenance-source", 'maintainer: "add export"')
    assert (code, ref) == (0, "gh:acme/app#1")
    code, out = run("capabilities", "acme/app")
    assert "claim" in json.loads(out)["capabilities"]
    assert run("claim", ref, "wave-1")[0] == 0
    assert run("heartbeat", ref, "--holder", "wave-1")[0] == 0
    assert json.loads(run("context", ref)[1])["title"] == "Add export"
    assert run("status", ref, "in_progress", "--holder", "wave-1", "--note", "red tests")[0] == 0
    assert run("decision", ref, "--what", "CSV only", "--decided-by", "maintainer",
               "--class", "human", "--source", 'maintainer: "CSV is enough"')[0] == 0
    assert run("complete", ref, "--summary", "Merged.", "--holder", "wave-1")[0] == 0

    db = json.loads((tmp_path / "svc.json").read_text())
    [task] = db["tasks"].values()
    assert (task["key"], task["state"], task["provenance_class"]) == ("local#1", "done", "human")
    assert task["notes"][0]["status"]["in_progress"] == "red tests"
    assert db["decisions"][0]["provenance_source"] == 'maintainer: "CSV is enough"'
    # GitHub is the record: closed, with the status note, decision and summary.
    assert gh.issues["acme/app"][1]["state"] == "closed"
    assert len(gh.comments[("acme/app", 1)]) == 3
    # The minted key maps back to the issue; a ref the CLI can use.
    assert json.loads(run("get", "task:local#1")[1])["ref"] == ref


def test_cli_mirror_error_exits_3(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", RecordingGh())
    root = ["--repo-root", str(_mcp_target(tmp_path))]
    assert tracker.main([*root, "create", "acme/app", "--title", "T"]) == 0
    ref = capsys.readouterr().out.strip()
    assert tracker.main([*root, "complete", ref, "--summary", "s"]) == 3
    assert "claim it first" in capsys.readouterr().err


def test_cli_lost_claim_exits_1_and_other_refusals_say_why(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", RecordingGh())
    root = ["--repo-root", str(_mcp_target(tmp_path, "--caller", "not-cw"))]
    assert tracker.main([*root, "create", "acme/app", "--title", "T"]) == 0
    ref = capsys.readouterr().out.strip()
    assert tracker.main([*root, "claim", ref, "w"]) == 1
    assert "ActorMismatch" in capsys.readouterr().err


def test_cli_service_report_shows_gaps_and_agent_proposals(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", RecordingGh())
    root = ["--repo-root", str(_mcp_target(tmp_path))]
    assert tracker.main([*root, "create", "acme/app", "--title", "T", "--provenance-class",
                         "agent", "--provenance-source", "cw-agent, 2026-11-03"]) == 0
    ref = capsys.readouterr().out.strip()
    assert tracker.main([*root, "service", "acme/app", "--ref", ref]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["gaps"] == ["comment (comment_task)"]
    record = report["ref"][ref]
    assert (record["agent_proposal"], record["key"]) == (True, "local#1")
    assert record["lease"]["state"] == "unclaimed"


def test_cw_side_command_and_target_selection_merge(tmp_path):
    cw = tmp_path / "cw.json"
    cw.write_text(json.dumps({"tracker": {"mcp": {"command": ["svc"], "mirror": "strict"}}}))
    target = tmp_path / "t"
    (target / "docs" / "cw").mkdir(parents=True)
    (target / "docs" / "cw" / "tracker.json").write_text(
        json.dumps({"backend": "mcp", "mcp": {"mirror": "best_effort"}}))
    assert tracker.resolve_backend_name(target, cw_config=cw) == "mcp"
    assert tracker.resolve_mcp_config(target, cw_config=cw) == {
        "command": ["svc"], "mirror": "best_effort"}


def test_unconfigured_target_stays_on_github(tmp_path):
    missing = tmp_path / "none.json"
    assert tracker.resolve_backend_name(tmp_path, cw_config=missing) == "github"
    assert tracker.resolve_mcp_config(tmp_path, cw_config=missing) == {}


def test_mcp_backend_has_no_gh_issue_call_of_its_own():
    from test_tracker_boundary import find_gh_issue_calls

    for name in ("tracker_mcp.py", "chief_wiggum/task_service.py"):
        text = (TESTS.parent / "scripts" / name).read_text()
        assert find_gh_issue_calls(text) == []
    assert tracker_mcp.McpTaskBackend.__doc__
