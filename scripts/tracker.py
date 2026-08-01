#!/usr/bin/env python3
"""
Tracker-agnostic issue interface for chief-wiggum.

Every workflow that touches issues (``/create-issue``, ``/plan-epic``, ...)
used to call the ``gh`` CLI directly, hard-coupling issue tracking to GitHub.
This module gives them a small, backend-pluggable interface instead, mirroring
the existing ``repo.py`` / provider-role patterns.

Issue refs are URIs: ``gh:owner/repo#42``, ``local:docs/issues/0042.md``. A
bare ``owner/repo#42`` keeps meaning GitHub, so existing usage/docs don't
break. ``obsidian:`` and ``jira:`` prefixes are recognized by the ref grammar
for forward compatibility but have no backend wired up yet (see
docs/tracker.md).

Backends (this ticket): ``github`` (wraps ``gh``, the reference
implementation) and ``local`` (one markdown file per issue with YAML
frontmatter under ``docs/issues/`` in the target repo).

Backend resolution is per-target-repo: ``docs/cw/tracker.json`` in the target
repo (``{"backend": "github"}``); if absent, ``~/.chief-wiggum/config.json``'s
``tracker.backend``; if that's absent too, ``github`` (today's behavior).

As a module::

    from tracker import GithubBackend, LocalBackend, IssueDraft, get_tracker

    backend = get_tracker("acme/app")  # -> GithubBackend unless configured otherwise
    ref = backend.create(IssueDraft(title="Fix bug", body="...", labels=["bug"]))
    issue = backend.get(ref)

As a CLI::

    python3 tracker.py --repo-root /path/to/checkout backend
    python3 tracker.py get gh:acme/app#42
    python3 tracker.py list acme/app --epic "Epic: Name"
    python3 tracker.py create acme/app --title "Fix bug" --body "..." --label bug
    python3 tracker.py update gh:acme/app#42 --set state=closed
    python3 tracker.py comment gh:acme/app#42 "Looks good"
    python3 tracker.py group "Epic: Name" gh:acme/app#42 gh:acme/app#43
    python3 tracker.py members acme/app "Epic: Name"
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum import github as gh_meta  # noqa: E402

Runner = Callable[..., subprocess.CompletedProcess]

ISSUES_SUBDIR = Path("docs") / "issues"


# --- data model --------------------------------------------------------------


@dataclass
class Issue:
    ref: str
    title: str
    body: str = ""
    state: str = "open"
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None
    epic: str | None = None
    url_or_path: str = ""

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "title": self.title,
            "body": self.body,
            "state": self.state,
            "labels": list(self.labels),
            "assignee": self.assignee,
            "epic": self.epic,
            "url_or_path": self.url_or_path,
        }


@dataclass
class IssueDraft:
    title: str
    body: str = ""
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None
    epic: str | None = None


VALID_STATES = ("open", "closed")


def _validate_update_fields(fields: dict[str, Any]) -> None:
    """Shared pre-dispatch validation for ``update()`` across all backends."""
    if "state" in fields and fields["state"] not in VALID_STATES:
        raise ValueError(
            f"invalid state {fields['state']!r}: must be one of {', '.join(VALID_STATES)}"
        )


def _matches_query(issue: Issue, query: str | dict[str, Any] | None) -> bool:
    """Client-side filter shared by every backend's ``list()``."""
    if query is None:
        return True
    if isinstance(query, str):
        haystack = f"{issue.title}\n{issue.body}".lower()
        return query.lower() in haystack
    if isinstance(query, dict):
        for key, value in query.items():
            if key == "labels":
                if value not in issue.labels:
                    return False
            elif getattr(issue, key, None) != value:
                return False
        return True
    raise TypeError(f"unsupported query type: {type(query)!r}")


# --- ref parsing ---------------------------------------------------------------

KNOWN_SCHEMES = ("gh", "local", "obsidian", "jira")

_SCHEME_RE = re.compile(r"^(" + "|".join(KNOWN_SCHEMES) + r"):(.+)$")
_BARE_GH_RE = re.compile(r"^[\w.-]+/[\w.-]+#\d+$")


def parse_ref(ref: str) -> tuple[str, str]:
    """Split an issue ref into ``(scheme, identifier)``.

    Bare ``owner/repo#42`` (no scheme prefix) is treated as ``("gh", ref)`` so
    existing GitHub-only usage keeps working unchanged.
    """
    match = _SCHEME_RE.match(ref)
    if match:
        return match.group(1), match.group(2)
    if _BARE_GH_RE.match(ref):
        return "gh", ref
    raise ValueError(f"unrecognized issue ref: {ref!r}")


# --- local frontmatter (a valid subset of YAML, no new dependency) -----------


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


def _dump_frontmatter(data: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in data.items():
        # json.dumps produces valid YAML scalars/flow-sequences, so this
        # round-trips through both our own parser and a real YAML parser
        # (e.g. Obsidian's), without pulling in a YAML dependency.
        lines.append(f"{key}: {json.dumps(value)}")
    lines.append("---")
    return "\n".join(lines)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("missing YAML frontmatter block")
    raw_frontmatter, body = match.group(1), match.group(2)
    data: dict[str, Any] = {}
    for line in raw_frontmatter.splitlines():
        if not line.strip():
            continue
        key, _, raw_value = line.partition(":")
        data[key.strip()] = json.loads(raw_value.strip())
    return data, body.lstrip("\n")


# --- github backend ------------------------------------------------------------


class GithubBackend:
    """Reference-implementation backend: wraps ``gh issue``/``gh api``."""

    def __init__(self, repo: str, *, runner: Runner | None = None):
        self.repo = repo
        # Resolved at construction time (not bound as a parameter default) so
        # tests can monkeypatch subprocess.run before a backend is built.
        self.runner = runner or subprocess.run

    def _run(self, args: list[str]) -> str:
        result = self.runner(
            ["gh", *args], capture_output=True, text=True, check=True, timeout=60
        )
        return result.stdout

    @staticmethod
    def _parse_ident(ident: str) -> tuple[str, int]:
        owner_repo, sep, number = ident.rpartition("#")
        if not sep or not owner_repo or not number.isdigit():
            raise ValueError(f"malformed github issue ref identifier: {ident!r}")
        return owner_repo, int(number)

    @staticmethod
    def _number_from_url(url: str) -> int:
        match = re.search(r"/issues/(\d+)\s*$", url)
        if not match:
            raise ValueError(f"could not parse issue number from gh output: {url!r}")
        return int(match.group(1))

    @staticmethod
    def _issue_from_json(owner_repo: str, data: dict[str, Any]) -> Issue:
        labels = [
            lbl["name"] if isinstance(lbl, dict) else str(lbl)
            for lbl in (data.get("labels") or [])
        ]
        assignees = data.get("assignees") or []
        assignee: str | None = None
        if assignees:
            first = assignees[0]
            assignee = first.get("login") if isinstance(first, dict) else str(first)
        milestone = data.get("milestone")
        epic = milestone.get("title") if isinstance(milestone, dict) else milestone
        number = int(data["number"])
        return Issue(
            ref=f"gh:{owner_repo}#{number}",
            title=data.get("title", ""),
            body=data.get("body") or "",
            state=str(data.get("state", "open")).lower(),
            labels=labels,
            assignee=assignee,
            epic=epic,
            url_or_path=data.get("url") or f"https://github.com/{owner_repo}/issues/{number}",
        )

    def _view(self, owner_repo: str, number: int) -> Issue:
        out = self._run(
            [
                "issue", "view", str(number), "--repo", owner_repo,
                "--json", "number,title,body,state,labels,assignees,milestone,url",
            ]
        )
        return self._issue_from_json(owner_repo, json.loads(out))

    def get(self, ref: str) -> Issue:
        scheme, ident = parse_ref(ref)
        if scheme != "gh":
            raise ValueError(f"GithubBackend cannot resolve ref with scheme {scheme!r}: {ref!r}")
        owner_repo, number = self._parse_ident(ident)
        return self._view(owner_repo, number)

    def list(self, query: str | dict[str, Any] | None = None) -> list[Issue]:
        args = [
            "issue", "list", "--repo", self.repo, "--state", "all", "--limit", "200",
            "--json", "number,title,body,state,labels,assignees,milestone,url",
        ]
        if isinstance(query, dict) and query.get("epic"):
            args += ["--milestone", query["epic"]]
        out = self._run(args)
        issues = [self._issue_from_json(self.repo, d) for d in json.loads(out or "[]")]
        return [issue for issue in issues if _matches_query(issue, query)]

    def create(self, draft: IssueDraft) -> str:
        args = [
            "issue", "create", "--repo", self.repo,
            "--title", draft.title, "--body", draft.body or "",
        ]
        for label in draft.labels:
            args += ["--label", label]
        if draft.assignee:
            args += ["--assignee", draft.assignee]
        out = self._run(args)
        number = self._number_from_url(out.strip())
        ref = f"gh:{self.repo}#{number}"
        if draft.epic:
            self.group([ref], draft.epic)
        return ref

    def update(self, ref: str, fields: dict[str, Any]) -> Issue:
        _validate_update_fields(fields)
        scheme, ident = parse_ref(ref)
        if scheme != "gh":
            raise ValueError(f"GithubBackend cannot resolve ref with scheme {scheme!r}: {ref!r}")
        owner_repo, number = self._parse_ident(ident)

        if "state" in fields:
            current = self._view(owner_repo, number)
            if fields["state"] != current.state:
                cmd = "close" if fields["state"] == "closed" else "reopen"
                self._run(["issue", cmd, str(number), "--repo", owner_repo])

        edit_args: list[str] = []
        if "title" in fields:
            edit_args += ["--title", fields["title"]]
        if "body" in fields:
            edit_args += ["--body", fields["body"]]
        if "assignee" in fields and fields["assignee"]:
            edit_args += ["--add-assignee", fields["assignee"]]
        if "epic" in fields:
            if fields["epic"]:
                edit_args += ["--milestone", fields["epic"]]
            else:
                edit_args += ["--remove-milestone"]
        if "labels" in fields:
            current = self._view(owner_repo, number)
            new_labels = set(fields["labels"])
            old_labels = set(current.labels)
            for label in sorted(old_labels - new_labels):
                edit_args += ["--remove-label", label]
            for label in sorted(new_labels - old_labels):
                edit_args += ["--add-label", label]

        if edit_args:
            self._run(["issue", "edit", str(number), "--repo", owner_repo, *edit_args])
        return self._view(owner_repo, number)

    def comment(self, ref: str, body: str) -> None:
        scheme, ident = parse_ref(ref)
        if scheme != "gh":
            raise ValueError(f"GithubBackend cannot resolve ref with scheme {scheme!r}: {ref!r}")
        owner_repo, number = self._parse_ident(ident)
        self._run(["issue", "comment", str(number), "--repo", owner_repo, "--body", body])

    def group(self, refs: list[str], epic_name: str) -> None:
        """Map epic grouping onto a GitHub milestone (as today)."""
        owner_repos = {self._parse_ident(parse_ref(ref)[1])[0] for ref in refs}
        for owner_repo in owner_repos:
            if gh_meta.find_milestone(owner_repo, epic_name, runner=self.runner) is None:
                self._run(["api", f"repos/{owner_repo}/milestones", "-f", f"title={epic_name}"])
        for ref in refs:
            self.update(ref, {"epic": epic_name})

    def members(self, epic_name: str) -> list[Issue]:
        return self.list(query={"epic": epic_name})


# --- local backend ---------------------------------------------------------


_COMMENTS_HEADER = "## cw-comments"
_COMMENTS_RE = re.compile(rf"^{re.escape(_COMMENTS_HEADER)}\s*$", re.MULTILINE)


def _split_comments(full_body: str) -> tuple[str, str | None]:
    """Split a stored issue body into (body, comments-section-or-None).

    Comments live under a ``## cw-comments`` heading at the end of the file so
    they never leak into ``Issue.body`` (keeping body semantics identical to
    the GitHub backend, where comments are a separate resource).
    """
    match = _COMMENTS_RE.search(full_body)
    if not match:
        return full_body, None
    return full_body[: match.start()], full_body[match.start():]


class LocalBackend:
    """One markdown file per issue, YAML frontmatter, under docs/issues/."""

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.issues_dir = self.root / ISSUES_SUBDIR

    def _path_for_id(self, issue_id: int) -> Path:
        return self.issues_dir / f"{issue_id:04d}.md"

    def _ref_for_id(self, issue_id: int) -> str:
        rel = (ISSUES_SUBDIR / f"{issue_id:04d}.md").as_posix()
        return f"local:{rel}"

    def _resolve_path(self, ref: str) -> Path:
        scheme, ident = parse_ref(ref)
        if scheme != "local":
            raise ValueError(f"LocalBackend cannot resolve ref with scheme {scheme!r}: {ref!r}")
        ident_path = Path(ident)
        if ident_path.is_absolute():
            raise ValueError(f"local ref must be repo-relative, got absolute path: {ref!r}")
        resolved = (self.root / ident_path).resolve()
        issues_root = self.issues_dir.resolve()
        if not resolved.is_relative_to(issues_root):
            raise ValueError(
                f"local ref escapes {ISSUES_SUBDIR.as_posix()}/: {ref!r}"
            )
        return resolved

    def _read(self, path: Path) -> Issue:
        if not path.is_file():
            raise FileNotFoundError(f"no local issue at {path}")
        data, full_body = _parse_frontmatter(path.read_text())
        body, _ = _split_comments(full_body)
        rel = path.relative_to(self.root).as_posix()
        return Issue(
            ref=f"local:{rel}",
            title=data.get("title", ""),
            body=body.rstrip("\n"),
            state=data.get("state", "open"),
            labels=list(data.get("labels") or []),
            assignee=data.get("assignee"),
            epic=data.get("epic"),
            url_or_path=str(path),
        )

    def get(self, ref: str) -> Issue:
        return self._read(self._resolve_path(ref))

    def list(self, query: str | dict[str, Any] | None = None) -> list[Issue]:
        if not self.issues_dir.is_dir():
            return []
        issues = [self._read(path) for path in sorted(self.issues_dir.glob("*.md"))]
        return [issue for issue in issues if _matches_query(issue, query)]

    def _next_id(self) -> int:
        if not self.issues_dir.is_dir():
            return 1
        ids = [int(p.stem) for p in self.issues_dir.glob("*.md") if p.stem.isdigit()]
        return max(ids, default=0) + 1

    def create(self, draft: IssueDraft) -> str:
        self.issues_dir.mkdir(parents=True, exist_ok=True)
        issue_id = self._next_id()
        path = self._path_for_id(issue_id)
        frontmatter = {
            "id": issue_id,
            "title": draft.title,
            "state": "open",
            "labels": list(draft.labels),
            "epic": draft.epic,
            "assignee": draft.assignee,
        }
        path.write_text(_dump_frontmatter(frontmatter) + "\n\n" + (draft.body or "") + "\n")
        return self._ref_for_id(issue_id)

    def update(self, ref: str, fields: dict[str, Any]) -> Issue:
        _validate_update_fields(fields)
        fields = dict(fields)
        path = self._resolve_path(ref)
        data, full_body = _parse_frontmatter(path.read_text())
        body, comments = _split_comments(full_body)
        if "body" in fields:
            body = fields.pop("body")
        for key in ("title", "state", "labels", "epic", "assignee"):
            if key in fields:
                data[key] = fields[key]
        new_full = body.rstrip("\n")
        if comments:
            new_full += "\n\n" + comments.rstrip("\n")
        path.write_text(_dump_frontmatter(data) + "\n\n" + new_full + "\n")
        return self._read(path)

    def comment(self, ref: str, body: str) -> None:
        path = self._resolve_path(ref)
        data, full_body = _parse_frontmatter(path.read_text())
        existing_body, comments = _split_comments(full_body)
        comments = (comments or _COMMENTS_HEADER).rstrip("\n")
        comments += f"\n\n---\n{body}"
        new_full = existing_body.rstrip("\n") + "\n\n" + comments
        path.write_text(_dump_frontmatter(data) + "\n\n" + new_full + "\n")

    def group(self, refs: list[str], epic_name: str) -> None:
        for ref in refs:
            self.update(ref, {"epic": epic_name})

    def members(self, epic_name: str) -> list[Issue]:
        return [issue for issue in self.list() if issue.epic == epic_name]


Backend = GithubBackend | LocalBackend


# --- backend resolution ------------------------------------------------------


DEFAULT_CW_CONFIG = Path.home() / ".chief-wiggum" / "config.json"


def resolve_backend_name(repo_root: Path | str, *, cw_config: Path | None = None) -> str:
    """Resolve which backend a target repo uses.

    1. ``<repo_root>/docs/cw/tracker.json``'s ``"backend"`` key.
    2. CW-side fallback: ``~/.chief-wiggum/config.json``'s ``tracker.backend``.
    3. Default: ``"github"`` (today's behavior).
    """
    config_path = Path(repo_root) / "docs" / "cw" / "tracker.json"
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text())
            name = data.get("backend")
            if name:
                return name
        except (json.JSONDecodeError, OSError):
            pass

    cw_config = cw_config if cw_config is not None else DEFAULT_CW_CONFIG
    if cw_config.is_file():
        try:
            data = json.loads(cw_config.read_text())
            name = (data.get("tracker") or {}).get("backend")
            if name:
                return name
        except (json.JSONDecodeError, OSError):
            pass

    return "github"


def get_tracker(
    target: str, *, repo_root: Path | str | None = None, runner: Runner | None = None
) -> Backend:
    """Build the resolved backend for a target repo.

    ``target`` is the ``owner/repo`` string used for GitHub operations.
    ``repo_root`` is the local checkout used to read ``docs/cw/tracker.json``
    and (for the local backend) to store issue files; defaults to cwd.
    """
    root = Path(repo_root).resolve() if repo_root else Path.cwd()
    backend_name = resolve_backend_name(root)
    if backend_name == "github":
        return GithubBackend(target, runner=runner)
    if backend_name == "local":
        return LocalBackend(root)
    raise NotImplementedError(
        f"tracker backend {backend_name!r} is not implemented yet "
        "(only 'github' and 'local' ship in this ticket; see docs/tracker.md)"
    )


def _backend_for_ref(
    scheme: str, ident: str, repo_root: str | None, *, runner: Runner | None = None
) -> Backend:
    """Build the backend a fully-qualified ref names, without config resolution."""
    if scheme == "gh":
        owner_repo, _ = GithubBackend._parse_ident(ident)
        return GithubBackend(owner_repo, runner=runner)
    if scheme == "local":
        root = Path(repo_root).resolve() if repo_root else Path.cwd()
        return LocalBackend(root)
    raise NotImplementedError(
        f"backend for scheme {scheme!r} is not implemented yet (see docs/tracker.md)"
    )


# --- CLI ---------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tracker-agnostic issue interface")
    parser.add_argument(
        "--repo-root",
        help="Local target-repo root for backend config/storage (default: cwd)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "backend",
        help="Print the resolved backend name for --repo-root (github/local/...)",
    )

    p_get = sub.add_parser("get", help="Fetch a single issue by ref")
    p_get.add_argument("ref")

    p_list = sub.add_parser("list", help="List issues for a target repo")
    p_list.add_argument("target", help="owner/repo (github) or local repo path")
    p_list.add_argument("--query", help="Substring filter over title/body")
    p_list.add_argument("--epic", help="Filter to issues in this epic")

    p_create = sub.add_parser("create", help="Create a new issue")
    p_create.add_argument("target")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--body", default="")
    p_create.add_argument("--label", action="append", default=[], dest="labels")
    p_create.add_argument("--assignee")
    p_create.add_argument("--epic")

    p_update = sub.add_parser("update", help="Update fields on an issue")
    p_update.add_argument("ref")
    p_update.add_argument(
        "--set", action="append", default=[], dest="sets", metavar="KEY=VALUE",
        help="e.g. --set state=closed (repeatable); labels value is comma-separated",
    )

    p_comment = sub.add_parser("comment", help="Add a comment to an issue")
    p_comment.add_argument("ref")
    p_comment.add_argument("body")

    p_group = sub.add_parser("group", help="Assign refs to an epic")
    p_group.add_argument("epic")
    p_group.add_argument("refs", nargs="+")

    p_members = sub.add_parser("members", help="List issues in an epic")
    p_members.add_argument("target")
    p_members.add_argument("epic")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = args.repo_root

    try:
        if args.command == "backend":
            root = Path(repo_root).resolve() if repo_root else Path.cwd()
            print(resolve_backend_name(root))

        elif args.command == "get":
            scheme, ident = parse_ref(args.ref)
            backend = _backend_for_ref(scheme, ident, repo_root)
            print(json.dumps(backend.get(args.ref).to_dict(), indent=2))

        elif args.command == "list":
            backend = get_tracker(args.target, repo_root=repo_root)
            query: str | dict[str, Any] | None = args.query
            if args.epic:
                query = {"epic": args.epic}
            issues = backend.list(query)
            print(json.dumps([i.to_dict() for i in issues], indent=2))

        elif args.command == "create":
            backend = get_tracker(args.target, repo_root=repo_root)
            draft = IssueDraft(
                title=args.title, body=args.body, labels=args.labels,
                assignee=args.assignee, epic=args.epic,
            )
            print(backend.create(draft))

        elif args.command == "update":
            scheme, ident = parse_ref(args.ref)
            backend = _backend_for_ref(scheme, ident, repo_root)
            fields: dict[str, Any] = {}
            for kv in args.sets:
                key, _, value = kv.partition("=")
                fields[key] = value.split(",") if key == "labels" else value
            print(json.dumps(backend.update(args.ref, fields).to_dict(), indent=2))

        elif args.command == "comment":
            scheme, ident = parse_ref(args.ref)
            backend = _backend_for_ref(scheme, ident, repo_root)
            backend.comment(args.ref, args.body)

        elif args.command == "group":
            scheme, ident = parse_ref(args.refs[0])
            backend = _backend_for_ref(scheme, ident, repo_root)
            backend.group(args.refs, args.epic)

        elif args.command == "members":
            backend = get_tracker(args.target, repo_root=repo_root)
            issues = backend.members(args.epic)
            print(json.dumps([i.to_dict() for i in issues], indent=2))

        else:  # pragma: no cover - argparse enforces valid subcommands
            print(f"Unknown command: {args.command}", file=sys.stderr)
            return 1

    except (ValueError, FileNotFoundError, NotImplementedError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Error: gh command failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
