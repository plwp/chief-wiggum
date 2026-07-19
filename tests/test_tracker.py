"""Conformance suite + unit tests for tracker.py (#158)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import tracker
from tracker import (
    GithubBackend,
    IssueDraft,
    LocalBackend,
    _dump_frontmatter,
    _parse_frontmatter,
    parse_ref,
    resolve_backend_name,
)

# --- fake gh CLI (statefully mocks the subprocess boundary) ------------------


def _cp(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr="")


def _split_positional_and_flags(args: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    positional: list[str] = []
    flags: dict[str, list[str]] = {}
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith("--"):
            flags.setdefault(token, []).append(args[i + 1])
            i += 2
        else:
            positional.append(token)
            i += 1
    return positional, flags


class FakeGh:
    """A tiny in-memory stand-in for the ``gh`` CLI.

    Implements just enough of ``gh issue`` / ``gh api .../milestones`` to
    exercise GithubBackend's create/get/list/update/comment/group/members
    without touching the network. Raises CalledProcessError the same way a
    real ``check=True`` subprocess.run would on failure.
    """

    def __init__(self):
        self.issues: dict[str, dict[int, dict]] = {}
        self._next_number: dict[str, int] = {}
        self.milestones: dict[str, set[str]] = {}
        self.comments: dict[tuple[str, int], list[str]] = {}

    def __call__(self, args: list[str], **kwargs) -> subprocess.CompletedProcess:
        assert args[0] == "gh"
        if args[1] == "issue":
            return self._issue(args[2], args[3:])
        if args[1] == "api":
            return self._api(args[2:])
        raise AssertionError(f"unexpected gh command: {args}")

    def _issue(self, sub: str, args: list[str]) -> subprocess.CompletedProcess:
        if sub == "create":
            return self._create(args)
        if sub == "view":
            return self._view(args)
        if sub == "list":
            return self._list(args)
        if sub == "edit":
            return self._edit(args)
        if sub in ("close", "reopen"):
            return self._set_state(sub, args)
        if sub == "comment":
            return self._comment(args)
        raise AssertionError(f"unexpected gh issue subcommand: {sub}")

    def _create(self, args: list[str]) -> subprocess.CompletedProcess:
        _, flags = _split_positional_and_flags(args)
        repo = flags["--repo"][0]
        title = flags["--title"][0]
        body = flags.get("--body", [""])[0]
        labels = flags.get("--label", [])
        assignee = flags.get("--assignee", [None])[0]
        number = self._next_number.get(repo, 1)
        self._next_number[repo] = number + 1
        self.issues.setdefault(repo, {})[number] = {
            "number": number,
            "title": title,
            "body": body,
            "state": "open",
            "labels": [{"name": lbl} for lbl in labels],
            "assignees": [{"login": assignee}] if assignee else [],
            "milestone": None,
            "url": f"https://github.com/{repo}/issues/{number}",
        }
        return _cp(f"https://github.com/{repo}/issues/{number}\n")

    def _find(self, repo: str, number: int) -> dict:
        data = self.issues.get(repo, {}).get(number)
        if data is None:
            raise subprocess.CalledProcessError(1, ["gh", "issue"], output="", stderr="not found")
        return data

    def _view(self, args: list[str]) -> subprocess.CompletedProcess:
        positional, flags = _split_positional_and_flags(args)
        number = int(positional[0])
        repo = flags["--repo"][0]
        return _cp(json.dumps(self._find(repo, number)))

    def _list(self, args: list[str]) -> subprocess.CompletedProcess:
        _, flags = _split_positional_and_flags(args)
        repo = flags["--repo"][0]
        milestone = flags.get("--milestone", [None])[0]
        items = list(self.issues.get(repo, {}).values())
        if milestone:
            items = [d for d in items if (d.get("milestone") or {}).get("title") == milestone]
        return _cp(json.dumps(items))

    def _edit(self, args: list[str]) -> subprocess.CompletedProcess:
        positional, flags = _split_positional_and_flags(args)
        number = int(positional[0])
        repo = flags["--repo"][0]
        data = self._find(repo, number)
        if "--title" in flags:
            data["title"] = flags["--title"][0]
        if "--body" in flags:
            data["body"] = flags["--body"][0]
        if "--add-assignee" in flags:
            data["assignees"] = [{"login": flags["--add-assignee"][0]}]
        if "--milestone" in flags:
            data["milestone"] = {"title": flags["--milestone"][0]}
        if "--remove-milestone" in flags:
            data["milestone"] = None
        if "--remove-label" in flags or "--add-label" in flags:
            current = {lbl["name"] for lbl in data.get("labels", [])}
            for lbl in flags.get("--remove-label", []):
                current.discard(lbl)
            for lbl in flags.get("--add-label", []):
                current.add(lbl)
            data["labels"] = [{"name": lbl} for lbl in sorted(current)]
        return _cp(f"https://github.com/{repo}/issues/{number}\n")

    def _set_state(self, sub: str, args: list[str]) -> subprocess.CompletedProcess:
        positional, flags = _split_positional_and_flags(args)
        number = int(positional[0])
        repo = flags["--repo"][0]
        self._find(repo, number)["state"] = "closed" if sub == "close" else "open"
        return _cp("")

    def _comment(self, args: list[str]) -> subprocess.CompletedProcess:
        positional, flags = _split_positional_and_flags(args)
        number = int(positional[0])
        repo = flags["--repo"][0]
        body = flags["--body"][0]
        self.comments.setdefault((repo, number), []).append(body)
        return _cp(f"https://github.com/{repo}/issues/{number}#issuecomment-1\n")

    def _api(self, args: list[str]) -> subprocess.CompletedProcess:
        endpoint = args[0]
        repo = endpoint.split("/milestones")[0].removeprefix("repos/")
        if "-f" in args:
            kv = args[args.index("-f") + 1]
            key, _, value = kv.partition("=")
            if key == "title":
                self.milestones.setdefault(repo, set()).add(value)
            return _cp("{}")
        titles = self.milestones.get(repo, set())
        payload = [
            {"title": t, "description": "", "number": i, "open_issues": 0, "closed_issues": 0}
            for i, t in enumerate(sorted(titles), start=1)
        ]
        return _cp(json.dumps(payload))


# --- parameterized conformance fixtures --------------------------------------


def _make_github_backend():
    fake = FakeGh()
    backend = GithubBackend("acme/widget", runner=fake)

    def verify_comment(ref: str, text: str) -> None:
        owner_repo, number = GithubBackend._parse_ident(parse_ref(ref)[1])
        assert fake.comments[(owner_repo, number)][-1] == text

    return backend, verify_comment


def _make_local_backend(root: Path):
    backend = LocalBackend(root)

    def verify_comment(ref: str, text: str) -> None:
        assert text in backend.get(ref).body

    return backend, verify_comment


@pytest.fixture(params=["github", "local"])
def backend_and_verify(request, tmp_path):
    if request.param == "github":
        return _make_github_backend()
    return _make_local_backend(tmp_path)


class TestConformance:
    """The same suite runs against every backend: create -> list -> group -> update -> comment."""

    def test_create_then_get_roundtrips(self, backend_and_verify):
        backend, _ = backend_and_verify
        draft = IssueDraft(title="Fix bug", body="Details here", labels=["bug"])
        ref = backend.create(draft)
        issue = backend.get(ref)
        assert issue.ref == ref
        assert issue.title == "Fix bug"
        assert issue.body == "Details here"
        assert issue.labels == ["bug"]
        assert issue.state == "open"

    def test_list_includes_created_issues(self, backend_and_verify):
        backend, _ = backend_and_verify
        ref = backend.create(IssueDraft(title="Alpha"))
        backend.create(IssueDraft(title="Beta"))
        issues = backend.list()
        assert {i.title for i in issues} == {"Alpha", "Beta"}
        assert any(i.ref == ref for i in issues)

    def test_list_query_filters_by_substring(self, backend_and_verify):
        backend, _ = backend_and_verify
        backend.create(IssueDraft(title="Alpha", body="mentions widgets"))
        backend.create(IssueDraft(title="Beta", body="mentions gadgets"))
        issues = backend.list("widgets")
        assert [i.title for i in issues] == ["Alpha"]

    def test_group_and_members_round_trip(self, backend_and_verify):
        backend, _ = backend_and_verify
        ref1 = backend.create(IssueDraft(title="One"))
        ref2 = backend.create(IssueDraft(title="Two"))
        backend.create(IssueDraft(title="Unrelated"))
        backend.group([ref1, ref2], "Epic: Widgets")

        members = backend.members("Epic: Widgets")
        assert {m.ref for m in members} == {ref1, ref2}
        assert all(m.epic == "Epic: Widgets" for m in members)

        # get() reflects the grouping too.
        assert backend.get(ref1).epic == "Epic: Widgets"

    def test_update_replaces_fields(self, backend_and_verify):
        backend, _ = backend_and_verify
        ref = backend.create(IssueDraft(title="Original", labels=["a"]))
        updated = backend.update(
            ref, {"title": "Updated", "labels": ["b", "c"], "state": "closed"}
        )
        assert updated.title == "Updated"
        assert set(updated.labels) == {"b", "c"}
        assert updated.state == "closed"

        refetched = backend.get(ref)
        assert refetched.title == "Updated"
        assert set(refetched.labels) == {"b", "c"}
        assert refetched.state == "closed"

    def test_comment_is_recorded(self, backend_and_verify):
        backend, verify_comment = backend_and_verify
        ref = backend.create(IssueDraft(title="Commentable"))
        backend.comment(ref, "This is a comment")
        verify_comment(ref, "This is a comment")


# --- ref parsing --------------------------------------------------------------


class TestParseRef:
    def test_bare_owner_repo_hash_number_is_github(self):
        assert parse_ref("acme/widget-api#42") == ("gh", "acme/widget-api#42")

    def test_gh_scheme(self):
        assert parse_ref("gh:acme/app#7") == ("gh", "acme/app#7")

    def test_local_scheme(self):
        assert parse_ref("local:docs/issues/0042.md") == ("local", "docs/issues/0042.md")

    @pytest.mark.parametrize("scheme", ["obsidian", "jira"])
    def test_recognizes_future_schemes_syntactically(self, scheme):
        # Not implemented yet, but the grammar recognizes the prefix so a
        # NotImplementedError (not a parse error) is what callers see.
        assert parse_ref(f"{scheme}:whatever") == (scheme, "whatever")

    @pytest.mark.parametrize(
        "bad", ["not-a-ref", "owner/repo", "owner/repo#", "unknownscheme:x", "#42"]
    )
    def test_rejects_malformed_refs(self, bad):
        with pytest.raises(ValueError):
            parse_ref(bad)


# --- backend resolution -------------------------------------------------------


class TestResolveBackendName:
    def test_absent_config_defaults_to_github(self, tmp_path):
        missing_cw_config = tmp_path / "no-such-config.json"
        assert resolve_backend_name(tmp_path, cw_config=missing_cw_config) == "github"

    def test_per_repo_config_wins(self, tmp_path):
        cw_dir = tmp_path / "docs" / "cw"
        cw_dir.mkdir(parents=True)
        (cw_dir / "tracker.json").write_text(json.dumps({"backend": "local"}))
        missing_cw_config = tmp_path / "no-such-config.json"
        assert resolve_backend_name(tmp_path, cw_config=missing_cw_config) == "local"

    def test_cw_side_fallback_used_when_no_per_repo_config(self, tmp_path):
        cw_config = tmp_path / "cw-config.json"
        cw_config.write_text(json.dumps({"tracker": {"backend": "local"}}))
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        assert resolve_backend_name(repo_root, cw_config=cw_config) == "local"

    def test_malformed_per_repo_config_falls_through(self, tmp_path):
        cw_dir = tmp_path / "docs" / "cw"
        cw_dir.mkdir(parents=True)
        (cw_dir / "tracker.json").write_text("{not json")
        missing_cw_config = tmp_path / "no-such-config.json"
        assert resolve_backend_name(tmp_path, cw_config=missing_cw_config) == "github"


class TestGetTracker:
    def test_defaults_to_github_backend(self, tmp_path):
        backend = tracker.get_tracker("acme/app", repo_root=tmp_path)
        assert isinstance(backend, GithubBackend)
        assert backend.repo == "acme/app"

    def test_local_config_selects_local_backend(self, tmp_path):
        cw_dir = tmp_path / "docs" / "cw"
        cw_dir.mkdir(parents=True)
        (cw_dir / "tracker.json").write_text(json.dumps({"backend": "local"}))
        backend = tracker.get_tracker("acme/app", repo_root=tmp_path)
        assert isinstance(backend, LocalBackend)
        assert backend.root == tmp_path.resolve()

    def test_unimplemented_backend_raises(self, tmp_path):
        cw_dir = tmp_path / "docs" / "cw"
        cw_dir.mkdir(parents=True)
        (cw_dir / "tracker.json").write_text(json.dumps({"backend": "jira"}))
        with pytest.raises(NotImplementedError):
            tracker.get_tracker("acme/app", repo_root=tmp_path)


# --- local backend frontmatter format -----------------------------------------


class TestLocalFrontmatter:
    def test_frontmatter_round_trips(self):
        text = _dump_frontmatter(
            {"id": 1, "title": "Hi", "state": "open", "labels": ["a", "b"], "epic": None}
        )
        data, body = _parse_frontmatter(text + "\n\nBody text\n")
        assert data == {"id": 1, "title": "Hi", "state": "open", "labels": ["a", "b"], "epic": None}
        assert body == "Body text\n"

    def test_missing_frontmatter_raises(self):
        with pytest.raises(ValueError):
            _parse_frontmatter("no frontmatter here")

    def test_create_writes_expected_file_layout(self, tmp_path):
        backend = LocalBackend(tmp_path)
        ref = backend.create(IssueDraft(title="First", body="hello", labels=["x"]))
        assert ref == "local:docs/issues/0001.md"
        path = tmp_path / "docs" / "issues" / "0001.md"
        assert path.is_file()
        data, body = _parse_frontmatter(path.read_text())
        assert data["id"] == 1
        assert data["state"] == "open"
        assert data["labels"] == ["x"]
        assert "hello" in body

    def test_ids_increment_across_files(self, tmp_path):
        backend = LocalBackend(tmp_path)
        ref1 = backend.create(IssueDraft(title="A"))
        ref2 = backend.create(IssueDraft(title="B"))
        assert ref1 == "local:docs/issues/0001.md"
        assert ref2 == "local:docs/issues/0002.md"

    def test_get_missing_issue_raises(self, tmp_path):
        backend = LocalBackend(tmp_path)
        with pytest.raises(FileNotFoundError):
            backend.get("local:docs/issues/0099.md")

    def test_wrong_scheme_rejected(self, tmp_path):
        backend = LocalBackend(tmp_path)
        with pytest.raises(ValueError):
            backend.get("gh:acme/app#1")


class TestGithubBackendRefHandling:
    def test_wrong_scheme_rejected(self):
        fake = FakeGh()
        backend = GithubBackend("acme/app", runner=fake)
        with pytest.raises(ValueError):
            backend.get("local:docs/issues/0001.md")

    def test_malformed_ident_rejected(self):
        with pytest.raises(ValueError):
            GithubBackend._parse_ident("not-a-valid-ident")


# --- CLI ----------------------------------------------------------------------


class TestCLI:
    def test_create_get_list_via_cli_local_backend(self, tmp_path, capsys):
        # Select the local backend for this repo_root BEFORE creating, so
        # `create` never falls through to the (unmocked) real `gh` CLI.
        cw_dir = tmp_path / "docs" / "cw"
        cw_dir.mkdir(parents=True)
        (cw_dir / "tracker.json").write_text(json.dumps({"backend": "local"}))

        exit_code = tracker.main(
            ["--repo-root", str(tmp_path), "create", "acme/app", "--title", "CLI issue"]
        )
        assert exit_code == 0
        ref = capsys.readouterr().out.strip()
        assert ref == "local:docs/issues/0001.md"

        exit_code = tracker.main(["--repo-root", str(tmp_path), "get", ref])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert json.loads(out)["title"] == "CLI issue"

        exit_code = tracker.main(["--repo-root", str(tmp_path), "list", "acme/app"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert json.loads(out)[0]["title"] == "CLI issue"

    def test_get_via_cli_github_backend(self, tmp_path, monkeypatch, capsys):
        fake = FakeGh()
        fake.issues.setdefault("acme/app", {})[42] = {
            "number": 42, "title": "From gh", "body": "b", "state": "open",
            "labels": [], "assignees": [], "milestone": None,
            "url": "https://github.com/acme/app/issues/42",
        }
        monkeypatch.setattr(subprocess, "run", fake)
        exit_code = tracker.main(["get", "gh:acme/app#42"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert json.loads(out)["title"] == "From gh"

    def test_unrecognized_ref_reports_error_exit_code(self, capsys):
        exit_code = tracker.main(["get", "not-a-ref"])
        assert exit_code == 1
        assert "Error" in capsys.readouterr().err
