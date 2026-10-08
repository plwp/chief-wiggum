"""Tests for scripts/remote_ci.py — remote CI is the floor (gh is mocked)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import remote_ci as rc

SHA = "23e9399abcdef0123456789"


def _gh(runs_by_poll, workflows=1, tip=SHA):
    """A fake `gh`: returns successive run lists on each `run list` call."""
    polls = iter(runs_by_poll)
    calls = []

    def gh(args):
        calls.append(args)
        if args[:2] == ["api", "repos/o/r/actions/workflows"]:
            return f"{workflows}\n"
        if args[0] == "api" and "/commits/" in args[1]:
            return tip + "\n"
        if args[:2] == ["run", "list"]:
            assert "--commit" in args
            return json.dumps(next(polls))
        raise AssertionError(args)

    gh.calls = calls
    return gh


def _run(status, conclusion="", name="CI"):
    return {"databaseId": 1, "workflowName": name, "status": status,
            "conclusion": conclusion, "url": "https://x/run/1", "headBranch": "main"}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _wait(gh, **kw):
    clock = Clock()
    out = io.StringIO()
    code = rc.wait("o/r", kw.pop("sha", SHA), kw.pop("branch", "main"), gh=gh,
                   sleep=clock.sleep, clock=clock, out=out, poll=10, **kw)
    return code, out.getvalue()


def test_green_after_runs_complete():
    gh = _gh([[_run("in_progress")], [_run("completed", "success")]])
    code, out = _wait(gh)
    assert code == rc.GREEN and "GREEN" in out


def test_red_is_stop_the_line():
    gh = _gh([[_run("completed", "failure", "CI"), _run("in_progress", name="lint")]])
    code, out = _wait(gh)
    assert code == rc.RED
    assert "STOP THE LINE" in out and "CI: failure" in out


def test_cancelled_is_not_green():
    code, _ = _wait(_gh([[_run("completed", "cancelled")]]))
    assert code == rc.RED


def test_no_workflows_is_reported_not_skipped():
    code, out = _wait(_gh([], workflows=0))
    assert code == rc.NO_CI and "NO_CI" in out and "do not call it green" in out


def test_no_run_for_the_sha_is_not_green():
    code, out = _wait(_gh([[]] * 100), appear_timeout=30, timeout=600)
    assert code == rc.PENDING and "no workflow run appeared" in out


def test_still_running_at_timeout_is_pending():
    code, out = _wait(_gh([[_run("in_progress")]] * 100), timeout=60)
    assert code == rc.PENDING and "still running" in out


def test_branch_tip_is_resolved_when_no_sha_given():
    gh = _gh([[_run("completed", "success")]], tip="feedface00")
    code, out = _wait(gh, sha=None)
    assert code == rc.GREEN and "feedface00" in out
    run_list = next(c for c in gh.calls if c[:2] == ["run", "list"])
    assert run_list[run_list.index("--commit") + 1] == "feedface00"


def test_gh_failure_is_an_error_not_a_pass():
    def gh(args):
        raise rc.GhError("HTTP 401")

    code, out = _wait(gh)
    assert code == rc.ERROR and "401" in out


# ---- the workflows actually call it --------------------------------------------


COMMANDS = Path(__file__).resolve().parent.parent / ".claude" / "commands"


def test_implement_wave_waits_for_remote_ci_after_promote_and_runs_gate_self_tests():
    text = (COMMANDS / "implement-wave.md").read_text()
    promote = text[text.index("#### 4g: Promote staging to main"):text.index("#### 4h")]
    assert promote.index("git push origin") < promote.index("scripts/remote_ci.py\" wait")
    assert "stop the line" in promote and "NO_CI" in promote
    check = text[text.index("#### 4e: Wave integration check"):text.index("#### 4f")]
    assert "ratchet.py\" self-test" in check and "check_traceability.py\" --self-test" in check
    assert check.index("self-test") < check.index("1. **Full test suite**")


def test_implement_waits_for_remote_ci_on_the_default_branch():
    text = (COMMANDS / "implement.md").read_text()
    assert "scripts/remote_ci.py\" wait" in text
