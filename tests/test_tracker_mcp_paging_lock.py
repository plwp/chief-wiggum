"""Mirror serialisation, paged service reads, and launch-per-operation (chief-wiggum#506)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import tracker
from chief_wiggum.mirror_lock import MirrorLockTimeout, mirror_lock
from chief_wiggum.task_service import ServiceMapping, ServiceState
from fake_task_service import FakeTaskService
from test_tracker_mcp import HUMAN, REPO, make_mcp
from test_tracker_work_verbs import RecordingGh
from tracker import IssueDraft
from tracker_mcp import McpTaskBackend, MirrorError

TESTS = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def user_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))


class Gauge:
    """A mirror that records how many runs overlapped."""

    def __init__(self, hold: float = 0.15):
        self.hold, self.now, self.max, self.runs = hold, 0, 0, 0
        self._guard = threading.Lock()

    def __call__(self):
        with self._guard:
            self.now += 1
            self.runs += 1
            self.max = max(self.max, self.now)
        time.sleep(self.hold)
        with self._guard:
            self.now -= 1


def _gh(repo):
    return tracker.GithubBackend(repo, runner=RecordingGh())


def _writers(tmp_path, repos, gauge, config=None):
    """One backend per repo against one fake service, each with a created task."""
    service = FakeTaskService(caller="cw-agent")
    backends, refs = [], []
    for i, repo in enumerate(repos):
        backend = McpTaskBackend(
            repo, transport=service, github=_gh(repo), config=config or {"agent": "cw-agent"},
            state=ServiceState(tmp_path / f"state{i}.json"), notify=lambda _m: None, mirror=gauge,
        )
        refs.append(backend.create(IssueDraft(title=f"T{i}")))
        backends.append(backend)
    return backends, refs


def _comment_together(backends, refs):
    threads = [threading.Thread(target=b.comment, args=(refs[i], "c")) for i, b in enumerate(backends)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads)


class TestMirrorLock:
    def test_two_writers_for_one_repo_mirror_one_at_a_time(self, tmp_path):
        gauge = Gauge()
        backends, refs = _writers(tmp_path, [REPO, REPO], gauge)
        gauge.runs = 0  # the creates mirrored too
        _comment_together(backends, refs)
        assert gauge.runs == 2
        assert gauge.max == 1

    def test_different_repos_do_not_block_each_other(self, tmp_path):
        gauge = Gauge()
        backends, refs = _writers(tmp_path, ["acme/a", "acme/b"], gauge)
        _comment_together(backends, refs)
        assert gauge.max == 2

    def test_timeout_is_a_clear_mirror_error(self, tmp_path):
        backends, refs = _writers(
            tmp_path, [REPO], Gauge(0), config={"agent": "cw-agent", "mirror_lock_timeout_seconds": 0.2})
        with mirror_lock(tmp_path / "cw-user", REPO):
            with pytest.raises(MirrorError, match=r"acme/widget.*0\.2s.*mirror_lock_timeout_seconds"):
                backends[0].comment(refs[0], "c")
        backends[0].comment(refs[0], "again")  # released: works

    def test_best_effort_warns_on_lock_timeout(self, tmp_path):
        notes: list[str] = []
        backend, _, service = make_mcp(tmp_path, mirror="best_effort", notices=notes)
        backend.config["mirror_lock_timeout_seconds"] = 0.1
        ref = backend.create(IssueDraft(title="T"))
        with mirror_lock(tmp_path / "cw-user", REPO):
            backend.comment(ref, "c")
        assert any("mirroring" in n for n in notes)
        assert len(service.db["comments"]) == 1

    def test_lock_is_released_when_the_mirror_fails(self, tmp_path):
        def broken():
            raise RuntimeError("boom")

        backend, _, _ = make_mcp(tmp_path, auto_mirror=False)
        ref = backend.create(IssueDraft(title="T"))
        backend.run_mirror = broken
        with pytest.raises(MirrorError):
            backend.comment(ref, "c")
        with mirror_lock(tmp_path / "cw-user", REPO, timeout=0.1):
            pass

    def test_lock_excludes_across_processes(self, tmp_path):
        code = (
            "import sys,time\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\n"
            "from chief_wiggum.mirror_lock import mirror_lock\n"
            "with mirror_lock(Path(sys.argv[2]),'acme/widget'):\n"
            "    print('held',flush=True)\n    time.sleep(1)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code, str(TESTS.parent / "scripts"), str(tmp_path / "cw-user")],
            stdout=subprocess.PIPE, text=True)
        try:
            assert proc.stdout.readline().strip() == "held"
            with pytest.raises(MirrorLockTimeout):
                with mirror_lock(tmp_path / "cw-user", REPO, timeout=0.2):
                    pass
        finally:
            proc.wait(10)


class TestPagedReads:
    def _service_with(self, n, **kw):
        service = FakeTaskService(caller="cw-agent", **kw)
        for i in range(n):
            service.add_service_task(f"T{i}")
        return service

    def test_list_pages_until_done_with_full_rows(self, tmp_path):
        service = self._service_with(5, page_size=2)
        backend, _, _ = make_mcp(tmp_path, read="service", service=service, auto_mirror=False)
        service.calls.clear()
        assert [i.title for i in backend.list()] == [f"T{i}" for i in range(5)]
        calls = [a for n, a in service.calls if n == "list_tasks"]
        assert len(calls) == 3
        assert all(a["full"] is True for a in calls)
        assert [a.get("cursor") for a in calls] == [None, "2", "4"]

    def test_requests_the_documented_page_size(self, tmp_path):
        service = self._service_with(1)
        backend, _, _ = make_mcp(tmp_path, read="service", service=service, auto_mirror=False)
        service.calls.clear()
        backend.list()
        assert service.calls == [("list_tasks", {"full": True, "limit": 50})]

    def test_full_rows_carry_done_when(self, tmp_path):
        service = self._service_with(3, page_size=2)
        backend, _, _ = make_mcp(tmp_path, read="service", service=service, auto_mirror=False)
        assert {i.body for i in backend.list()} == {"Done when: x"}

    def test_ready_pages_too(self, tmp_path):
        service = self._service_with(5, page_size=2)
        backend, _, _ = make_mcp(tmp_path, read="service", service=service, auto_mirror=False)
        assert len(backend.ready()) == 5

    def test_unpaged_service_still_works(self, tmp_path):
        service = self._service_with(5, paged=False)
        backend, _, _ = make_mcp(tmp_path, read="service", service=service, auto_mirror=False)
        service.calls.clear()
        assert len(backend.list()) == 5
        assert service.calls == [("list_tasks", {})]  # nothing the old schema would refuse

    def test_a_repeating_cursor_cannot_loop_forever(self, tmp_path):
        class Stuck:
            n = 0

            def tools(self):
                return FakeTaskService().tools()

            def call(self, name, arguments):
                self.n += 1
                return {"tasks": [], "next_cursor": "same", "total": 1}

        stuck = Stuck()
        backend = McpTaskBackend(REPO, transport=stuck, github=_gh(REPO), config={"read": "service"},
                                 state=ServiceState(tmp_path / "state.json"))
        backend.list()
        assert stuck.n <= 3

    def test_task_read_pages_back_through_all_notes(self, tmp_path):
        service = FakeTaskService(caller="cw-agent", notes_page=2)
        backend, _, _ = make_mcp(tmp_path, read="service", service=service, auto_mirror=False)
        ref = backend.create(IssueDraft(title="T", provenance=HUMAN))
        for i in range(5):
            backend.comment(ref, f"n{i}")
        assert [c["body"] for c in backend.read_context(ref)["comments"]] == [f"n{i}" for i in range(5)]
        raw = service.record("local#1")  # the service itself shows only the newest page
        assert (raw["notes_total"], len(raw["notes"])) == (5, 2)
        assert raw["notes_before"] == raw["notes"][0]["id"]

    def test_task_read_asks_for_the_largest_note_page_and_cursors_are_the_servers(self, tmp_path):
        service = FakeTaskService(caller="cw-agent", notes_page=100)
        backend, _, _ = make_mcp(tmp_path, read="service", service=service, auto_mirror=False)
        ref = backend.create(IssueDraft(title="T"))
        service.calls.clear()
        backend.read_context(ref)
        reads = [a for n, a in service.calls if n == "task"]
        assert reads and all(a["notes_limit"] == 100 for a in reads)
        with pytest.raises(Exception, match="-32602"):
            service.call("task", {"task_id": "T0001", "notes_before": "forged"})
        with pytest.raises(Exception, match="-32602"):
            service.call("list_tasks", {"cursor": "forged"})
        with pytest.raises(Exception, match="-32602"):
            service.call("list_tasks", {"limit": 201})

    def test_unpaged_task_read_still_works(self, tmp_path):
        service = FakeTaskService(caller="cw-agent", paged=False)
        backend, _, _ = make_mcp(tmp_path, read="service", service=service, auto_mirror=False)
        ref = backend.create(IssueDraft(title="T"))
        backend.comment(ref, "one")
        assert [c["body"] for c in backend.read_context(ref)["comments"]] == ["one"]

    def test_mapping_detects_the_shape(self):
        m = ServiceMapping()
        row = {"id": "T1", "key": "k#1", "title": "t"}
        assert m.page_from({"tasks": [row], "next_cursor": "50", "total": 9}).paged is True
        old = m.page_from({"tasks": [row]})
        assert (old.paged, old.next_cursor, len(old.tasks)) == (False, None, 1)
        assert len(m.page_from([row]).tasks) == 1
        task = m.task_from({"task": row, "notes_total": 40, "notes_before": "N20", "notes": []})
        assert (task.notes_total, task.notes_before) == (40, "N20")


class TestLaunchPerOperation:
    def test_each_cli_operation_launches_and_ends_its_own_service_process(self, tmp_path):
        target = tmp_path / "target"
        (target / "docs" / "cw").mkdir(parents=True)
        svc = tmp_path / "svc.json"
        FakeTaskService(state_file=svc).add_service_task("T")
        log = tmp_path / "launches.log"
        wrapper = tmp_path / "launch.py"
        wrapper.write_text(
            "import os,sys\n"
            f"open({str(log)!r},'a').write(str(os.getpid())+'\\n')\n"
            f"os.execv(sys.executable,[sys.executable,{str(TESTS / 'fake_task_service.py')!r},"
            f"'--state',{str(svc)!r}])\n")
        (target / "docs" / "cw" / "tracker.json").write_text(json.dumps({
            "backend": "mcp", "mcp": {"command": [sys.executable, str(wrapper)], "agent": "cw-agent",
                                      "repo": "acme/app", "read": "service"}}))
        env = {**os.environ, "CHIEF_WIGGUM_USER_DIR": str(tmp_path / "cw-user")}
        for _ in range(2):
            done = subprocess.run(
                [sys.executable, str(TESTS.parent / "scripts" / "tracker.py"), "--repo-root", str(target),
                 "get", "task:local#1"], env=env, capture_output=True, text=True, timeout=60)
            assert done.returncode == 0, done.stderr
        pids = [int(p) for p in log.read_text().split()]
        assert len(pids) == 2 and pids[0] != pids[1]  # a fresh launch per operation
        for pid in pids:  # and none is left running
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
