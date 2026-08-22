"""Survival analysis on a detached HEAD (chief-wiggum#381).

`git worktree add --detach` is the natural way to analyse a historical ref, and
it is exactly what broke: git-of-theseus verifies --branch with `git show-ref
refs/heads/<name>` and falls back to GitPython's `active_branch`, which raises
on a detached HEAD.

The tests use a REAL git repository and a real detached worktree, because the
defect lives in git state rather than in Python. A fake tool stands in for
git-of-theseus so the assertions are about which --branch it receives.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from quality import survival  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    """A tiny repo with one commit, on a pinned branch name.

    `--initial-branch` is pinned because Apple git defaults to `main` and Linux
    CI to `master`; an unpinned fixture passes locally and fails in CI.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "--initial-branch", "main")
    _git(root, "config", "user.name", "Ada")
    _git(root, "config", "user.email", "ada@example.com")
    (root / "a.py").write_text("print('hello')\n")
    _git(root, "add", "-A")
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "first"],
        check=True, capture_output=True, text=True,
        env={
            "GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.com",
            "GIT_COMMITTER_NAME": "Ada", "GIT_COMMITTER_EMAIL": "ada@example.com",
            "GIT_AUTHOR_DATE": "2026-01-01T12:00:00",
            "GIT_COMMITTER_DATE": "2026-01-01T12:00:00",
            "PATH": os.environ.get("PATH", ""),
        },
    )
    return root


@pytest.fixture()
def detached(repo, tmp_path):
    """A detached-HEAD worktree, exactly what `--detach` produces."""
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = tmp_path / "detached"
    _git(repo, "worktree", "add", "--detach", str(worktree), sha)
    yield worktree
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
                   capture_output=True, text=True, check=False)


def _recording_tool(tmp_path, *, payload=None, returncode=0):
    """A stand-in for git-of-theseus that records the argv it was given."""
    record = tmp_path / "argv.json"
    script = tmp_path / "git-of-theseus-analyze"
    body = [
        "#!/usr/bin/env python3",
        "import sys, os, json",
        "args = sys.argv[1:]",
        f"json.dump(args, open({str(record)!r}, 'w'))",
        "outdir = args[args.index('--outdir') + 1]",
        "os.makedirs(outdir, exist_ok=True)",
    ]
    if payload is not None:
        body.append(
            "json.dump(" + repr(payload) + ", open(os.path.join(outdir, 'survival.json'), 'w'))"
        )
    body.append(f"sys.exit({returncode})")
    script.write_text("\n".join(body) + "\n")
    script.chmod(0o755)
    return str(script), record


def _use_tool(monkeypatch, tool_path):
    monkeypatch.setattr(survival.shutil, "which",
                        lambda name: tool_path if name == "git-of-theseus-analyze" else None)
    monkeypatch.setattr(survival.os.path, "exists", os.path.exists)


class TestBranchResolution:
    def test_an_attached_checkout_passes_its_real_branch(self, repo):
        assert survival._current_branch(str(repo)) == "main"

    def test_a_detached_checkout_has_no_branch(self, detached):
        assert survival._current_branch(str(detached)) is None

    def test_a_detached_checkout_gets_a_throwaway_branch(self, detached):
        with survival._analysable_branch(str(detached)) as branch:
            assert branch is not None
            assert branch.startswith("cw/survival-")
            listed = subprocess.run(
                ["git", "-C", str(detached), "branch", "--list", branch],
                capture_output=True, text=True, check=False,
            )
            assert branch in listed.stdout, "the temporary branch must really exist"

    def test_the_throwaway_branch_is_cleaned_up(self, detached):
        with survival._analysable_branch(str(detached)) as branch:
            created = branch
        listed = subprocess.run(
            ["git", "-C", str(detached), "branch", "--list", created],
            capture_output=True, text=True, check=False,
        )
        assert created not in listed.stdout, "a temporary branch must not be left behind"

    def test_the_throwaway_branch_is_cleaned_up_even_if_the_body_raises(self, detached):
        created: list[str] = []
        with pytest.raises(RuntimeError):
            with survival._analysable_branch(str(detached)) as branch:
                created.append(branch)
                raise RuntimeError("analysis blew up")
        assert created, "the context manager must have yielded a branch"
        listed = subprocess.run(
            ["git", "-C", str(detached), "branch", "--list", created[0]],
            capture_output=True, text=True, check=False,
        )
        assert created[0] not in listed.stdout

    def test_an_attached_checkout_creates_no_branch(self, repo):
        before = _git(repo, "branch", "--list").stdout
        with survival._analysable_branch(str(repo)) as branch:
            assert branch == "main"
        assert _git(repo, "branch", "--list").stdout == before

    def test_a_non_git_directory_yields_no_branch_rather_than_inventing_one(self, tmp_path):
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        with survival._analysable_branch(str(plain)) as branch:
            assert branch is None


class TestAnalyzeOnDetachedHead:
    def test_survival_runs_and_passes_a_real_branch(self, detached, tmp_path, monkeypatch):
        """The regression: this used to crash with a detached-HEAD TypeError."""
        payload = {"a" * 40: [[1767225600, 10], [1767830400, 9]]}
        tool, record = _recording_tool(tmp_path, payload=payload)
        _use_tool(monkeypatch, tool)

        result = survival.analyze(str(detached), workdir=str(tmp_path / "work"))
        assert result.get("status") != "crashed", result
        assert "skipped" not in result, result

        argv = json.loads(record.read_text())
        assert "--branch" in argv, "a real branch must be passed"
        branch = argv[argv.index("--branch") + 1]
        assert branch.startswith("cw/survival-")
        assert branch != "HEAD", "HEAD was never a real branch; that was the bug"

    def test_the_temporary_branch_does_not_survive_the_analysis(self, detached, tmp_path,
                                                                monkeypatch):
        tool, record = _recording_tool(tmp_path, payload={"b" * 40: [[1767225600, 5]]})
        _use_tool(monkeypatch, tool)
        survival.analyze(str(detached), workdir=str(tmp_path / "work"))
        branch = json.loads(record.read_text())
        name = branch[branch.index("--branch") + 1]
        listed = subprocess.run(["git", "-C", str(detached), "branch", "--list", name],
                                capture_output=True, text=True, check=False)
        assert name not in listed.stdout

    def test_an_attached_repo_passes_its_own_branch_not_head(self, repo, tmp_path, monkeypatch):
        tool, record = _recording_tool(tmp_path, payload={"c" * 40: [[1767225600, 5]]})
        _use_tool(monkeypatch, tool)
        survival.analyze(str(repo), workdir=str(tmp_path / "work"))
        argv = json.loads(record.read_text())
        assert argv[argv.index("--branch") + 1] == "main"

    def test_a_crashing_tool_is_still_reported_as_crashed(self, detached, tmp_path, monkeypatch):
        """The #289 skipped/crashed split must survive this change."""
        tool, _ = _recording_tool(tmp_path, returncode=1)
        _use_tool(monkeypatch, tool)
        result = survival.analyze(str(detached), workdir=str(tmp_path / "work"))
        assert result.get("status") == "crashed"
        assert "exited 1" in result.get("crashed", "")
