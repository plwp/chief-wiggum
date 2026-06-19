"""GitHub issue / milestone / dependency metadata client.

The command prompts embed repeated ``gh issue list``, ``gh issue view``,
``gh api repos/.../milestones`` and ``gh pr`` calls, plus ad-hoc parsing of the
machine-readable ``<!-- DEPENDENCIES ... -->`` block that ``/plan-epic`` writes
into milestone descriptions. That dependency block is a contract the wave
planner depends on, so it should be parsed by tested code rather than prose.

This module keeps the pure parsing/normalization (dependency block, JSON ->
dataclass) separate from the ``gh`` transport so the logic is unit-testable
without network access. A ``runner`` callable is injectable for the gh-backed
helpers.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

Runner = Callable[..., subprocess.CompletedProcess]


# --- dataclasses ------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    state: str = "OPEN"
    labels: tuple[str, ...] = ()
    milestone: str | None = None
    body: str = ""

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "state": self.state,
            "labels": list(self.labels),
            "milestone": self.milestone,
            "body": self.body,
        }


@dataclass(frozen=True)
class Milestone:
    title: str
    description: str = ""
    number: int | None = None
    open_issues: int = 0
    closed_issues: int = 0

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "number": self.number,
            "open_issues": self.open_issues,
            "closed_issues": self.closed_issues,
        }


@dataclass(frozen=True)
class PullRequestSummary:
    number: int
    title: str
    state: str = "OPEN"
    head_ref: str | None = None
    base_ref: str | None = None

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "state": self.state,
            "head_ref": self.head_ref,
            "base_ref": self.base_ref,
        }


@dataclass
class DependencyGraphMetadata:
    """Adjacency list parsed from a milestone ``<!-- DEPENDENCIES -->`` block.

    ``edges[n]`` is the list of issue numbers that ``#n`` depends on (blockers).
    Parsing never raises: malformed input is reported via ``warnings`` so the
    caller can degrade gracefully instead of crashing a workflow.
    """

    edges: dict[int, list[int]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_block(self) -> bool:
        return not any(w.startswith("missing DEPENDENCIES block") for w in self.warnings)

    def to_dict(self) -> dict:
        return {
            "edges": {str(k): v for k, v in self.edges.items()},
            "warnings": list(self.warnings),
            "has_block": self.has_block,
        }


# --- pure parsing -----------------------------------------------------------

_BLOCK_RE = re.compile(r"<!--\s*DEPENDENCIES\b(.*?)-->", re.DOTALL | re.IGNORECASE)
_LINE_RE = re.compile(r"^#?(\d+)\s*:\s*\[(.*)\]$")
_DEP_RE = re.compile(r"#?(\d+)")


def parse_dependency_block(description: str | None) -> DependencyGraphMetadata:
    """Parse the ``<!-- DEPENDENCIES ... -->`` block from a milestone body.

    Format (one line per ticket, inside an HTML comment)::

        <!-- DEPENDENCIES
        #42: []
        #43: [#42]
        -->

    Empty brackets mean no dependencies. Malformed lines are skipped with a
    warning; a missing block yields empty edges plus a warning.
    """
    meta = DependencyGraphMetadata()
    if not description:
        meta.warnings.append("missing DEPENDENCIES block: empty description")
        return meta

    match = _BLOCK_RE.search(description)
    if not match:
        meta.warnings.append("missing DEPENDENCIES block: not found in description")
        return meta

    body = match.group(1)
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        line_match = _LINE_RE.match(line)
        if not line_match:
            meta.warnings.append(f"malformed dependency line: {raw.strip()!r}")
            continue
        node = int(line_match.group(1))
        deps_text = line_match.group(2).strip()
        deps = [int(m.group(1)) for m in _DEP_RE.finditer(deps_text)] if deps_text else []
        if node in meta.edges:
            meta.warnings.append(f"duplicate dependency entry for #{node}")
        # Self-dependency is meaningless; drop it but warn.
        cleaned = []
        for d in deps:
            if d == node:
                meta.warnings.append(f"#{node} lists itself as a dependency")
                continue
            cleaned.append(d)
        meta.edges[node] = cleaned

    return meta


def format_dependency_block(edges: dict[int, Iterable[int]]) -> str:
    """Render an adjacency list back into a ``<!-- DEPENDENCIES -->`` block.

    Inverse of :func:`parse_dependency_block` so ``/plan-epic`` can generate the
    canonical block via tested code instead of hand-formatting it in prose.
    Nodes and their dependencies are emitted in ascending order for stable diffs.
    """
    lines = ["<!-- DEPENDENCIES"]
    for node in sorted(edges):
        deps = sorted(set(edges[node]))
        rendered = ", ".join(f"#{d}" for d in deps)
        lines.append(f"#{node}: [{rendered}]")
    lines.append("-->")
    return "\n".join(lines)


def issue_from_json(data: dict[str, Any]) -> Issue:
    """Normalize a ``gh issue`` JSON object into an :class:`Issue`."""
    labels = data.get("labels") or []
    label_names = tuple(
        lbl["name"] if isinstance(lbl, dict) else str(lbl) for lbl in labels
    )
    milestone = data.get("milestone")
    if isinstance(milestone, dict):
        milestone = milestone.get("title")
    return Issue(
        number=int(data["number"]),
        title=data.get("title", ""),
        state=str(data.get("state", "OPEN")).upper(),
        labels=label_names,
        milestone=milestone,
        body=data.get("body", "") or "",
    )


def milestone_from_json(data: dict[str, Any]) -> Milestone:
    """Normalize a milestone JSON object (gh api or gh issue view) into one shape."""
    return Milestone(
        title=data.get("title", ""),
        description=data.get("description", "") or "",
        number=data.get("number"),
        open_issues=int(data.get("open_issues", 0) or 0),
        closed_issues=int(data.get("closed_issues", 0) or 0),
    )


def pr_from_json(data: dict[str, Any]) -> PullRequestSummary:
    return PullRequestSummary(
        number=int(data["number"]),
        title=data.get("title", ""),
        state=str(data.get("state", "OPEN")).upper(),
        head_ref=data.get("headRefName"),
        base_ref=data.get("baseRefName"),
    )


# --- gh transport -----------------------------------------------------------


def _run_gh(args: list[str], runner: Runner) -> str:
    result = runner(
        ["gh", *args], capture_output=True, text=True, check=True, timeout=60
    )
    return result.stdout


def list_issues(
    repo: str, *, state: str = "open", limit: int = 200, runner: Runner = subprocess.run
) -> list[Issue]:
    out = _run_gh(
        [
            "issue", "list", "--repo", repo, "--state", state,
            "--limit", str(limit),
            "--json", "number,title,state,labels,milestone,body",
        ],
        runner,
    )
    return [issue_from_json(d) for d in json.loads(out or "[]")]


def view_issue(repo: str, number: int, *, runner: Runner = subprocess.run) -> Issue:
    out = _run_gh(
        [
            "issue", "view", str(number), "--repo", repo,
            "--json", "number,title,state,labels,milestone,body",
        ],
        runner,
    )
    return issue_from_json(json.loads(out))


def list_milestones(repo: str, *, runner: Runner = subprocess.run) -> list[Milestone]:
    out = _run_gh(
        ["api", f"repos/{repo}/milestones", "--paginate"],
        runner,
    )
    return [milestone_from_json(d) for d in json.loads(out or "[]")]


def find_milestone(
    repo: str, title: str, *, runner: Runner = subprocess.run
) -> Milestone | None:
    for milestone in list_milestones(repo, runner=runner):
        if milestone.title == title:
            return milestone
    return None


def dependency_graph(
    repo: str, milestone_title: str, *, runner: Runner = subprocess.run
) -> DependencyGraphMetadata:
    """Fetch a milestone and parse its dependency block."""
    milestone = find_milestone(repo, milestone_title, runner=runner)
    if milestone is None:
        meta = DependencyGraphMetadata()
        meta.warnings.append(
            f"missing DEPENDENCIES block: milestone {milestone_title!r} not found"
        )
        return meta
    return parse_dependency_block(milestone.description)


def issues_as_dicts(issues: Iterable[Issue]) -> list[dict]:
    return [i.to_dict() for i in issues]
