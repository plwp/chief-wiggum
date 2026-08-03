"""Conformance suite + unit tests for tracker.py (#158)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import artifacts  # scripts/artifacts.py — the meta-location resolver (#213)
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

CW_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def user_dir(tmp_path, monkeypatch):
    """Isolate every test from the real ~/.chief-wiggum — tests must never
    touch it (resolve_backend_name/LocalBackend now consult the resolver)."""
    d = tmp_path / "cw-user"
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(d))
    return d


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def make_sidecar_target(tmp_path, remote="https://github.com/acme/app.git"):
    """A tmp git repo elected into sidecar mode — tracker config and issue
    storage must resolve OUTSIDE this tree, in the sidecar meta root."""
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", remote)
    artifacts.elect(repo, "sidecar", backing="local")
    return repo

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
        # Comments are stored in the file's ## cw-comments section, which is
        # deliberately NOT part of Issue.body (same semantics as GitHub, where
        # comments are a separate resource).
        raw = backend._resolve_path(ref).read_text()
        assert text in raw
        assert text not in backend.get(ref).body

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

    def test_update_rejects_invalid_state(self, backend_and_verify):
        backend, _ = backend_and_verify
        ref = backend.create(IssueDraft(title="Stateful"))
        with pytest.raises(ValueError, match="invalid state"):
            backend.update(ref, {"state": "bogus"})
        # Validation happens before dispatch: nothing was mutated.
        assert backend.get(ref).state == "open"


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


# --- sidecar awareness (#266) --------------------------------------------------


class TestSidecarAwareness:
    def test_resolve_backend_name_reads_sidecar_meta_root(self, tmp_path):
        """Placing docs/cw/tracker.json in the TARGET tree must NOT select the
        local backend on a sidecar-elected repo — the config has to live in
        the sidecar meta root to be honored (and placing it there dirties the
        tree the sidecar footprint exists to keep clean)."""
        repo = make_sidecar_target(tmp_path)
        (repo / "docs" / "cw").mkdir(parents=True)
        (repo / "docs" / "cw" / "tracker.json").write_text(json.dumps({"backend": "local"}))
        assert resolve_backend_name(repo) == "github"  # wrong location: ignored

        resolver = artifacts.Resolver.resolve(repo)
        sidecar_cfg = resolver.meta_root / "cw" / "tracker.json"
        sidecar_cfg.parent.mkdir(parents=True, exist_ok=True)
        sidecar_cfg.write_text(json.dumps({"backend": "local"}))
        assert resolve_backend_name(repo) == "local"

    def test_sidecar_local_backend_stores_and_lists_from_meta_root(self, tmp_path):
        repo = make_sidecar_target(tmp_path)
        resolver = artifacts.Resolver.resolve(repo)
        (resolver.meta_root / "cw").mkdir(parents=True)
        (resolver.meta_root / "cw" / "tracker.json").write_text(json.dumps({"backend": "local"}))

        backend = tracker.get_tracker("acme/app", repo_root=repo)
        assert isinstance(backend, LocalBackend)
        ref = backend.create(IssueDraft(title="Sidecar issue"))

        # the file physically lives under the SIDECAR meta root...
        expected = resolver.meta_root / "issues" / "0001.md"
        assert expected.is_file()
        # ...never under the target tree.
        assert not (repo / "docs").exists()

        titles = {i.title for i in backend.list()}
        assert titles == {"Sidecar issue"}
        assert backend.get(ref).title == "Sidecar issue"

    def test_sidecar_local_backend_writes_zero_files_into_target_tree(self, tmp_path):
        repo = make_sidecar_target(tmp_path)
        resolver = artifacts.Resolver.resolve(repo)
        (resolver.meta_root / "cw").mkdir(parents=True)
        (resolver.meta_root / "cw" / "tracker.json").write_text(json.dumps({"backend": "local"}))

        backend = tracker.get_tracker("acme/app", repo_root=repo)
        ref = backend.create(IssueDraft(title="Clean tree"))
        backend.comment(ref, "a comment")
        backend.update(ref, {"state": "closed"})

        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert status == "", f"sidecar tracker writes leaked into the target tree: {status!r}"

    def test_sidecar_local_backend_used_even_when_upstream_issues_disabled(self, tmp_path, monkeypatch):
        """Prove the local backend is reached WITHOUT ever falling through to
        the github default: the (unused) github path is wired to explode with
        the exact error an issues-disabled upstream returns, so a resolver
        regression that silently defaults to github fails loudly here rather
        than passing by accident."""
        repo = make_sidecar_target(tmp_path)
        resolver = artifacts.Resolver.resolve(repo)
        (resolver.meta_root / "cw").mkdir(parents=True)
        (resolver.meta_root / "cw" / "tracker.json").write_text(json.dumps({"backend": "local"}))

        real_run = subprocess.run

        def exploding_gh(args, **kwargs):
            # Only the (unused) `gh` path explodes — plain `git` calls (the
            # resolver's own bookkeeping) must keep working for real, or this
            # stub would fail the test for the wrong reason.
            if args and args[0] == "gh":
                raise subprocess.CalledProcessError(
                    1, args, output="", stderr="the 'acme/app' repository has disabled issues"
                )
            return real_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", exploding_gh)

        backend = tracker.get_tracker("acme/app", repo_root=repo)
        assert isinstance(backend, LocalBackend)
        ref = backend.create(IssueDraft(title="Works despite disabled issues"))
        assert backend.get(ref).title == "Works despite disabled issues"

    def test_embedded_mode_unchanged_by_resolver_adoption(self, tmp_path):
        """No election at all (today's status quo) must resolve identically
        to before sidecar-awareness was wired in."""
        cw_dir = tmp_path / "docs" / "cw"
        cw_dir.mkdir(parents=True)
        (cw_dir / "tracker.json").write_text(json.dumps({"backend": "local"}))
        assert resolve_backend_name(tmp_path) == "local"

        backend = tracker.get_tracker("acme/app", repo_root=tmp_path)
        assert isinstance(backend, LocalBackend)
        assert backend.root == tmp_path.resolve()
        assert backend.issues_dir == tmp_path.resolve() / "docs" / "issues"
        ref = backend.create(IssueDraft(title="Embedded issue"))
        assert (tmp_path / "docs" / "issues" / "0001.md").is_file()
        assert backend.get(ref).title == "Embedded issue"


# --- epic membership via tracker.py, not gh issue (#267) -----------------------


class TestEpicMembershipViaTracker:
    """The mechanized half of #267: /architect and /close-epic now fetch epic
    tickets via `tracker.py members`, not `gh issue list --milestone`. Proves
    that primitive works end to end for a local-backend target — including
    one whose upstream has issues disabled, so the github default can't mask
    a regression that silently falls back to it."""

    def test_members_works_against_local_backend_with_issues_disabled_upstream(
        self, tmp_path, monkeypatch, capsys
    ):
        cw_dir = tmp_path / "docs" / "cw"
        cw_dir.mkdir(parents=True)
        (cw_dir / "tracker.json").write_text(json.dumps({"backend": "local"}))

        real_run = subprocess.run

        def exploding_gh(args, **kwargs):
            if args and args[0] == "gh":
                raise subprocess.CalledProcessError(
                    1, args, output="", stderr="the 'acme/app' repository has disabled issues"
                )
            return real_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", exploding_gh)

        run = lambda *a: tracker.main(["--repo-root", str(tmp_path), *a])  # noqa: E731
        refs = []
        for title in ("In the epic", "Also in the epic", "Not in the epic"):
            assert run("create", "acme/app", "--title", title) == 0
            refs.append(capsys.readouterr().out.strip())
        assert run("group", "Epic: Orders", refs[0], refs[1]) == 0
        capsys.readouterr()

        assert run("members", "acme/app", "Epic: Orders") == 0
        members = json.loads(capsys.readouterr().out)
        assert {m["title"] for m in members} == {"In the epic", "Also in the epic"}


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


class TestLocalPathContainment:
    @pytest.mark.parametrize(
        "bad_ref",
        [
            "local:/tmp/x.md",
            "local:../other.md",
            "local:docs/issues/../../secret.md",
            "local:docs/other/0001.md",
            "local:docs/issues/../cw/tracker.json",
        ],
    )
    def test_refs_outside_docs_issues_are_rejected(self, tmp_path, bad_ref):
        backend = LocalBackend(tmp_path)
        with pytest.raises(ValueError):
            backend.get(bad_ref)
        with pytest.raises(ValueError):
            backend.update(bad_ref, {"title": "x"})
        with pytest.raises(ValueError):
            backend.comment(bad_ref, "x")

    def test_contained_ref_still_resolves(self, tmp_path):
        backend = LocalBackend(tmp_path)
        ref = backend.create(IssueDraft(title="In bounds"))
        assert backend.get(ref).title == "In bounds"


class TestLocalComments:
    def test_comment_excluded_from_body_and_list(self, tmp_path):
        backend = LocalBackend(tmp_path)
        ref = backend.create(IssueDraft(title="T", body="original body"))
        backend.comment(ref, "zzyzx unique comment")
        issue = backend.get(ref)
        assert issue.body == "original body"
        # list()'s substring match must not see comment text either.
        assert backend.list("zzyzx") == []
        # But the comment IS persisted, under the cw-comments delimiter.
        raw = backend._resolve_path(ref).read_text()
        assert "## cw-comments" in raw
        assert "zzyzx unique comment" in raw

    def test_multiple_comments_accumulate_under_one_section(self, tmp_path):
        backend = LocalBackend(tmp_path)
        ref = backend.create(IssueDraft(title="T", body="b"))
        backend.comment(ref, "first")
        backend.comment(ref, "second")
        raw = backend._resolve_path(ref).read_text()
        assert raw.count("## cw-comments") == 1
        assert raw.index("first") < raw.index("second")
        assert backend.get(ref).body == "b"

    def test_update_body_preserves_comments(self, tmp_path):
        backend = LocalBackend(tmp_path)
        ref = backend.create(IssueDraft(title="T", body="old body"))
        backend.comment(ref, "keep me")
        updated = backend.update(ref, {"body": "new body"})
        assert updated.body == "new body"
        raw = backend._resolve_path(ref).read_text()
        assert "keep me" in raw
        assert "old body" not in raw


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

    def test_create_get_list_via_cli_sidecar_target(self, tmp_path, capsys):
        """AC (#266): 'tracker.py --repo-root <target> list' finds issues
        stored in the sidecar meta root on a sidecar-elected target."""
        repo = make_sidecar_target(tmp_path)
        resolver = artifacts.Resolver.resolve(repo)
        (resolver.meta_root / "cw").mkdir(parents=True)
        (resolver.meta_root / "cw" / "tracker.json").write_text(json.dumps({"backend": "local"}))

        exit_code = tracker.main(
            ["--repo-root", str(repo), "create", "acme/app", "--title", "CLI sidecar issue"]
        )
        assert exit_code == 0
        ref = capsys.readouterr().out.strip()

        exit_code = tracker.main(["--repo-root", str(repo), "list", "acme/app"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert json.loads(out)[0]["title"] == "CLI sidecar issue"

        exit_code = tracker.main(["--repo-root", str(repo), "get", ref])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert json.loads(out)["title"] == "CLI sidecar issue"

        # zero footprint in the target tree
        assert not (repo / "docs").exists()
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert status == ""

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

    def test_repo_root_honored_from_foreign_cwd(self, tmp_path, monkeypatch, capsys):
        # The commands run from arbitrary cwds; --repo-root must be what
        # selects the TARGET repo's docs/cw/tracker.json, not the cwd.
        repo = tmp_path / "target"
        (repo / "docs" / "cw").mkdir(parents=True)
        (repo / "docs" / "cw" / "tracker.json").write_text(json.dumps({"backend": "local"}))
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        exit_code = tracker.main(
            ["--repo-root", str(repo), "create", "acme/app", "--title", "Foreign cwd"]
        )
        assert exit_code == 0
        assert capsys.readouterr().out.strip() == "local:docs/issues/0001.md"
        assert (repo / "docs" / "issues" / "0001.md").is_file()
        assert not (elsewhere / "docs").exists()

    def test_backend_subcommand_prints_resolved_backend(self, tmp_path, monkeypatch, capsys):
        # Hermetic: don't let a real ~/.chief-wiggum/config.json leak in.
        monkeypatch.setattr(tracker, "DEFAULT_CW_CONFIG", tmp_path / "no-such-config.json")

        exit_code = tracker.main(["--repo-root", str(tmp_path), "backend"])
        assert exit_code == 0
        assert capsys.readouterr().out.strip() == "github"

        (tmp_path / "docs" / "cw").mkdir(parents=True)
        (tmp_path / "docs" / "cw" / "tracker.json").write_text(json.dumps({"backend": "local"}))
        exit_code = tracker.main(["--repo-root", str(tmp_path), "backend"])
        assert exit_code == 0
        assert capsys.readouterr().out.strip() == "local"

    def test_plan_epic_flow_against_local_backend(self, tmp_path, monkeypatch, capsys):
        """The /plan-epic sequence (list -> group -> members -> update) on a
        local-configured scratch repo, driven entirely through the CLI, from a
        foreign cwd — no GitHub mutation path is ever reachable."""
        repo = tmp_path / "scratch-repo"
        (repo / "docs" / "cw").mkdir(parents=True)
        (repo / "docs" / "cw" / "tracker.json").write_text(json.dumps({"backend": "local"}))
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        run = lambda *a: tracker.main(["--repo-root", str(repo), *a])  # noqa: E731

        refs = []
        for title in ("Data model", "API endpoints", "Unrelated chore"):
            assert run("create", "acme/app", "--title", title) == 0
            refs.append(capsys.readouterr().out.strip())

        assert run("group", "Epic: Orders", refs[0], refs[1]) == 0
        capsys.readouterr()

        assert run("members", "acme/app", "Epic: Orders") == 0
        members = json.loads(capsys.readouterr().out)
        assert {m["title"] for m in members} == {"Data model", "API endpoints"}
        assert all(m["epic"] == "Epic: Orders" for m in members)

        assert run("update", refs[0], "--set", "state=closed") == 0
        assert json.loads(capsys.readouterr().out)["state"] == "closed"

        # Epic membership is durable frontmatter in the repo, not GitHub state.
        raw = (repo / "docs" / "issues" / "0001.md").read_text()
        assert '"Epic: Orders"' in raw


# --- command markdown migration (doc contracts) -------------------------------


class TestCommandMarkdownMigration:
    def test_create_issue_command_uses_tracker_and_creates_once(self):
        text = (CW_ROOT / ".claude" / "commands" / "create-issue.md").read_text()
        assert "gh issue create" not in text
        assert "gh issue edit" not in text
        # Exactly ONE create invocation (regression: a milestone used to
        # trigger a second full create).
        creates = re.findall(r'tracker\.py"[^\n]*[^-]\bcreate\b', text)
        assert len(creates) == 1
        # Every tracker call passes the resolved target repo root.
        assert '--repo-root "$target_root"' in creates[0]
        assert "scripts/repo.py" in text

    def test_plan_epic_command_is_backend_conditional(self):
        text = (CW_ROOT / ".claude" / "commands" / "plan-epic.md").read_text()
        assert "gh issue list" not in text
        assert "gh issue edit" not in text
        # Target repo root is resolved and passed on tracker calls.
        assert "scripts/repo.py" in text
        assert '--repo-root "$target_root"' in text
        # Milestone plumbing is conditional on the resolved backend...
        assert '"$backend" = "github"' in text
        # ...and the local backend has a storage path for the dependency graph.
        assert "epic.md" in text

    def test_no_unconditional_gh_milestone_mutation_in_plan_epic(self):
        text = (CW_ROOT / ".claude" / "commands" / "plan-epic.md").read_text()
        for line_block in re.findall(r"```bash\n(.*?)```", text, re.DOTALL):
            if "gh api" in line_block and "milestones" in line_block and "-f" in line_block:
                # every milestone-mutating gh api call must live in a
                # backend=github conditional block
                assert 'if [ "$backend" = "github" ]' in line_block

    def test_architect_uses_tracker_not_gh_issue_for_epic_tickets(self):
        """(#267) /architect Step 1 fetches epic tickets and Step 8 posts
        per-ticket comments — both used to shell out to `gh issue` directly,
        making the skill unusable against a non-github tracker backend (or a
        repo with issues disabled). Both must go through tracker.py."""
        text = (CW_ROOT / ".claude" / "commands" / "architect.md").read_text()
        assert "gh issue list" not in text
        assert "gh issue comment" not in text
        assert "gh issue" not in text
        assert 'scripts/tracker.py" --repo-root "$TARGET_REPO" members' in text
        assert 'scripts/tracker.py" --repo-root "$TARGET_REPO" comment' in text

    def test_close_epic_uses_tracker_not_gh_issue_for_epic_tickets(self):
        """(#267) /close-epic Step 1 fetches the epic's tickets to verify
        they're all closed — same substitution as /architect."""
        text = (CW_ROOT / ".claude" / "commands" / "close-epic.md").read_text()
        assert "gh issue list" not in text
        assert "gh issue" not in text
        assert 'scripts/tracker.py" --repo-root "$TARGET_REPO" members' in text

    def test_no_gh_issue_milestone_listing_in_architect_or_close_epic(self):
        """Grep-based audit (#267 AC): neither skill fetches epic membership
        via `gh issue list --milestone` anywhere in its documented flow —
        tracker.py's `members` is the only sanctioned path now."""
        for name in ("architect.md", "close-epic.md"):
            text = (CW_ROOT / ".claude" / "commands" / name).read_text()
            assert not re.search(r"gh issue list[^\n]*--milestone", text), (
                f"{name} still lists epic tickets via gh issue directly"
            )
