"""The tracker's mcp backend against a fake task service (chief-wiggum#498).

1. Conformance: the shared suite, with the differences the mirror makes.
2. Writes: the service only, GitHub written by the service's mirror; no
   double write.
3. Reads: one configured source, github (default) or service.
4. Claims, statuses and completion from the run's holder.
5. Decisions: who decided, relayed humans, provenance.
6. Keys and pre-adoption issues.
7. The round-trip contract: create, claim, status, comment, decision and
   complete through the service, mirror, then compare GitHub and service.
8. End to end through the CLI and a stdio MCP server.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import test_tracker
import tracker
import tracker_mcp
from chief_wiggum.task_service import (
    InvalidArguments,
    ServiceMapping,
    ServiceState,
    StdioTransport,
    TaskServiceError,
    result_payload,
)
from fake_task_service import FakeTaskService
from test_tracker_work_verbs import RecordingGh
from tracker import CAP_CLAIM, Decision, IssueDraft, Provenance, UnsupportedCapability
from tracker_mcp import ClaimRefused, McpTaskBackend, MirrorError, NotHeld, NotVisibleOnGitHub

TESTS = Path(__file__).resolve().parent
HUMAN = Provenance("human", 'maintainer, 2026-11-03: "make it a priority"')
REPO = "acme/widget"
GH_WRITES = {("issue", "create"), ("issue", "comment"), ("issue", "close")}


@pytest.fixture(autouse=True)
def user_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))


def make_mcp(tmp_path, *, read="github", mirror="strict", notices=None, agent="cw-agent",
             auto_mirror=True, service=None):
    gh = RecordingGh()
    service = service or FakeTaskService(caller="cw-agent")
    backend = McpTaskBackend(
        REPO,
        transport=service,
        github=tracker.GithubBackend(REPO, runner=gh),
        config={"mirror": mirror, "agent": agent, "read": read},
        state=ServiceState(tmp_path / "state.json"),
        notify=(notices.append if notices is not None else lambda _m: None),
        mirror=(lambda: service.mirror(REPO, gh)) if auto_mirror else None,
    )
    return backend, gh, service


def _gh_comments(gh, ref):
    owner_repo, number = tracker.GithubBackend._parse_ident(tracker.parse_ref(ref)[1])
    return gh.comments.get((owner_repo, number), [])


def _only_task(service):
    [task] = service.db["tasks"].values()
    return task


def _cw_gh_writes(gh):
    """GitHub writes CW made itself (the mirror writes the store directly)."""
    return [c for c in gh.calls if tuple(c[1:3]) in GH_WRITES]


# --- 1. conformance -------------------------------------------------------------


@pytest.fixture
def backend_and_verify(tmp_path):
    backend, gh, _ = make_mcp(tmp_path)

    def verify_comment(ref, text):
        # The service's mirror posts the comment in its own format.
        assert text in _gh_comments(gh, ref)[-1]

    return backend, verify_comment


class TestMcpConformance(test_tracker.TestConformance):
    def test_create_then_get_roundtrips(self, backend_and_verify):
        """The body is the author's, followed by the service's mirror block."""
        backend, _ = backend_and_verify
        ref = backend.create(IssueDraft(title="Fix bug", body="Details here", labels=["bug"]))
        issue = backend.get(ref)
        assert (issue.ref, issue.title, issue.labels, issue.state) == (ref, "Fix bug", ["bug"], "open")
        assert issue.body.startswith("Details here\n\n")
        assert "Created in the task service as local#1" in issue.body


# --- 2. writes: the service only -----------------------------------------------------


class TestWrites:
    def test_create_goes_to_the_service_and_the_mirror_makes_the_issue(self, tmp_path):
        backend, gh, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T", provenance=HUMAN))
        assert ref == "gh:acme/widget#1"
        task = _only_task(service)
        assert (task["key"], task["provenance_class"], task["provenance_source"]) == (
            "local#1", "human", HUMAN.source)
        assert _cw_gh_writes(gh) == []
        assert backend.get(ref).title == "T"

    def test_create_carries_the_target_repo(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T", provenance=HUMAN))
        assert service.calls[0][0] == "create_task"
        assert service.calls[0][1]["repo"] == REPO
        assert _only_task(service)["repo"] == REPO
        assert backend.service_report(ref)["repo"] == REPO

    def test_create_without_a_repo_is_refused_before_the_service(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        backend.github.repo = None
        backend.repo = ""
        with pytest.raises(TaskServiceError, match="needs the target's GitHub repo"):
            backend.create(IssueDraft(title="T", provenance=HUMAN))
        assert service.calls == []

    def test_an_invalid_repo_from_the_service_surfaces_as_invalid_repo(self, tmp_path):
        for bad in ("widget", "a/b/c", "a/..", "ac me/w", "/w"):
            backend, _, service = make_mcp(tmp_path)
            backend.github.repo = bad
            with pytest.raises(TaskServiceError, match="InvalidRepo: repo must be <owner>/<repo>"):
                backend.create(IssueDraft(title="T", provenance=HUMAN))
            assert service.db["tasks"] == {}

    def test_a_non_string_repo_is_invalid_arguments(self, tmp_path):
        _, _, service = make_mcp(tmp_path)
        with pytest.raises(InvalidArguments, match="-32602"):
            service.call("create_task", {
                "title": "T", "actor": "worker", "priority": "P2", "done_when": "x",
                "provenance_class": "human", "provenance_source": "s", "repo": 5})

    def test_the_mirror_for_another_repo_leaves_the_task_alone(self, tmp_path):
        backend, gh, service = make_mcp(tmp_path, auto_mirror=False)
        backend.create(IssueDraft(title="T", provenance=HUMAN))
        service.mirror("acme/other", gh)
        assert service.db["mirrors"] == {}
        service.mirror(REPO, gh)
        assert len(service.db["mirrors"]) == 1

    def test_create_without_provenance_is_an_agent_proposal(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        task = _only_task(service)
        assert task["provenance_class"] == "agent"
        assert task["provenance_source"].startswith("cw-agent via tracker.py create, ")
        assert backend.service_report(ref)["agent_proposal"] is True

    def test_agent_task_is_claimable_at_once(self, tmp_path):
        """Provenance: human. The repo owner, 2026-11-03, relayed by the service
        owner: "No we don't need human approval on every single task"."""
        backend, _, _ = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T", provenance=Provenance("agent", "a, today", "a")))
        assert backend.claim(ref, "worker-1") is True

    def test_body_labels_and_epic_reach_github_once_mirrored(self, tmp_path):
        backend, gh, _ = make_mcp(tmp_path, auto_mirror=False)
        ref = backend.create(IssueDraft(title="T", body="AC: x", labels=["bug"], epic="E1"))
        assert ref == "task:local#1"
        assert gh.issues == {}
        backend.transport.mirror(REPO, gh)  # the operator mirrors later
        issue = backend.get(ref)
        assert issue.ref == "gh:acme/widget#1"
        assert issue.body.startswith("AC: x")
        assert (issue.labels, issue.epic) == (["bug"], "E1")
        assert "pending" not in backend.state.task("local#1")

    def test_comment_goes_to_the_service_only(self, tmp_path):
        backend, gh, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.comment(ref, "AC amended: handle empty input")
        [note] = service.db["comments"]
        assert (note["body"], note["author"]) == ("AC amended: handle empty input", "cw-agent")
        assert _cw_gh_writes(gh) == []
        assert "AC amended: handle empty input" in _gh_comments(gh, ref)[-1]

    def test_comment_needs_no_claim_and_changes_no_state(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.comment(ref, "note")
        assert _only_task(service)["state"] == "ready"
        assert backend.lease(ref).state == "unclaimed"

    def test_refused_comment_writes_nothing_anywhere(self, tmp_path):
        backend, gh, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        with pytest.raises(TaskServiceError, match="InvalidComment: text must be nonblank"):
            backend.comment(ref, "   ")
        assert service.db["comments"] == []
        assert _gh_comments(gh, ref) == []

    def test_a_full_flow_makes_no_github_write_of_its_own(self, tmp_path):
        backend, gh, _ = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T", provenance=HUMAN))
        backend.claim(ref, "w")
        backend.set_status(ref, "in_progress", holder="w", note="red tests")
        backend.comment(ref, "c")
        backend.log_decision(ref, Decision(what="x", decided_by="cw-agent", provenance=HUMAN))
        backend.complete(ref, "Merged.", holder="w")
        assert _cw_gh_writes(gh) == []
        assert backend.get(ref).state == "closed"

    def test_without_a_mirror_command_github_lags_and_cw_says_so(self, tmp_path):
        notices: list[str] = []
        backend, gh, service = make_mcp(tmp_path, auto_mirror=False, notices=notices)
        ref = backend.create(IssueDraft(title="T"))
        backend.comment(ref, "c")
        assert gh.issues == {}
        assert len(service.db["comments"]) == 1
        assert sum("no mcp.mirror_command" in n for n in notices) == 1

    def test_mirror_failure_is_loud_in_strict_and_warned_in_best_effort(self, tmp_path):
        def broken():
            raise RuntimeError("tracker request failed")

        for mode in ("strict", "best_effort"):
            notices: list[str] = []
            backend, _, service = make_mcp(tmp_path / mode, mirror=mode, notices=notices,
                                           auto_mirror=False)
            ref = backend.create(IssueDraft(title="T"))
            backend.run_mirror = broken
            if mode == "strict":
                with pytest.raises(MirrorError, match="took the write.*tracker request failed"):
                    backend.comment(ref, "c")
            else:
                backend.comment(ref, "c")
                assert any("tracker request failed" in n for n in notices)
            assert len(service.db["comments"]) == 1  # the service write stands

    def test_bad_modes_refused(self, tmp_path):
        with pytest.raises(ValueError, match="mcp.mirror"):
            make_mcp(tmp_path, mirror="sometimes")
        with pytest.raises(ValueError, match="mcp.read"):
            make_mcp(tmp_path, read="both")

    def test_every_call_matches_the_service_schema(self, tmp_path):
        """The fake rejects missing or unexpected arguments (-32602)."""
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T", provenance=HUMAN))
        backend.comment(ref, "before claim")
        backend.claim(ref, "w")
        backend.heartbeat(ref, "w")
        backend.set_status(ref, "blocked", holder="w", note="n")
        backend.log_decision(ref, Decision(what="x", decided_by="maintainer", provenance=HUMAN))
        backend.complete(ref, "done", holder="w")
        writes = [name for name, _ in service.calls if name not in ("task", "list_tasks")]
        assert writes == ["create_task", "comment", "claim_task", "heartbeat", "log_work",
                          "log_decision", "complete_task"]


# --- 3. reads -------------------------------------------------------------------------


class TestReads:
    def test_github_reads_never_touch_the_service(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        service.calls.clear()
        backend.get(ref)
        backend.list()
        backend.read_context(ref)
        backend.members("E")
        backend.ready()
        assert service.calls == []

    def test_service_reads_make_no_gh_call(self, tmp_path):
        backend, gh, service = make_mcp(tmp_path, read="service")
        ref = backend.create(IssueDraft(title="T", provenance=HUMAN))
        backend.comment(ref, "AC amended")
        gh.calls.clear()
        issue = backend.get(ref)
        assert (issue.ref, issue.title, issue.state) == (ref, "T", "open")
        assert [i.title for i in backend.list()] == ["T"]
        assert [i.ref for i in backend.ready()] == [ref]
        context = backend.read_context(ref)
        assert context["comments"] == [{"author": {"login": "cw-agent"}, "body": "AC amended"}]
        assert context["provenance"] == {"class": "human", "source": HUMAN.source}
        assert gh.calls == []

    def test_service_read_sees_an_unmirrored_task(self, tmp_path):
        backend, _, service = make_mcp(tmp_path, read="service", auto_mirror=False)
        ref = backend.create(IssueDraft(title="T"))
        assert ref == "task:local#1"
        assert backend.get(ref).title == "T"
        assert backend.ready()[0].ref == ref

    def test_service_ready_excludes_blocked_and_held(self, tmp_path):
        backend, _, service = make_mcp(tmp_path, read="service")
        a = backend.create(IssueDraft(title="A"))
        b = backend.create(IssueDraft(title="B"))
        service.call("create_task", {"title": "C", "actor": "worker", "priority": "P2",
                                     "done_when": "x", "provenance_class": "agent",
                                     "provenance_source": "s", "deps": ["local#2"]})
        backend.claim(a, "w")
        assert [i.ref for i in backend.ready()] == [b]

    def test_completion_reads_closed_from_both_sources(self, tmp_path):
        for read in ("github", "service"):
            backend, _, _ = make_mcp(tmp_path / read, read=read)
            ref = backend.create(IssueDraft(title="T"))
            backend.claim(ref, "w")
            backend.complete(ref, "Merged.", holder="w")
            assert backend.get(ref).state == "closed"


# --- 4. claims, status, completion ----------------------------------------------------


class TestHolder:
    def test_status_without_a_claim_writes_nothing(self, tmp_path):
        backend, gh, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        with pytest.raises(NotHeld, match="claim it first"):
            backend.set_status(ref, "blocked", holder="w", note="waiting on API")
        assert _only_task(service)["status_at"] is None
        assert _gh_comments(gh, ref) == []

    def test_status_by_a_non_holder_is_refused(self, tmp_path):
        backend, _, _ = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.claim(ref, "w1")
        with pytest.raises(NotHeld, match="held by 'w1'"):
            backend.set_status(ref, "blocked", holder="w2", note="x")

    def test_invalid_status_refused_before_any_call(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        service.calls.clear()
        with pytest.raises(ValueError, match="invalid status"):
            backend.set_status(ref, "done", holder="w")
        assert service.calls == []

    @pytest.mark.parametrize("status,field,value", [
        ("in_progress", "status_in_progress", "red tests"),
        ("blocked", "status_blocked_on", "red tests"),
        ("ready", "status_next", "red tests"),
    ])
    def test_status_lands_in_its_field_and_is_mirrored(self, tmp_path, status, field, value):
        backend, gh, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.claim(ref, "w")
        backend.set_status(ref, status, holder="w", note=value)
        assert _only_task(service)[field] == value
        assert value in _gh_comments(gh, ref)[-1]

    def test_status_without_a_note_still_says_which(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.claim(ref, "w")
        backend.set_status(ref, "blocked", holder="w")
        assert _only_task(service)["status_blocked_on"] == "blocked"

    def test_complete_without_a_claim_writes_nothing(self, tmp_path):
        backend, gh, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        with pytest.raises(NotHeld):
            backend.complete(ref, "s")
        assert _only_task(service)["state"] == "ready"
        assert backend.github.get(ref).state == "open"

    def test_second_claimant_loses(self, tmp_path):
        backend, _, _ = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        assert backend.claim(ref, "worker-1")
        backend.state.drop("local#1")  # another machine: no local record of the run
        assert backend.claim(ref, "worker-2") is False

    def test_a_blocked_task_is_not_claimable(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        backend.create(IssueDraft(title="A"))
        service.call("create_task", {"title": "B", "actor": "worker", "priority": "P2",
                                     "done_when": "x", "provenance_class": "agent",
                                     "provenance_source": "s", "deps": ["local#1"]})
        assert backend.claim("task:local#2", "w") is False

    def test_the_service_identity_claims_by_task_id(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.claim(ref, "wave-3")
        [args] = [a for name, a in service.calls if name == "claim_task"]
        assert (args["agent"], args["task"]) == ("cw-agent", _only_task(service)["id"])
        lease = backend.lease(ref)
        assert (lease.holder, lease.state, lease.ref) == ("wave-3", "active", ref)
        assert lease.expires_at == "2026-11-03T01:05:00Z"  # heartbeat_at + ttl_secs
        assert backend.state.run("local#1")["resources"] == ["slot:env:chief-wiggum"]

    def test_other_refusals_raise_with_the_reason(self, tmp_path):
        backend, _, _ = make_mcp(tmp_path, agent="someone-else")
        ref = backend.create(IssueDraft(title="T"))
        with pytest.raises(ClaimRefused, match="AgentMismatch"):
            backend.claim(ref, "w")

    def test_heartbeat_renews_and_release_frees_every_lease(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.claim(ref, "w")
        lease_ids = backend.state.run("local#1")["lease_ids"]
        assert lease_ids
        backend.heartbeat(ref, "w")
        assert all(service.db["leases"][i]["beats"] == 1 for i in lease_ids)
        assert backend.release(ref, "w") is True
        assert all(service.db["leases"][i]["released_at"] for i in lease_ids)
        assert _only_task(service)["state"] == "ready"
        assert backend.lease(ref).state == "unclaimed"

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
        assert backend.service_gaps() == ["claim (claim_task)"]
        with pytest.raises(UnsupportedCapability, match="targeted claim"):
            backend.claim(ref, "w")
        assert not any(name == "claim_task" for name, _ in service.calls)

    def test_the_real_service_serves_every_verb(self, tmp_path):
        backend, _, _ = make_mcp(tmp_path)
        assert backend.service_gaps() == []
        assert CAP_CLAIM in backend.capabilities()


# --- 5. decisions -------------------------------------------------------------------


class TestDecisions:
    def test_relayed_human_decision(self, tmp_path):
        backend, gh, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.log_decision(ref, Decision(
            what="GitHub stays readable", decided_by="maintainer", provenance=HUMAN,
            channel="chat", approval_words="make it a priority", relayed_by="cw-agent"))
        [d] = service.db["decisions"]
        assert (d["decided_by"], d["relayed_by"], d["provenance_class"]) == (
            "human:maintainer", "cw-agent", "human")
        assert d["approval_words"] is None  # the service refuses relayed words
        assert d["provenance_source"] == HUMAN.source  # already quotes them
        assert "relayed by cw-agent" in d["channel"]
        assert "GitHub stays readable" in _gh_comments(gh, ref)[-1]

    def test_relayed_words_not_in_the_source_travel_in_it(self):
        args = ServiceMapping.decision_args("T1", Decision(
            what="x", decided_by="maintainer", provenance=Provenance("human", "chat, today"),
            approval_words="yes do it"), "cw-agent")
        assert args["provenance_source"] == 'chat, today (approval words: "yes do it")'
        assert "approval_words" not in args

    def test_agent_decision_by_the_caller_keeps_its_words(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.log_decision(ref, Decision(
            what="Retry twice", decided_by="cw-agent",
            provenance=Provenance("agent", "cw-agent, 2026-11-03")))
        [d] = service.db["decisions"]
        assert (d["decided_by"], d["provenance_class"], d["channel"]) == (
            "cw-agent", "agent", "cw-tracker")

    def test_another_agents_decision_is_logged_on_its_behalf(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.log_decision(ref, Decision(
            what="Retry", decided_by="worker-7", provenance=Provenance("agent", "worker-7, today")))
        [d] = service.db["decisions"]
        assert (d["decided_by"], d["channel"]) == ("cw-agent", "cw-tracker (for worker-7)")

    def test_unknown_human_is_refused_and_nothing_written(self, tmp_path):
        backend, gh, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        with pytest.raises(TaskServiceError, match="UnknownHuman: stranger"):
            backend.log_decision(ref, Decision(what="x", decided_by="stranger", provenance=HUMAN))
        assert service.db["decisions"] == []
        assert _gh_comments(gh, ref) == []


# --- 6. keys and pre-adoption issues -----------------------------------------------------


class TestKeys:
    def test_a_service_key_resolves_to_its_mirrored_issue(self, tmp_path):
        backend, _, _ = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        assert backend.get("task:local#1").ref == ref

    def test_a_key_from_another_machine_resolves_through_the_service(self, tmp_path):
        backend, gh, service = make_mcp(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        backend.state.data["keys"].clear()  # a fresh machine
        assert backend.get("task:local#1").ref == ref

    def test_unmirrored_task_is_not_visible_on_github(self, tmp_path):
        backend, _, service = make_mcp(tmp_path)
        service.add_service_task("service-only")
        with pytest.raises(NotVisibleOnGitHub, match="local#1 has no GitHub issue"):
            backend.get("task:local#1")

    def test_pre_adoption_issue_is_written_on_github_with_a_notice(self, tmp_path):
        notices: list[str] = []
        backend, gh, service = make_mcp(tmp_path, notices=notices)
        gh_ref = backend.github.create(IssueDraft(title="pre-adoption"))
        backend.comment(gh_ref, "hello")
        backend.log_decision(gh_ref, Decision(what="x", decided_by="m", provenance=HUMAN))
        assert _gh_comments(gh, gh_ref)[0] == "hello"
        assert service.db["decisions"] == [] and service.db["comments"] == []
        assert any("does not hold this task" in n for n in notices)
        with pytest.raises(TaskServiceError, match="cannot be claimed"):
            backend.claim(gh_ref, "w")

    def test_an_imported_issue_is_found_by_its_repo_key(self, tmp_path):
        backend, gh, service = make_mcp(tmp_path)
        gh_ref = backend.github.create(IssueDraft(title="pre"))
        service.add_service_task("pre", key="widget#1", source="github")
        backend.comment(gh_ref, "on the service")
        assert service.db["comments"][0]["body"] == "on the service"
        assert backend.claim(gh_ref, "w") is True

    def test_mapping_is_the_one_place(self):
        m = ServiceMapping()
        assert m.candidate_keys("a/b", 3) == ["b#3", "a/b#3"]
        assert m.issue_ref("a/b#3") == "gh:a/b#3"
        assert m.issue_ref(None) is None
        assert m.read_args("local#3") == {"key": "local#3"} and m.read_args("01X") == {"task_id": "01X"}
        assert m.is_lost_claim("TaskHeld: agent-x") and m.is_lost_claim("TaskNotReady: blocked")
        assert not m.is_lost_claim("CapacityFull") and not m.is_lost_claim("AgentMismatch")
        assert m.is_not_found("TaskNotFound")


# --- 7. the round-trip contract -----------------------------------------------------------


def _round_trip(backend):
    ref = backend.create(IssueDraft(title="Add export", provenance=HUMAN))
    assert backend.claim(ref, "wave-1")
    backend.set_status(ref, "in_progress", holder="wave-1", note="red tests")
    backend.comment(ref, "AC amended: handle empty input")
    backend.log_decision(ref, Decision(
        what="CSV only", decided_by="maintainer",
        provenance=Provenance("human", 'maintainer: "CSV is enough"'), channel="chat"))
    backend.complete(ref, "Merged in PR 9.", holder="wave-1")
    return ref


class TestRoundTrip:
    def test_github_and_service_agree_after_the_mirror(self, tmp_path):
        backend, _, _ = make_mcp(tmp_path)
        ref = _round_trip(backend)
        report = backend.compare(ref)
        assert report["mismatches"] == []
        assert report["github"]["state"] == "closed"
        assert report["service"]["summaries"] == ["Merged in PR 9."]

    def test_the_comparison_catches_a_missing_write(self, tmp_path):
        """The gate bites: drop each mirrored comment in turn and it is caught."""
        backend, gh, _ = make_mcp(tmp_path)
        ref = _round_trip(backend)
        comments = _gh_comments(gh, ref)
        for i in range(len(comments)):
            saved = comments.pop(i)
            assert backend.compare(ref)["mismatches"], f"comment {i} dropped, not caught"
            comments.insert(i, saved)
        gh.issues[REPO][1]["state"] = "open"
        assert any("state" in m for m in backend.compare(ref)["mismatches"])

    def test_an_unmirrored_task_fails_the_comparison(self, tmp_path):
        backend, _, _ = make_mcp(tmp_path, auto_mirror=False)
        ref = backend.create(IssueDraft(title="T"))
        assert backend.compare(ref)["mismatches"] == [
            "no GitHub issue: the service has not mirrored this task"]

    def test_the_two_read_sources_agree(self, tmp_path):
        service = FakeTaskService(caller="cw-agent")
        writer, gh, _ = make_mcp(tmp_path / "w", service=service)
        ref = _round_trip(writer)
        reader = McpTaskBackend(
            REPO, transport=service, github=tracker.GithubBackend(REPO, runner=gh),
            config={"agent": "cw-agent", "read": "service"},
            state=ServiceState(tmp_path / "r.json"), notify=lambda _m: None)
        from_github, from_service = writer.get(ref), reader.get(ref)
        assert (from_github.ref, from_github.title, from_github.state) == (
            from_service.ref, from_service.title, from_service.state)


# --- transport ----------------------------------------------------------------------------


class TestStdioTransport:
    def _transport(self, tmp_path):
        return StdioTransport([sys.executable, str(TESTS / "fake_task_service.py"),
                               "--state", str(tmp_path / "svc.json")])

    def test_round_trip_and_refusals(self, tmp_path):
        transport = self._transport(tmp_path)
        try:
            assert "comment" in transport.tools()
            created = transport.call("create_task", {
                "title": "T", "actor": "worker", "priority": "P2", "done_when": "closed",
                "provenance_class": "human", "provenance_source": "maintainer"})
            assert (created["task"]["key"], created["task"]["state"]) == ("local#1", "ready")
            with pytest.raises(TaskServiceError, match="InvalidTask: done_when"):
                transport.call("create_task", {
                    "title": "T", "actor": "worker", "priority": "P2", "done_when": " ",
                    "provenance_class": "human", "provenance_source": "maintainer"})
            with pytest.raises(TaskServiceError, match="UnknownDependency: local#9"):
                transport.call("create_task", {
                    "title": "T", "actor": "worker", "priority": "P2", "done_when": "d",
                    "provenance_class": "human", "provenance_source": "m", "deps": ["local#9"]})
            with pytest.raises(InvalidArguments, match="-32602"):
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


# --- 8. end to end through the CLI ------------------------------------------------------


def _mcp_target(tmp_path, *, caller="cw-agent", read="github"):
    target = tmp_path / "target"
    (target / "docs" / "cw").mkdir(parents=True)
    svc = tmp_path / "svc.json"
    command = [sys.executable, str(TESTS / "fake_task_service.py"), "--state", str(svc)]
    (target / "docs" / "cw" / "tracker.json").write_text(json.dumps({
        "backend": "mcp",
        "mcp": {"command": [*command, "--caller", caller], "agent": "cw-agent",
                "repo": "acme/app", "read": read,
                "mirror_command": [*command, "mirror", "--repo", "{repo}"]},
    }))
    return target


class MirroringGh(RecordingGh):
    """``gh`` for CW's own calls; the fake service's mirror for its command."""

    def __init__(self, svc: Path):
        super().__init__()
        self.svc = svc
        self.mirrors: list[list[str]] = []

    def __call__(self, args, **kwargs):
        if "mirror" in args and "--repo" in args:
            self.mirrors.append(list(args))
            repo = args[args.index("--repo") + 1]
            FakeTaskService(state_file=self.svc).mirror(repo, self)
            return subprocess.CompletedProcess(args, 0, stdout="mirrored\n", stderr="")
        return super().__call__(args, **kwargs)


def test_skill_flow_end_to_end_through_the_cli(tmp_path, monkeypatch, capsys):
    """An /implement-wave-shaped run: create, claim, status, comment, decision, complete."""
    gh = MirroringGh(tmp_path / "svc.json")
    monkeypatch.setattr(subprocess, "run", gh)
    target = _mcp_target(tmp_path)
    root = ["--repo-root", str(target)]

    def run(*argv):
        code = tracker.main([*root, *argv])
        return code, capsys.readouterr().out.strip()

    assert run("backend") == (0, "mcp")
    code, ref = run("create", "acme/app", "--title", "Add export", "--body", "AC: CSV",
                    "--provenance-class", "human", "--provenance-source", 'maintainer: "add export"')
    assert (code, ref) == (0, "gh:acme/app#1")
    assert run("claim", ref, "wave-1")[0] == 0
    assert run("heartbeat", ref, "--holder", "wave-1")[0] == 0
    assert json.loads(run("context", ref)[1])["body"].startswith("AC: CSV")
    assert run("status", ref, "in_progress", "--holder", "wave-1", "--note", "red tests")[0] == 0
    assert run("comment", ref, "AC amended")[0] == 0
    assert run("decision", ref, "--what", "CSV only", "--decided-by", "maintainer",
               "--class", "human", "--source", 'maintainer: "CSV is enough"')[0] == 0
    assert run("complete", ref, "--summary", "Merged.", "--holder", "wave-1")[0] == 0

    db = json.loads((tmp_path / "svc.json").read_text())
    [task] = db["tasks"].values()
    assert (task["key"], task["state"], task["provenance_class"]) == ("local#1", "done", "human")
    assert db["decisions"][0]["decided_by"] == "human:maintainer"
    assert gh.issues["acme/app"][1]["state"] == "closed"
    assert [c for c in gh.calls if tuple(c[1:3]) in GH_WRITES] == []
    assert len(gh.mirrors) == 5  # once after each mirrored write
    assert json.loads(run("get", "task:local#1")[1])["ref"] == ref
    code, out = run("service", "acme/app", "--ref", ref, "--compare")
    assert code == 0, out
    assert json.loads(out)["compare"]["mismatches"] == []


def test_cli_compare_exits_4_on_disagreement(tmp_path, monkeypatch, capsys):
    gh = MirroringGh(tmp_path / "svc.json")
    monkeypatch.setattr(subprocess, "run", gh)
    root = ["--repo-root", str(_mcp_target(tmp_path))]
    assert tracker.main([*root, "create", "acme/app", "--title", "T"]) == 0
    ref = capsys.readouterr().out.strip()
    assert tracker.main([*root, "comment", ref, "hello"]) == 0
    gh.comments[("acme/app", 1)].clear()
    capsys.readouterr()
    assert tracker.main([*root, "service", "acme/app", "--ref", ref, "--compare"]) == 4


def test_cli_status_without_claim_exits_1_and_says_why(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", MirroringGh(tmp_path / "svc.json"))
    root = ["--repo-root", str(_mcp_target(tmp_path))]
    assert tracker.main([*root, "create", "acme/app", "--title", "T"]) == 0
    ref = capsys.readouterr().out.strip()
    assert tracker.main([*root, "complete", ref, "--summary", "s"]) == 1
    assert "claim it first" in capsys.readouterr().err


def test_cli_mirror_failure_exits_3(tmp_path, monkeypatch, capsys):
    gh = MirroringGh(tmp_path / "svc.json")
    monkeypatch.setattr(subprocess, "run", gh)
    root = ["--repo-root", str(_mcp_target(tmp_path))]
    assert tracker.main([*root, "create", "acme/app", "--title", "T"]) == 0
    ref = capsys.readouterr().out.strip()

    def failing(args, **kwargs):
        if "mirror" in args:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="tracker request failed")
        return gh(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", failing)
    assert tracker.main([*root, "comment", ref, "c"]) == 3
    assert "tracker request failed" in capsys.readouterr().err


def test_cli_lost_claim_exits_1_and_other_refusals_say_why(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", MirroringGh(tmp_path / "svc.json"))
    root = ["--repo-root", str(_mcp_target(tmp_path, caller="not-cw"))]
    assert tracker.main([*root, "create", "acme/app", "--title", "T"]) == 0
    ref = capsys.readouterr().out.strip()
    assert tracker.main([*root, "claim", ref, "w"]) == 1
    assert "AgentMismatch" in capsys.readouterr().err


def test_cli_service_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", MirroringGh(tmp_path / "svc.json"))
    root = ["--repo-root", str(_mcp_target(tmp_path, read="service"))]
    assert tracker.main([*root, "create", "acme/app", "--title", "T", "--provenance-class",
                         "agent", "--provenance-source", "cw-agent, 2026-11-03"]) == 0
    ref = capsys.readouterr().out.strip()
    assert tracker.main([*root, "service", "acme/app", "--ref", ref]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["read"], report["mirror_command"], report["gaps"]) == ("service", True, [])
    record = report["ref"][ref]
    assert (record["agent_proposal"], record["key"], record["issue"]) == (True, "local#1", ref)
    assert record["lease"]["state"] == "unclaimed"


def test_cw_side_command_and_target_selection_merge(tmp_path):
    cw = tmp_path / "cw.json"
    cw.write_text(json.dumps({"tracker": {"mcp": {"command": ["svc"], "mirror": "strict"}}}))
    target = tmp_path / "t"
    (target / "docs" / "cw").mkdir(parents=True)
    (target / "docs" / "cw" / "tracker.json").write_text(
        json.dumps({"backend": "mcp", "mcp": {"mirror": "best_effort", "read": "service"}}))
    assert tracker.resolve_backend_name(target, cw_config=cw) == "mcp"
    assert tracker.resolve_mcp_config(target, cw_config=cw) == {
        "command": ["svc"], "mirror": "best_effort", "read": "service"}


def test_mirror_command_fills_in_the_repo(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: (
        seen.append(argv), subprocess.CompletedProcess(argv, 0, "ok", ""))[1])
    run = tracker_mcp._mirror_runner({"mirror_command": ["svc", "mirror", "--repo", "{repo}"]}, "a/b")
    assert run() == "ok"
    assert seen == [["svc", "mirror", "--repo", "a/b"]]
    assert tracker_mcp._mirror_runner({}, "a/b") is None


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
