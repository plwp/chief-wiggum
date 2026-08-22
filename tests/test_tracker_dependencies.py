"""Dependency verb group on the tracker seam (chief-wiggum#371).

The parity suite runs link / deps / ready against both backends, because a
dependency edge that means different things per backend is worse than no seam.
Claim is deliberately asymmetric and tested that way: local excludes for real,
GitHub refuses loudly rather than emulating an exclusion it cannot provide.
"""

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from test_tracker import FakeGh  # noqa: E402
from tracker import (  # noqa: E402
    CAP_CLAIM,
    CAP_DEPENDENCIES,
    CAP_READY,
    DependencyCycle,
    GithubBackend,
    IssueDraft,
    LocalBackend,
    UnsupportedCapability,
    main,
    parse_blocked_by_block,
    write_blocked_by_block,
)


@pytest.fixture(params=["github", "local"])
def backend(request, tmp_path):
    if request.param == "github":
        return GithubBackend("acme/widget", runner=FakeGh())
    return LocalBackend(tmp_path)


def use_local_backend(root: Path) -> None:
    """Point backend resolution at `local` for this target, as a real repo would."""
    config = root / "docs" / "cw"
    config.mkdir(parents=True, exist_ok=True)
    (config / "tracker.json").write_text(json.dumps({"backend": "local"}))


def two_issues(backend):
    blocker = backend.create(IssueDraft(title="Blocker", body="must land first"))
    blocked = backend.create(IssueDraft(title="Blocked", body="waits on the blocker"))
    return blocker, blocked


# ------------------------------------------------------------ parity suite


class TestDependencyParity:
    def test_link_then_deps_reads_back_in_both_directions(self, backend):
        blocker, blocked = two_issues(backend)
        backend.link(blocked, blocker)

        downstream = backend.deps(blocked)
        assert downstream.blocked_by == [blocker]
        assert downstream.blocks == []

        upstream = backend.deps(blocker)
        assert upstream.blocked_by == []
        assert upstream.blocks == [blocked]

    def test_link_is_idempotent(self, backend):
        blocker, blocked = two_issues(backend)
        backend.link(blocked, blocker)
        backend.link(blocked, blocker)
        assert backend.deps(blocked).blocked_by == [blocker]

    def test_unlink_removes_the_edge(self, backend):
        blocker, blocked = two_issues(backend)
        backend.link(blocked, blocker)
        backend.unlink(blocked, blocker)
        assert backend.deps(blocked).blocked_by == []
        assert backend.deps(blocker).blocks == []

    def test_ready_excludes_blocked_then_includes_it_once_the_blocker_closes(self, backend):
        """The end-to-end acceptance walk from the ticket."""
        blocker, blocked = two_issues(backend)
        backend.link(blocked, blocker)

        ready = {issue.ref for issue in backend.ready()}
        assert blocker in ready
        assert blocked not in ready, "an open blocker must hold its dependant back"

        backend.update(blocker, {"state": "closed"})

        ready = {issue.ref for issue in backend.ready()}
        assert blocked in ready, "closing the blocker must release the dependant"
        assert blocker not in ready, "ready lists open work only"

    def test_ready_honours_a_query(self, backend):
        backend.create(IssueDraft(title="Alpha", body="first"))
        backend.create(IssueDraft(title="Beta", body="second"))
        titles = {issue.title for issue in backend.ready("Alpha")}
        assert titles == {"Alpha"}

    def test_a_missing_blocker_does_not_wedge_its_dependant(self, backend):
        """A blocker that does not exist cannot be closed, so it must not block."""
        blocked = backend.create(IssueDraft(title="Blocked", body="on a ghost"))
        backend.link(blocked, "local:docs/issues/9999.md"
                     if isinstance(backend, LocalBackend) else "acme/widget#9999")
        assert blocked in {issue.ref for issue in backend.ready()}

    def test_self_link_is_refused(self, backend):
        blocked = backend.create(IssueDraft(title="Solo", body="alone"))
        with pytest.raises(DependencyCycle):
            backend.link(blocked, blocked)

    def test_a_cycle_is_refused_at_link_time(self, backend):
        """A cycle makes every node in it permanently unready, so it is caught
        when the edge is drawn rather than when the schedule comes up short."""
        first, second = two_issues(backend)
        backend.link(second, first)
        with pytest.raises(DependencyCycle, match="cycle"):
            backend.link(first, second)
        assert backend.deps(first).blocked_by == []

    def test_a_longer_cycle_is_refused(self, backend):
        one = backend.create(IssueDraft(title="One"))
        two = backend.create(IssueDraft(title="Two"))
        three = backend.create(IssueDraft(title="Three"))
        backend.link(two, one)
        backend.link(three, two)
        with pytest.raises(DependencyCycle):
            backend.link(one, three)

    def test_capabilities_are_declared_not_probed(self, backend):
        caps = backend.capabilities()
        assert CAP_DEPENDENCIES in caps
        assert CAP_READY in caps


# --------------------------------------------------- claim, deliberately split


class TestLocalClaim:
    def test_claim_is_exclusive(self, tmp_path):
        backend = LocalBackend(tmp_path)
        ref = backend.create(IssueDraft(title="Contended"))
        assert backend.claim(ref, "agent-a") is True
        assert backend.claim(ref, "agent-b") is False, "a second agent must not win"
        assert backend.claimant(ref) == "agent-a"

    def test_reclaiming_what_you_hold_is_idempotent(self, tmp_path):
        backend = LocalBackend(tmp_path)
        ref = backend.create(IssueDraft(title="Mine"))
        assert backend.claim(ref, "agent-a") is True
        assert backend.claim(ref, "agent-a") is True

    def test_claim_records_the_assignee(self, tmp_path):
        backend = LocalBackend(tmp_path)
        ref = backend.create(IssueDraft(title="Assigned"))
        backend.claim(ref, "agent-a")
        assert backend.get(ref).assignee == "agent-a"

    def test_release_frees_the_claim_only_for_its_holder(self, tmp_path):
        backend = LocalBackend(tmp_path)
        ref = backend.create(IssueDraft(title="Held"))
        backend.claim(ref, "agent-a")
        assert backend.release(ref, "agent-b") is False, "only the holder may release"
        assert backend.release(ref, "agent-a") is True
        assert backend.claimant(ref) is None
        assert backend.claim(ref, "agent-b") is True

    def test_releasing_an_unclaimed_issue_is_false_not_an_error(self, tmp_path):
        backend = LocalBackend(tmp_path)
        ref = backend.create(IssueDraft(title="Free"))
        assert backend.release(ref, "agent-a") is False

    def test_concurrent_claims_produce_exactly_one_winner(self, tmp_path):
        """Exclusion must come from O_EXCL, not a read-then-write.

        Repeated over many trials on purpose. A read-then-write implementation
        wins some individual races by luck of scheduling, so a single trial is
        a flaky test that reports the guard as working when it is not. Across
        this many trials it loses at least once.
        """
        workers = 8
        trials = 25
        for trial in range(trials):
            root = tmp_path / f"trial-{trial}"
            root.mkdir()
            backend = LocalBackend(root)
            ref = backend.create(IssueDraft(title="Raced"))
            results = {}
            barrier = threading.Barrier(workers)

            def attempt(name, ref=ref, root=root, barrier=barrier, results=results):
                worker = LocalBackend(root)
                barrier.wait(timeout=30)
                try:
                    results[name] = worker.claim(ref, name)
                except BaseException as exc:  # noqa: BLE001 - asserted below
                    results[name] = exc

            threads = [threading.Thread(target=attempt, args=(f"agent-{i}",), daemon=True)
                       for i in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
                assert not thread.is_alive()

            for name, result in results.items():
                assert not isinstance(result, BaseException), f"{name} raised {result!r}"
            winners = [name for name, won in results.items() if won]
            assert len(winners) == 1, (
                f"trial {trial}: exactly one agent may win, got {winners}"
            )
            assert backend.claimant(ref) == winners[0]


class TestGithubClaimIsRefused:
    def test_claim_raises_rather_than_emulating_an_exclusion_it_cannot_provide(self):
        """AC: no silent no-ops. GitHub has no CAS on assignee."""
        backend = GithubBackend("acme/widget", runner=FakeGh())
        ref = backend.create(IssueDraft(title="Contended"))
        with pytest.raises(UnsupportedCapability, match="compare-and-set"):
            backend.claim(ref, "agent-a")
        with pytest.raises(UnsupportedCapability):
            backend.release(ref, "agent-a")

    def test_capabilities_says_so_before_the_caller_tries(self):
        caps = GithubBackend("acme/widget", runner=FakeGh()).capabilities()
        assert CAP_CLAIM not in caps
        assert CAP_DEPENDENCIES in caps and CAP_READY in caps

    def test_local_declares_claim_support(self, tmp_path):
        assert CAP_CLAIM in LocalBackend(tmp_path).capabilities()


# ----------------------------------------------------------- block encoding


class TestBlockedByBlock:
    def test_roundtrips(self):
        body = write_blocked_by_block("Some prose.", ["acme/app#1", "acme/app#2"])
        assert parse_blocked_by_block(body) == ["acme/app#1", "acme/app#2"]
        assert "Some prose." in body

    def test_rewriting_replaces_rather_than_appends(self):
        body = write_blocked_by_block("Prose.", ["acme/app#1"])
        body = write_blocked_by_block(body, ["acme/app#2"])
        assert parse_blocked_by_block(body) == ["acme/app#2"]
        assert body.count("BLOCKED-BY") == 1

    def test_empty_list_removes_the_block_entirely(self):
        body = write_blocked_by_block("Prose.", ["acme/app#1"])
        cleared = write_blocked_by_block(body, [])
        assert "BLOCKED-BY" not in cleared
        assert cleared.strip() == "Prose."

    def test_a_body_without_a_block_reads_as_no_edges(self):
        assert parse_blocked_by_block("just prose") == []
        assert parse_blocked_by_block("") == []
        assert parse_blocked_by_block(None) == []

    def test_prose_survives_linking(self):
        original = "The real description.\n\nWith paragraphs."
        body = write_blocked_by_block(original, ["acme/app#7"])
        assert "The real description." in body
        assert "With paragraphs." in body


# ------------------------------------------------------------------- CLI


class TestDependencyCLI:
    def test_link_deps_and_ready_round_trip(self, tmp_path, capsys):
        use_local_backend(tmp_path)
        backend = LocalBackend(tmp_path)
        blocker = backend.create(IssueDraft(title="Blocker"))
        blocked = backend.create(IssueDraft(title="Blocked"))

        assert main(["--repo-root", str(tmp_path), "link", blocked, blocker]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["blocked_by"] == [blocker]

        assert main(["--repo-root", str(tmp_path), "deps", blocker]) == 0
        assert json.loads(capsys.readouterr().out)["blocks"] == [blocked]

        assert main(["--repo-root", str(tmp_path), "ready", str(tmp_path)]) == 0
        refs = {issue["ref"] for issue in json.loads(capsys.readouterr().out)}
        assert refs == {blocker}

    def test_claim_exit_code_reflects_whether_it_was_won(self, tmp_path, capsys):
        use_local_backend(tmp_path)
        backend = LocalBackend(tmp_path)
        ref = backend.create(IssueDraft(title="Contended"))
        assert main(["--repo-root", str(tmp_path), "claim", ref, "agent-a"]) == 0
        assert json.loads(capsys.readouterr().out)["held"] is True
        assert main(["--repo-root", str(tmp_path), "claim", ref, "agent-b"]) == 1
        assert json.loads(capsys.readouterr().out)["held"] is False

    def test_capabilities_subcommand_reports_the_matrix(self, tmp_path, capsys):
        use_local_backend(tmp_path)
        assert main(["--repo-root", str(tmp_path), "capabilities", str(tmp_path)]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["backend"] == "LocalBackend"
        assert sorted(payload["capabilities"]) == sorted([CAP_CLAIM, CAP_DEPENDENCIES, CAP_READY])


# ----------------------------------------------- the five verbs are unchanged


class TestExistingContractUnchanged:
    def test_the_original_five_verbs_still_work(self, backend):
        """AC: existing five-verb behaviour and backend resolution are unchanged."""
        ref = backend.create(IssueDraft(title="Ordinary", body="Nothing special"))
        assert backend.get(ref).title == "Ordinary"
        assert any(issue.ref == ref for issue in backend.list())
        backend.update(ref, {"state": "closed"})
        assert backend.get(ref).state == "closed"
        backend.comment(ref, "a note")
        backend.group([ref], "Epic: Something")
        assert [issue.ref for issue in backend.members("Epic: Something")] == [ref]

    def test_linking_does_not_disturb_the_issue_body_or_title(self, backend):
        blocker, blocked = two_issues(backend)
        before = backend.get(blocked)
        backend.link(blocked, blocker)
        after = backend.get(blocked)
        assert after.title == before.title
        assert "waits on the blocker" in after.body

    def test_an_unlinked_issue_reports_no_edges(self, backend):
        ref = backend.create(IssueDraft(title="Lonely"))
        assert backend.deps(ref).to_dict() == {"blocked_by": [], "blocks": []}
