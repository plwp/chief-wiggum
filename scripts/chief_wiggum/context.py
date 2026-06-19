"""Shared workflow context resolver.

Nearly every Chief Wiggum command repeats the same setup: find the install
home, create a session temp directory, resolve the target repo and its default
branch, parse an optional issue number, and derive epic slug/artifact paths.
Doing this in prose/shell per command causes temp collisions, wrong-repo
writes, and hardcoded branch assumptions.

`WorkflowContext` resolves all of it in one tested place. Pure parsing is kept
separate from side-effecting resolution (temp creation, repo cloning, default
branch lookup) so the logic is unit-testable without network or filesystem
surprises.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import env
import repo


@dataclass(frozen=True)
class TargetRef:
    """A parsed ``owner/repo`` or ``owner/repo#42`` reference."""

    owner: str
    repo: str
    issue: int | None = None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_target(ref: str) -> TargetRef:
    """Parse ``owner/repo`` or ``owner/repo#42`` into a :class:`TargetRef`.

    Reuses ``repo._parse_owner_repo`` for owner/repo validation (path-traversal
    safety) and additionally parses an optional ``#<issue>`` suffix.
    """
    owner, name = repo._parse_owner_repo(ref)
    issue: int | None = None
    if "#" in ref:
        suffix = ref.split("#", 1)[1].strip()
        if suffix:
            try:
                issue = int(suffix)
            except ValueError as exc:
                raise ValueError(f"invalid issue number in {ref!r}: {suffix!r}") from exc
            if issue <= 0:
                raise ValueError(f"issue number must be positive in {ref!r}")
    return TargetRef(owner=owner, repo=name, issue=issue)


def detect_default_branch(
    repo_path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    fallback: str = "main",
) -> str:
    """Return the repo's default branch, falling back to ``main``.

    Uses ``gh`` first (authoritative for the remote), then a local
    ``origin/HEAD`` symbolic ref, then the fallback. Never raises: branch
    detection should degrade gracefully rather than abort a workflow.
    """
    try:
        result = runner(
            ["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        branch = (result.stdout or "").strip()
        if result.returncode == 0 and branch:
            return branch
    except (subprocess.SubprocessError, OSError):
        pass

    try:
        result = runner(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        ref = (result.stdout or "").strip()
        if result.returncode == 0 and ref:
            # ref looks like "origin/main"
            return ref.split("/", 1)[-1]
    except (subprocess.SubprocessError, OSError):
        pass

    return fallback


@dataclass
class WorkflowContext:
    """Resolved context shared by every command workflow."""

    home: Path
    tmp: Path
    default_branch: str
    target: TargetRef | None = None
    repo_path: Path | None = None
    issue: int | None = None
    epic_name: str | None = None
    epic_slug: str | None = None
    epic_dir: Path | None = None

    def ticket_tmp(self) -> Path:
        """Per-ticket temp subdirectory, created on demand.

        `/implement` runs multiple tickets in one session and needs collision
        free per-ticket scratch space (see CLAUDE.md temp-dir guidance).
        """
        sub = self.tmp / (str(self.issue) if self.issue is not None else "_")
        sub.mkdir(parents=True, exist_ok=True)
        return sub

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("home", "tmp", "repo_path", "epic_dir"):
            value = data.get(key)
            data[key] = str(value) if value is not None else None
        if self.target is not None:
            data["target"] = self.target.slug
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def shell_exports(self) -> str:
        """Emit ``export NAME=...`` lines for command-prompt shell snippets."""
        lines = [
            f"export CW_HOME={env.shell_quote(str(self.home))}",
            f"export CW_TMP={env.shell_quote(str(self.tmp))}",
            f"export DEFAULT_BRANCH={env.shell_quote(self.default_branch)}",
        ]
        if self.repo_path is not None:
            lines.append(f"export TARGET_REPO={env.shell_quote(str(self.repo_path))}")
        if self.target is not None:
            lines.append(f"export TARGET_SLUG={env.shell_quote(self.target.slug)}")
        if self.issue is not None:
            lines.append(f"export ISSUE_NUMBER={self.issue}")
        if self.epic_slug is not None:
            lines.append(f"export EPIC_SLUG={env.shell_quote(self.epic_slug)}")
            lines.append(f'export EPIC_DIR="$TARGET_REPO/docs/epics/{self.epic_slug}"')
        return "\n".join(lines)


def resolve(
    target: str | None = None,
    *,
    epic: str | None = None,
    tmp: Path | None = None,
    resolve_repo_path: bool = True,
    detect_branch: bool = True,
    home: Path | None = None,
    branch_detector: Callable[[Path], str] = detect_default_branch,
) -> WorkflowContext:
    """Build a :class:`WorkflowContext`.

    Args:
        target: ``owner/repo`` or ``owner/repo#42`` (optional for home-only use).
        epic: epic / milestone name; slugged into ``docs/epics/<slug>``.
        tmp: reuse an existing session temp dir instead of creating a new one.
        resolve_repo_path: clone/resolve the target repo to a local path.
        detect_branch: look up the repo default branch (else assume ``main``).
        home: override the chief-wiggum home (else discovered).
        branch_detector: injectable for tests.
    """
    home = home or env.find_home()
    tmp = tmp or env.create_tmp()

    target_ref = parse_target(target) if target else None
    repo_path: Path | None = None
    default_branch = "main"

    if target_ref is not None and resolve_repo_path:
        repo_path = repo.resolve_repo(target_ref.slug)
        if detect_branch:
            default_branch = branch_detector(repo_path)

    epic_slug = env.slugify(epic) if epic else None
    epic_dir: Path | None = None
    if epic_slug and repo_path is not None:
        epic_dir = repo_path / "docs" / "epics" / epic_slug

    return WorkflowContext(
        home=home,
        tmp=tmp,
        default_branch=default_branch,
        target=target_ref,
        repo_path=repo_path,
        issue=target_ref.issue if target_ref else None,
        epic_name=epic,
        epic_slug=epic_slug,
        epic_dir=epic_dir,
    )
