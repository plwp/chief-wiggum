"""Work-tracking verbs on the tracker interface (chief-wiggum#498).

set_status, complete, log_decision, read_context and lease, run as one
conformance suite over every backend, plus the GitHub adapter's exact ``gh``
argument shapes (the skills moved onto these verbs with no behaviour change).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import tracker
from chief_wiggum import github as gh_meta
from test_tracker import FakeGh, _cp, _split_positional_and_flags
from tracker import (
    LEASE_STATES,
    Decision,
    GithubBackend,
    IssueDraft,
    Lease,
    LocalBackend,
    Provenance,
    UnsupportedCapability,
    parse_decision_block,
    parse_ref,
)

CW_ROOT = Path(__file__).resolve().parents[1]
_REAL_RUN = subprocess.run


@pytest.fixture(autouse=True)
def user_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))


class RecordingGh(FakeGh):
    """FakeGh that also records every argv and honours ``close --comment``."""

    def __init__(self):
        super().__init__()
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        if args[0] != "gh":  # other tools, when patched over subprocess.run
            return _REAL_RUN(args, **kwargs)
        self.calls.append(list(args))
        if args[1] == "issue" and args[2] == "view" and "author" in args[-1]:
            positional, flags = _split_positional_and_flags(args[3:])
            data = dict(self._find(flags["--repo"][0], int(positional[0])))
            key = (flags["--repo"][0], int(positional[0]))
            data["author"] = {"login": "someone"}
            data["comments"] = [
                {"author": {"login": "someone"}, "body": body}
                for body in self.comments.get(key, [])
            ]
            return _cp(json.dumps(data))
        return super().__call__(args, **kwargs)

    def _set_state(self, sub, args):
        positional, flags = _split_positional_and_flags(args)
        if "--comment" in flags:
            key = (flags["--repo"][0], int(positional[0]))
            self.comments.setdefault(key, []).append(flags["--comment"][0])
        return super()._set_state(sub, args)


def _github():
    fake = RecordingGh()
    backend = GithubBackend("acme/widget", runner=fake)

    def comments(ref):
        owner_repo, number = GithubBackend._parse_ident(parse_ref(ref)[1])
        return fake.comments.get((owner_repo, number), [])

    return backend, comments


def _local(root: Path):
    backend = LocalBackend(root)
    return backend, backend._comments


@pytest.fixture(params=["github", "local"])
def backend_and_comments(request, tmp_path):
    return _github() if request.param == "github" else _local(tmp_path)


HUMAN = Provenance("human", 'Pat, 2026-11-03: "ship the adapter"')


class TestWorkVerbConformance:
    def test_status_with_note_is_recorded(self, backend_and_comments):
        backend, comments = backend_and_comments
        ref = backend.create(IssueDraft(title="T"))
        backend.set_status(ref, "in_progress", holder="worker-1", note="tests written")
        assert comments(ref) == ["**Status: in_progress** (worker-1): tests written"]

    def test_status_without_note_writes_nothing(self, backend_and_comments):
        backend, comments = backend_and_comments
        ref = backend.create(IssueDraft(title="T"))
        backend.set_status(ref, "blocked", holder="worker-1")
        assert comments(ref) == []

    def test_invalid_status_is_refused_before_any_write(self, backend_and_comments):
        backend, comments = backend_and_comments
        ref = backend.create(IssueDraft(title="T"))
        for bad in ("done", "doing", ""):
            with pytest.raises(ValueError, match="invalid status"):
                backend.set_status(ref, bad, holder="w", note="x")
        assert comments(ref) == []

    def test_complete_closes_with_summary(self, backend_and_comments):
        backend, comments = backend_and_comments
        ref = backend.create(IssueDraft(title="T"))
        backend.complete(ref, "Implemented in wave 2.")
        assert backend.get(ref).state == "closed"
        assert comments(ref) == ["Implemented in wave 2."]

    def test_decision_round_trips_with_provenance(self, backend_and_comments):
        backend, comments = backend_and_comments
        ref = backend.create(IssueDraft(title="T"))
        decision = Decision(
            what="Use the queue", decided_by="Pat", provenance=HUMAN,
            channel="chat", approval_words="ship the adapter", relayed_by="agent-7",
        )
        backend.log_decision(ref, decision)
        [text] = comments(ref)
        block = parse_decision_block(text)
        assert block == decision.to_dict()
        assert block["provenance_class"] == "human"
        assert "Relayed by: agent-7" in text

    def test_agent_decision_is_shown_as_a_proposal(self, backend_and_comments):
        backend, comments = backend_and_comments
        ref = backend.create(IssueDraft(title="T"))
        backend.log_decision(ref, Decision(
            what="Retry twice", decided_by="agent-3",
            provenance=Provenance("agent", "agent-3, 2026-11-03"),
        ))
        assert comments(ref)[0].startswith("**Decision** (agent proposal):")

    def test_context_carries_comments(self, backend_and_comments):
        backend, _ = backend_and_comments
        ref = backend.create(IssueDraft(title="T", body="B", labels=["bug"]))
        backend.comment(ref, "AC amended: also handle empty input")
        ctx = backend.read_context(ref)
        assert ctx["title"] == "T"
        assert ctx["body"] == "B"
        assert [c["body"] for c in ctx["comments"]] == ["AC amended: also handle empty input"]
        assert [lbl["name"] for lbl in ctx["labels"]] == ["bug"]


class TestProvenanceValidation:
    @pytest.mark.parametrize("klass", ["", "to confirm", "Human", "team"])
    def test_only_the_four_classes(self, klass):
        with pytest.raises(ValueError, match="invalid provenance class"):
            Provenance(klass, "x")

    def test_source_is_required(self):
        with pytest.raises(ValueError, match="source is required"):
            Provenance("human", "  ")

    def test_decision_needs_text_and_decider(self):
        with pytest.raises(ValueError):
            Decision(what="", decided_by="Pat", provenance=HUMAN)
        with pytest.raises(ValueError):
            Decision(what="x", decided_by="", provenance=HUMAN)


class TestGithubShapesUnchanged:
    """The exact gh argv the skills used before they moved behind the interface."""

    def test_complete_is_one_close_with_comment(self):
        fake = RecordingGh()
        backend = GithubBackend("acme/widget", runner=fake)
        ref = backend.create(IssueDraft(title="T"))
        fake.calls.clear()
        backend.complete(ref, "Implemented in wave 1. Merged to main.")
        assert fake.calls == [[
            "gh", "issue", "close", "1", "--repo", "acme/widget",
            "--comment", "Implemented in wave 1. Merged to main.",
        ]]

    def test_context_is_the_implement_step2_view(self):
        fake = RecordingGh()
        backend = GithubBackend("acme/widget", runner=fake)
        ref = backend.create(IssueDraft(title="T"))
        fake.calls.clear()
        backend.read_context(ref)
        assert fake.calls == [[
            "gh", "issue", "view", "1", "--repo", "acme/widget",
            "--json", "title,body,author,labels,assignees,milestone,comments",
        ]]

    def test_chief_wiggum_github_list_and_view_route_through_the_adapter(self):
        seen = []

        def runner(args, **kwargs):
            seen.append(args)
            if args[2] == "list":
                return _cp(json.dumps([{"number": 3, "title": "x"}]))
            return _cp(json.dumps({"number": 3, "title": "x"}))

        assert gh_meta.list_issues("acme/app", state="closed", limit=50, runner=runner)[0].number == 3
        assert gh_meta.view_issue("acme/app", 3, runner=runner).number == 3
        assert seen == [
            ["gh", "issue", "list", "--repo", "acme/app", "--state", "closed",
             "--limit", "50", "--json", "number,title,state,labels,milestone,body"],
            ["gh", "issue", "view", "3", "--repo", "acme/app",
             "--json", "number,title,state,labels,milestone,body"],
        ]

    def test_github_has_no_lease(self):
        with pytest.raises(UnsupportedCapability):
            GithubBackend("acme/widget", runner=RecordingGh()).lease("gh:acme/widget#1")


class TestCli:
    def test_list_numbers_by_epic_and_state(self, monkeypatch, tmp_path, capsys):
        fake = RecordingGh()
        monkeypatch.setattr(subprocess, "run", fake)
        backend = GithubBackend("acme/widget", runner=fake)
        refs = [backend.create(IssueDraft(title=f"T{i}")) for i in range(3)]
        backend.group(refs[:2], "Epic: E")
        backend.update(refs[0], {"state": "closed"})
        argv = ["--repo-root", str(tmp_path), "list", "acme/widget", "--epic", "Epic: E"]
        assert tracker.main(argv + ["--state", "all", "--numbers"]) == 0
        assert capsys.readouterr().out.strip() == "1,2"
        assert tracker.main(argv + ["--state", "closed", "--numbers"]) == 0
        assert capsys.readouterr().out.strip() == "1"

    def test_decision_cli_refuses_unprovenanced_class(self, tmp_path):
        with pytest.raises(SystemExit):
            tracker.main(["decision", "gh:acme/widget#1", "--what", "x",
                          "--decided-by", "Pat", "--class", "to-confirm", "--source", "s"])

    def test_local_lease_reports_the_holder(self, tmp_path, capsys):
        backend = LocalBackend(tmp_path)
        ref = backend.create(IssueDraft(title="T"))
        assert tracker.main(["--repo-root", str(tmp_path), "lease", ref]) == 0
        assert json.loads(capsys.readouterr().out)["state"] == "unclaimed"
        backend.claim(ref, "agent-a")
        assert tracker.main(["--repo-root", str(tmp_path), "lease", ref]) == 0
        out = json.loads(capsys.readouterr().out)
        assert (out["state"], out["holder"]) == ("active", "agent-a")


class TestDagLeaseMapping:
    def test_lease_states_are_the_dag_vocabulary(self):
        vocab = json.loads((CW_ROOT / "schemas/dag/v1/vocabulary.schema.json").read_text())
        assert tuple(vocab["$defs"]["lease_state"]["enum"]) == LEASE_STATES

    def test_dag_fields_cover_the_lease_record(self):
        schema = json.loads((CW_ROOT / "schemas/dag/v1/lease-record.schema.json").read_text())
        lease = Lease(ref="gh:a/b#1", holder="w", lease_id="L1", run_id="R1", epoch=2)
        fields = lease.to_dag_lease_fields()
        # Everything a lease record needs except the envelope keys and the
        # execution node id, which the DAG compiler resolves from intent_ref.
        supplied = set(fields) - {"intent_ref"}
        required = set(schema["required"]) - {"schema_version", "record_type", "execution_node_id"}
        assert supplied == required
        assert fields["attempt_id"] == "R1"
        assert fields["fencing_token"] == "L1"

    def test_bad_lease_state_refused(self):
        with pytest.raises(ValueError):
            Lease(ref="gh:a/b#1", holder=None, state="claimed")
