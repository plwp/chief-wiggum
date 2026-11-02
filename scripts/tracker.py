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
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifacts as _meta_location  # noqa: E402 - meta-location resolver (#213)
from chief_wiggum import ai_disclosure  # noqa: E402
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
    # Who asked for this work and on what authority (chief-wiggum#498). The
    # GitHub backend does not render it (no behaviour change for today's
    # skills); a task-service backend carries it on create so an
    # agent-class task is shown as an agent proposal.
    provenance: Provenance | None = None


VALID_STATES = ("open", "closed")

# --- work-tracking verbs (chief-wiggum#498) ----------------------------------

# The four provenance classes a decision (or a created task) may carry. Order
# is strongest first; "agent" is the weakest and is always shown as such.
PROVENANCE_CLASSES = ("legal", "human", "customer", "agent")

# CW's tracker-neutral work statuses for set_status(). "done" is not here:
# finishing work is complete(), which closes the issue.
STATUSES = ("ready", "in_progress", "blocked")

# Lease states, deliberately the same vocabulary as the DAG contract's
# ``lease_state`` (schemas/dag/v1/vocabulary.schema.json); a test pins it.
LEASE_STATES = ("unclaimed", "active", "released", "expired", "revoked")


@dataclass(frozen=True)
class Provenance:
    """Where a requirement or ruling comes from.

    ``klass`` is one of PROVENANCE_CLASSES. ``source`` cites it: the
    instrument and provision for legal, who decided and their quoted words
    for human, the REQ id and source line for customer, the proposing agent
    and date for agent. ``actor`` is the agent that recorded it, when one did.
    """

    klass: str
    source: str
    actor: str | None = None

    def __post_init__(self) -> None:
        if self.klass not in PROVENANCE_CLASSES:
            raise ValueError(
                f"invalid provenance class {self.klass!r}: must be one of "
                + ", ".join(PROVENANCE_CLASSES)
            )
        if not (self.source or "").strip():
            raise ValueError("provenance source is required: cite where this comes from")

    @property
    def is_agent_proposal(self) -> bool:
        return self.klass == "agent"

    def to_dict(self) -> dict:
        return {"class": self.klass, "source": self.source, "actor": self.actor}


@dataclass(frozen=True)
class Decision:
    """A ruling logged against one issue, with its provenance."""

    what: str
    decided_by: str
    provenance: Provenance
    channel: str = ""
    approval_words: str | None = None
    relayed_by: str | None = None

    def __post_init__(self) -> None:
        if not (self.what or "").strip():
            raise ValueError("a decision needs its text (what was decided)")
        if not (self.decided_by or "").strip():
            raise ValueError("a decision needs decided_by")

    def to_dict(self) -> dict:
        return {
            "what": self.what,
            "decided_by": self.decided_by,
            "channel": self.channel,
            "approval_words": self.approval_words,
            "relayed_by": self.relayed_by,
            "provenance_class": self.provenance.klass,
            "provenance_source": self.provenance.source,
        }


@dataclass
class Lease:
    """A claim on one issue, held by one agent.

    Field-for-field this is the tracker's view of the DAG contract's
    ``lease_record`` (docs/dag-contract.md); see ``to_dag_lease_fields``.
    """

    ref: str
    holder: str | None
    state: str = "active"
    lease_id: str | None = None
    run_id: str | None = None
    expires_at: str | None = None
    epoch: int = 0
    fencing_token: str | None = None

    def __post_init__(self) -> None:
        if self.state not in LEASE_STATES:
            raise ValueError(
                f"invalid lease state {self.state!r}: must be one of {', '.join(LEASE_STATES)}"
            )

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "holder": self.holder,
            "state": self.state,
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "expires_at": self.expires_at,
            "epoch": self.epoch,
            "fencing_token": self.fencing_token,
        }

    def to_dag_lease_fields(self) -> dict:
        """The lease-record fields this lease supplies (docs/tracker.md).

        ``execution_node_id`` is not ours to mint: the DAG compiles execution
        nodes from intent nodes, and an intent node pins the tracker ``ref``.
        So the ref is returned as ``intent_ref`` for the compiler to resolve,
        and the run is the attempt.
        """
        return {
            "lease_id": self.lease_id,
            "intent_ref": self.ref,
            "attempt_id": self.run_id,
            "epoch": self.epoch,
            "state": self.state,
            "fencing_token": self.fencing_token or self.lease_id or self.run_id,
        }


_DECISION_BLOCK = "<!-- CW-DECISION"


def render_decision(decision: Decision) -> str:
    """A decision as an issue comment: readable prose plus a parseable block."""
    prov = decision.provenance
    label = "agent proposal" if prov.is_agent_proposal else prov.klass
    lines = [
        f"**Decision** ({label}): {decision.what}",
        "",
        f"- Decided by: {decision.decided_by}"
        + (f", via {decision.channel}" if decision.channel else ""),
    ]
    if decision.relayed_by:
        lines.append(f"- Relayed by: {decision.relayed_by}")
    if decision.approval_words:
        lines.append(f"- Words: \"{decision.approval_words}\"")
    lines.append(f"- Provenance: {prov.klass}, {prov.source}")
    lines += ["", _DECISION_BLOCK, json.dumps(decision.to_dict(), sort_keys=True), "-->"]
    return "\n".join(lines)


def parse_decision_block(text: str) -> dict | None:
    """Read back the machine block render_decision() wrote, or None."""
    match = re.search(r"<!--\s*CW-DECISION\s*\n(.*?)\n-->", text or "", re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def render_status(status: str, holder: str, note: str) -> str:
    return f"**Status: {status}** ({holder}): {note}"


def _validate_status(status: str) -> None:
    if status not in STATUSES:
        raise ValueError(
            f"invalid status {status!r}: must be one of {', '.join(STATUSES)}"
            " (finish work with complete())"
        )

# --- dependency verb group (chief-wiggum#371) --------------------------------

# Capability names for feature detection. A caller asks what a backend can do
# rather than probing with try/except, so an unsupported verb is a planned
# branch instead of an exception path.
CAP_DEPENDENCIES = "dependencies"   # link / unlink / deps
CAP_READY = "ready"                 # ready()
CAP_CLAIM = "claim"                 # claim / release, with real mutual exclusion


class UnsupportedCapability(NotImplementedError):
    """A backend cannot honour a verb.

    Raised rather than returning a falsy no-op: a claim that silently fails to
    exclude is worse than no claim at all, because two workers then believe
    they hold it.
    """


@dataclass
class Dependencies:
    """Edges in both directions for one issue."""

    blocked_by: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"blocked_by": list(self.blocked_by), "blocks": list(self.blocks)}


class DependencyCycle(ValueError):
    """A link that would make ready() unsatisfiable for the whole cycle."""


_BLOCKED_BY_RE = re.compile(
    r"<!--\s*BLOCKED-BY\s*\n(?P<body>.*?)\n?-->", re.DOTALL | re.IGNORECASE
)
_BLOCKED_BY_HEADER = "<!-- BLOCKED-BY"


def parse_blocked_by_block(body: str | None) -> list[str]:
    """Read the per-issue BLOCKED-BY block from an issue body.

    Deliberately distinct from the milestone-level DEPENDENCIES block in
    chief_wiggum.github: that one describes a whole epic's graph in one place,
    this one records the edges of a single issue. Parsing one with the other's
    reader would silently mix epic-wide and per-issue scope.

    Format, one ref per line inside an HTML comment::

        <!-- BLOCKED-BY
        acme/app#42
        -->
    """
    if not body:
        return []
    match = _BLOCKED_BY_RE.search(body)
    if not match:
        return []
    refs = []
    for line in match.group("body").splitlines():
        candidate = line.strip().lstrip("-").strip()
        if candidate:
            refs.append(candidate)
    return refs


def write_blocked_by_block(body: str, blockers: list[str]) -> str:
    """Replace (or remove) the BLOCKED-BY block, leaving the rest of the body."""
    stripped = _BLOCKED_BY_RE.sub("", body or "").rstrip()
    if not blockers:
        return stripped
    block = _BLOCKED_BY_HEADER + "\n" + "\n".join(blockers) + "\n-->"
    return (stripped + "\n\n" + block).lstrip("\n")


def _would_cycle(
    ref: str, blocker: str, blocked_by_of: Callable[[str], list[str]]
) -> list[str] | None:
    """Return the path if adding ref -> blocker closes a cycle.

    Checked at link time rather than discovered at schedule time: a cycle makes
    every node in it permanently unready, and ready() returning a silently
    shorter list is the failure mode this prevents.
    """
    # No special case for ref == blocker: the walk below starts at `blocker`
    # and its first comparison already matches, so a separate self-link branch
    # would be unreachable.
    stack = [(blocker, [ref, blocker])]
    seen = set()
    while stack:
        node, path = stack.pop()
        if node == ref:
            return path
        if node in seen:
            continue
        seen.add(node)
        for parent in blocked_by_of(node):
            stack.append((parent, path + [parent]))
    return None



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

KNOWN_SCHEMES = ("gh", "local", "task", "obsidian", "jira")

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

    # --- dependency verb group (chief-wiggum#371) ---------------------------

    @staticmethod
    def capabilities() -> frozenset[str]:
        """Dependencies and readiness are emulated; claim is NOT supported.

        GitHub offers no compare-and-set on assignee, so an assignee-based
        claim cannot exclude two workers racing for the same issue. A claim
        that does not exclude is worse than none, because both workers then
        proceed believing they hold it, so this backend declines the verb
        instead of emulating it.
        """
        return frozenset({CAP_DEPENDENCIES, CAP_READY})

    def _blocked_by(self, ref: str) -> list[str]:
        return parse_blocked_by_block(self.get(ref).body)

    def _blocked_by_or_empty(self, ref: str) -> list[str]:
        """Edges of a possibly-absent issue, for cycle traversal. See LocalBackend."""
        try:
            return self._blocked_by(ref)
        except Exception:  # noqa: BLE001 - an unreadable issue has no edges
            return []

    def link(self, ref: str, blocked_by: str) -> None:
        cycle = _would_cycle(ref, blocked_by, self._blocked_by_or_empty)
        if cycle is not None:
            raise DependencyCycle(
                "link would create a dependency cycle: " + " -> ".join(cycle)
            )
        issue = self.get(ref)
        blockers = parse_blocked_by_block(issue.body)
        if blocked_by in blockers:
            return
        blockers.append(blocked_by)
        self.update(ref, {"body": write_blocked_by_block(issue.body, blockers)})

    def unlink(self, ref: str, blocked_by: str) -> None:
        issue = self.get(ref)
        blockers = [item for item in parse_blocked_by_block(issue.body) if item != blocked_by]
        self.update(ref, {"body": write_blocked_by_block(issue.body, blockers)})

    def deps(self, ref: str) -> Dependencies:
        blocked_by = self._blocked_by(ref)
        blocks = [
            issue.ref for issue in self.list()
            if ref in parse_blocked_by_block(issue.body)
        ]
        return Dependencies(blocked_by=blocked_by, blocks=sorted(blocks))

    def ready(self, query: str | dict[str, Any] | None = None) -> list[Issue]:
        everything = self.list()
        states = {issue.ref: issue.state for issue in everything}
        result = []
        for issue in everything:
            if issue.state != "open" or not _matches_query(issue, query):
                continue
            blockers = parse_blocked_by_block(issue.body)
            if any(states.get(blocker) == "open" for blocker in blockers):
                continue
            result.append(issue)
        return result

    def claim(self, ref: str, agent_id: str) -> bool:
        raise UnsupportedCapability(
            "the github backend cannot claim atomically: GitHub has no"
            " compare-and-set on assignee, so an emulated claim would let two"
            " workers both believe they won. Use the wave lock, or a backend"
            " that reports CAP_CLAIM."
        )

    def release(self, ref: str, agent_id: str) -> bool:
        raise UnsupportedCapability(
            "the github backend does not support claim, so there is nothing to release"
        )

    def members(self, epic_name: str) -> list[Issue]:
        return self.list(query={"epic": epic_name})

    # --- work-tracking verbs (chief-wiggum#498) ------------------------------

    def _gh_ref(self, ref: str) -> tuple[str, int]:
        scheme, ident = parse_ref(ref)
        if scheme != "gh":
            raise ValueError(f"GithubBackend cannot resolve ref with scheme {scheme!r}: {ref!r}")
        return self._parse_ident(ident)

    # The JSON fields /implement reads at Step 2 (ticket context, #83).
    CONTEXT_FIELDS = "title,body,author,labels,assignees,milestone,comments"

    def read_context(self, ref: str) -> dict:
        """The issue as ``gh issue view --json`` returns it, comments included.

        Passed through unchanged: write_ticket_context.py parses this shape,
        and AC amendments live in the comments.
        """
        owner_repo, number = self._gh_ref(ref)
        return json.loads(self.raw_view(number, self.CONTEXT_FIELDS, repo=owner_repo))

    def set_status(self, ref: str, status: str, *, holder: str, note: str = "") -> None:
        """GitHub has no work status, so only a note is recorded, as a comment.

        No status label is invented: a label scheme the target never chose
        would be a behaviour change. A status with no note writes nothing.
        """
        _validate_status(status)
        if note:
            self.comment(ref, render_status(status, holder, note))

    def complete(self, ref: str, summary: str, *, holder: str | None = None) -> None:
        """Close with a closing comment, in one ``gh issue close --comment``."""
        owner_repo, number = self._gh_ref(ref)
        args = ["issue", "close", str(number), "--repo", owner_repo]
        if summary:
            args += ["--comment", summary]
        self._run(args)

    def log_decision(self, ref: str, decision: Decision) -> None:
        self.comment(ref, render_decision(decision))

    def lease(self, ref: str) -> Lease:
        raise UnsupportedCapability(
            "the github backend holds no claims, so it has no lease to report"
        )

    # --- raw transport for chief_wiggum.github (the only other gh-issue user)

    def raw_list(self, *, state: str, limit: int, fields: str, milestone: str | None = None) -> str:
        args = [
            "issue", "list", "--repo", self.repo, "--state", state,
            "--limit", str(limit), "--json", fields,
        ]
        if milestone:
            args += ["--milestone", milestone]
        return self._run(args)

    def raw_view(self, number: int, fields: str, *, repo: str | None = None) -> str:
        return self._run(
            ["issue", "view", str(number), "--repo", repo or self.repo, "--json", fields]
        )


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
    """One markdown file per issue, YAML frontmatter, under docs/issues/.

    Storage location is meta-location resolved, never assumed
    (chief-wiggum#266): ``root`` is the TARGET repo checkout, but issues are
    stored under ``<meta root>/issues`` — identical to
    ``<target>/docs/issues`` in embedded mode (the status quo), but under the
    sidecar meta root (``~/.chief-wiggum/meta/<owner>/<repo>/docs/issues``)
    on a sidecar-elected target, so a local-backend target never has to
    dirty its own tree to store issues.
    """

    def __init__(self, root: Path | str):
        target_repo = Path(root).resolve()
        resolver = _meta_location.Resolver.resolve(target_repo)
        # meta_root is "<base>/docs" in both modes (embedded: <target>/docs;
        # sidecar: <sidecar dir>/docs) — its parent is the base that
        # ISSUES_SUBDIR ("docs/issues") is relative to, so the SAME relative
        # arithmetic below yields <target>/docs/issues in embedded mode
        # (self.root == target_repo, unchanged) and the sidecar issues dir
        # in sidecar mode, with no other code path to diverge.
        self.root = resolver.meta_root.parent
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

    # --- dependency verb group (chief-wiggum#371) ---------------------------

    @staticmethod
    def capabilities() -> frozenset[str]:
        """Local supports everything: it owns its storage and its filesystem."""
        return frozenset({CAP_DEPENDENCIES, CAP_READY, CAP_CLAIM})

    def _blocked_by(self, ref: str) -> list[str]:
        path = self._resolve_path(ref)
        data, _ = _parse_frontmatter(path.read_text())
        return [str(item) for item in (data.get("blocked_by") or [])]

    def _write_blocked_by(self, ref: str, blockers: list[str]) -> None:
        path = self._resolve_path(ref)
        data, full_body = _parse_frontmatter(path.read_text())
        data["blocked_by"] = blockers
        path.write_text(_dump_frontmatter(data) + "\n\n" + full_body.lstrip("\n"))

    def _blocked_by_or_empty(self, ref: str) -> list[str]:
        """Edges of a possibly-absent issue, for cycle traversal.

        A ref that cannot be read has no edges. This mirrors ready()'s rule
        that a blocker which does not exist does not block, so linking to a
        not-yet-created issue is allowed rather than crashing the walk.
        """
        try:
            return self._blocked_by(ref)
        except (FileNotFoundError, ValueError):
            return []

    def link(self, ref: str, blocked_by: str) -> None:
        """Record that `ref` is blocked by `blocked_by`."""
        self._resolve_path(ref)
        cycle = _would_cycle(ref, blocked_by, self._blocked_by_or_empty)
        if cycle is not None:
            raise DependencyCycle(
                "link would create a dependency cycle: " + " -> ".join(cycle)
            )
        blockers = self._blocked_by(ref)
        if blocked_by not in blockers:
            blockers.append(blocked_by)
            self._write_blocked_by(ref, blockers)

    def unlink(self, ref: str, blocked_by: str) -> None:
        blockers = [item for item in self._blocked_by(ref) if item != blocked_by]
        self._write_blocked_by(ref, blockers)

    def deps(self, ref: str) -> Dependencies:
        self._resolve_path(ref)
        blocks = [
            issue.ref for issue in self.list()
            if ref in self._blocked_by(issue.ref)
        ]
        return Dependencies(blocked_by=self._blocked_by(ref), blocks=sorted(blocks))

    def ready(self, query: str | dict[str, Any] | None = None) -> list[Issue]:
        """Open issues with no OPEN blocker.

        A blocker that no longer exists does not block: a deleted issue would
        otherwise wedge its dependants forever with nothing to close.
        """
        issues = self.list(query)
        states = {issue.ref: issue.state for issue in self.list()}
        result = []
        for issue in issues:
            if issue.state != "open":
                continue
            if any(states.get(blocker) == "open" for blocker in self._blocked_by(issue.ref)):
                continue
            result.append(issue)
        return result

    def _claim_path(self, ref: str) -> Path:
        issue_path = self._resolve_path(ref)
        return issue_path.with_suffix(".claim")

    def claim(self, ref: str, agent_id: str) -> bool:
        """Atomically claim an issue. True if this agent now holds it.

        Exclusion comes from O_EXCL, not from a read-then-write: two workers
        racing here must not both be told they won.
        """
        claim_path = self._claim_path(ref)
        try:
            handle = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # Re-claiming what you already hold is idempotent, not a failure.
            return claim_path.read_text().strip() == agent_id
        with os.fdopen(handle, "w") as stream:
            stream.write(agent_id)
        self.update(ref, {"assignee": agent_id})
        return True

    def release(self, ref: str, agent_id: str) -> bool:
        """Release a claim. False if this agent does not hold it."""
        claim_path = self._claim_path(ref)
        if not claim_path.exists():
            return False
        if claim_path.read_text().strip() != agent_id:
            return False
        claim_path.unlink()
        self.update(ref, {"assignee": None})
        return True

    def claimant(self, ref: str) -> str | None:
        claim_path = self._claim_path(ref)
        return claim_path.read_text().strip() if claim_path.exists() else None

    # --- work-tracking verbs (chief-wiggum#498) ------------------------------

    def _comments(self, ref: str) -> list[str]:
        _, full_body = _parse_frontmatter(self._resolve_path(ref).read_text())
        _, section = _split_comments(full_body)
        if not section:
            return []
        parts = section[len(_COMMENTS_HEADER):].split("\n\n---\n")
        return [part.strip("\n") for part in parts if part.strip()]

    def read_context(self, ref: str) -> dict:
        """The same shape GithubBackend.read_context returns, from the file."""
        issue = self.get(ref)
        return {
            "title": issue.title,
            "body": issue.body,
            "author": {"login": ""},
            "labels": [{"name": label} for label in issue.labels],
            "assignees": [{"login": issue.assignee}] if issue.assignee else [],
            "milestone": {"title": issue.epic} if issue.epic else None,
            "comments": [
                {"author": {"login": ""}, "body": body} for body in self._comments(ref)
            ],
        }

    def set_status(self, ref: str, status: str, *, holder: str, note: str = "") -> None:
        _validate_status(status)
        if note:
            self.comment(ref, render_status(status, holder, note))

    def complete(self, ref: str, summary: str, *, holder: str | None = None) -> None:
        if summary:
            self.comment(ref, summary)
        self.update(ref, {"state": "closed"})

    def log_decision(self, ref: str, decision: Decision) -> None:
        self.comment(ref, render_decision(decision))

    def lease(self, ref: str) -> Lease:
        holder = self.claimant(ref)
        return Lease(ref=ref, holder=holder, state="active" if holder else "unclaimed")


Backend = GithubBackend | LocalBackend


# --- backend resolution ------------------------------------------------------


DEFAULT_CW_CONFIG = Path.home() / ".chief-wiggum" / "config.json"


def resolve_backend_name(repo_root: Path | str, *, cw_config: Path | None = None) -> str:
    """Resolve which backend a target repo uses.

    1. ``<meta root>/cw/tracker.json``'s ``"backend"`` key — meta-location
       resolved (chief-wiggum#266) via ``artifacts.Resolver``: identical to
       ``<repo_root>/docs/cw/tracker.json`` in embedded mode (today's
       behavior, unchanged), but the SIDECAR meta root on a sidecar-elected
       target, where the target tree never carries a ``docs/`` at all.
    2. CW-side fallback: ``~/.chief-wiggum/config.json``'s ``tracker.backend``.
    3. Default: ``"github"`` (today's behavior).
    """
    resolver = _meta_location.Resolver.resolve(Path(repo_root))
    config_path = resolver.meta_root / "cw" / "tracker.json"
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


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_mcp_config(repo_root: Path | str, *, cw_config: Path | None = None) -> dict:
    """The ``mcp`` block for the mcp backend (chief-wiggum#498).

    CW-side ``~/.chief-wiggum/config.json`` ``tracker.mcp`` first (where a
    machine-local server command belongs), then the target's
    ``<meta root>/cw/tracker.json`` ``mcp`` over it, key by key.
    """
    cw_config = cw_config if cw_config is not None else DEFAULT_CW_CONFIG
    merged: dict = {}
    if cw_config.is_file():
        merged.update((_read_json(cw_config).get("tracker") or {}).get("mcp") or {})
    resolver = _meta_location.Resolver.resolve(Path(repo_root))
    target_config = resolver.meta_root / "cw" / "tracker.json"
    if target_config.is_file():
        merged.update(_read_json(target_config).get("mcp") or {})
    return merged


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
    if backend_name == "mcp":
        import tracker_mcp  # lazy: it imports this module

        return tracker_mcp.build(target, resolve_mcp_config(root), runner=runner)
    raise NotImplementedError(
        f"tracker backend {backend_name!r} is not implemented yet "
        "(github, local and mcp ship; see docs/tracker.md)"
    )


def _backend_for_ref(
    scheme: str, ident: str, repo_root: str | None, *, runner: Runner | None = None
) -> Backend:
    """Build the backend a fully-qualified ref names.

    A ``gh:`` ref is GitHub unless the target is configured for the mcp
    backend, which then takes the write as well (GitHub first, the service
    mirrored). A ``task:`` ref names a task-service key and needs mcp.
    """
    if scheme in ("gh", "task"):
        root = Path(repo_root).resolve() if repo_root else Path.cwd()
        mcp = resolve_backend_name(root) == "mcp"
        if scheme == "task":
            if not mcp:
                raise ValueError(f"task:{ident} names a task-service key; the target is not on the mcp backend")
            # Which GitHub repo the key belongs to: a GitHub-shaped key says;
            # a local#<n> key belongs to the target (mcp.repo, else origin).
            prefix = ident.partition("#")[0]
            owner_repo = prefix if "/" in prefix else (
                resolve_mcp_config(root).get("repo") or _meta_location.derive_target_id(root)
            )
            if "/" not in owner_repo or owner_repo.startswith("local/"):
                raise ValueError(
                    f"task:{ident}: cannot tell the target's GitHub repo; set mcp.repo (owner/repo)"
                )
        else:
            owner_repo, _ = GithubBackend._parse_ident(ident)
        if mcp:
            return get_tracker(owner_repo, repo_root=root, runner=runner)
        return GithubBackend(owner_repo, runner=runner)
    if scheme == "local":
        root = Path(repo_root).resolve() if repo_root else Path.cwd()
        return LocalBackend(root)
    raise NotImplementedError(
        f"backend for scheme {scheme!r} is not implemented yet (see docs/tracker.md)"
    )


def _mirror_error() -> type[Exception]:
    """tracker_mcp.MirrorError when importable; a never-raised class otherwise."""
    try:
        import tracker_mcp
    except ImportError:  # pragma: no cover - ships alongside this file
        return type("NeverRaised", (Exception,), {})
    return tracker_mcp.MirrorError


def _service_error() -> type[Exception]:
    from chief_wiggum.task_service import TaskServiceError

    return TaskServiceError


def _ref_backend(ref: str, repo_root: str | None) -> Backend:
    scheme, ident = parse_ref(ref)
    return _backend_for_ref(scheme, ident, repo_root)


def ref_number(ref: str) -> int:
    """The issue number a ref carries: ``#N`` for GitHub, the file id for local."""
    scheme, ident = parse_ref(ref)
    if scheme == "gh":
        return GithubBackend._parse_ident(ident)[1]
    if scheme == "local":
        stem = Path(ident).stem
        if stem.isdigit():
            return int(stem)
    raise ValueError(f"ref {ref!r} carries no issue number")


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
    p_list.add_argument(
        "--state", choices=("open", "closed", "all"), default="all",
        help="Filter by state (default: all)",
    )
    p_list.add_argument("--label", help="Only issues carrying this label")
    p_list.add_argument(
        "--numbers", action="store_true",
        help="Print a comma-joined list of issue numbers instead of JSON",
    )

    p_create = sub.add_parser("create", help="Create a new issue")
    p_create.add_argument("target")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--body", default="")
    p_create.add_argument("--body-file", help="Read the body from this file instead")
    p_create.add_argument("--label", action="append", default=[], dest="labels")
    p_create.add_argument("--assignee")
    p_create.add_argument("--epic")
    p_create.add_argument(
        "--provenance-class", choices=PROVENANCE_CLASSES,
        help="Who asked for this work (a task service records it; GitHub ignores it)",
    )
    p_create.add_argument("--provenance-source", help="Citation for --provenance-class")
    p_create.add_argument("--agent", help="The agent recording the provenance, if any")
    p_create.add_argument(
        "--disclose-ai", action="store_true",
        help="Append the CW AI-authorship disclosure line to the issue body (#317) — "
        "pass this for issues published to a public repo (e.g. /create-issue, /plan-epic)",
    )

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

    p_context = sub.add_parser(
        "context", help="Print the issue with author and comments (gh issue view --json shape)"
    )
    p_context.add_argument("ref")

    p_status = sub.add_parser("status", help="Record a work status (ready/in_progress/blocked)")
    p_status.add_argument("ref")
    p_status.add_argument("status", choices=STATUSES)
    p_status.add_argument("--holder", required=True, help="The agent holding the work")
    p_status.add_argument("--note", default="", help="What changed")

    p_complete = sub.add_parser("complete", help="Finish work: close with a summary comment")
    p_complete.add_argument("ref")
    p_complete.add_argument("--summary", default="")
    p_complete.add_argument("--holder")

    p_decision = sub.add_parser("decision", help="Log a decision with its provenance")
    p_decision.add_argument("ref")
    p_decision.add_argument("--what", required=True)
    p_decision.add_argument("--decided-by", required=True)
    p_decision.add_argument("--class", dest="provenance_class", required=True,
                            choices=PROVENANCE_CLASSES)
    p_decision.add_argument("--source", dest="provenance_source", required=True)
    p_decision.add_argument("--channel", default="")
    p_decision.add_argument("--words", dest="approval_words")
    p_decision.add_argument("--relayed-by")

    p_lease = sub.add_parser("lease", help="Show the current claim on an issue")
    p_lease.add_argument("ref")

    p_beat = sub.add_parser(
        "heartbeat", help="Renew the leases of a held claim (backends whose leases expire)"
    )
    p_beat.add_argument("ref")
    p_beat.add_argument("--holder", help="Refuse unless this holder has the claim")

    p_service = sub.add_parser(
        "service",
        help="mcp backend: which verbs the task service serves, and a ref's service record",
    )
    p_service.add_argument("target")
    p_service.add_argument("--ref", help="Also show this ref's service key, task id, provenance and run")

    p_caps = sub.add_parser(
        "capabilities", help="Print which optional verb groups a backend supports"
    )
    p_caps.add_argument("target")

    p_link = sub.add_parser("link", help="Record that REF is blocked by BLOCKER")
    p_link.add_argument("ref")
    p_link.add_argument("blocker")
    p_link.add_argument("--remove", action="store_true", help="Remove the edge instead")

    p_deps = sub.add_parser("deps", help="Show dependency edges in both directions")
    p_deps.add_argument("ref")

    p_ready = sub.add_parser("ready", help="List open issues with no open blocker")
    p_ready.add_argument("target")
    p_ready.add_argument("--query", help="Substring filter over title/body")

    p_claim = sub.add_parser("claim", help="Atomically claim an issue for an agent")
    p_claim.add_argument("ref")
    p_claim.add_argument("agent_id")
    p_claim.add_argument("--release", action="store_true", help="Release instead of claim")

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
            if args.state != "all":
                issues = [i for i in issues if i.state == args.state]
            if args.label:
                issues = [i for i in issues if args.label in i.labels]
            if args.numbers:
                print(",".join(str(ref_number(i.ref)) for i in issues))
            else:
                print(json.dumps([i.to_dict() for i in issues], indent=2))

        elif args.command == "create":
            backend = get_tracker(args.target, repo_root=repo_root)
            body = Path(args.body_file).read_text() if args.body_file else args.body
            if args.disclose_ai:
                body = ai_disclosure.ensure_disclosure(body)
            draft = IssueDraft(
                title=args.title, body=body, labels=args.labels,
                assignee=args.assignee, epic=args.epic,
                provenance=(
                    Provenance(args.provenance_class, args.provenance_source or "", args.agent)
                    if args.provenance_class else None
                ),
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

        elif args.command == "context":
            backend = _ref_backend(args.ref, repo_root)
            print(json.dumps(backend.read_context(args.ref), indent=2))

        elif args.command == "status":
            backend = _ref_backend(args.ref, repo_root)
            backend.set_status(args.ref, args.status, holder=args.holder, note=args.note)

        elif args.command == "complete":
            backend = _ref_backend(args.ref, repo_root)
            backend.complete(args.ref, args.summary, holder=args.holder)

        elif args.command == "decision":
            backend = _ref_backend(args.ref, repo_root)
            decision = Decision(
                what=args.what, decided_by=args.decided_by,
                provenance=Provenance(args.provenance_class, args.provenance_source),
                channel=args.channel, approval_words=args.approval_words,
                relayed_by=args.relayed_by,
            )
            backend.log_decision(args.ref, decision)

        elif args.command == "lease":
            backend = _ref_backend(args.ref, repo_root)
            print(json.dumps(backend.lease(args.ref).to_dict(), indent=2))

        elif args.command == "heartbeat":
            backend = _ref_backend(args.ref, repo_root)
            if not hasattr(backend, "heartbeat"):
                # github holds no claims; local claims do not expire.
                print(json.dumps(backend.lease(args.ref).to_dict(), indent=2))
            else:
                print(json.dumps(backend.heartbeat(args.ref, args.holder).to_dict(), indent=2))

        elif args.command == "service":
            backend = get_tracker(args.target, repo_root=repo_root)
            if not hasattr(backend, "service_gaps"):
                print(json.dumps({"backend": type(backend).__name__, "service": None}, indent=2))
            else:
                report: dict[str, Any] = {
                    "backend": type(backend).__name__,
                    "mirror": backend.mirror_mode,
                    "gaps": backend.service_gaps(),
                }
                if args.ref:
                    ref = backend.resolve_ref(args.ref)
                    record = dict(backend.state.task(ref))
                    provenance = record.get("provenance") or {}
                    record["agent_proposal"] = provenance.get("class") == "agent"
                    record["key"] = backend.service_key(ref)
                    record["lease"] = backend.lease(ref).to_dict()
                    report["ref"] = {ref: record}
                print(json.dumps(report, indent=2))

        elif args.command == "capabilities":
            backend = get_tracker(args.target, repo_root=repo_root)
            caps = sorted(getattr(backend, "capabilities", lambda: frozenset())())
            print(json.dumps({"backend": type(backend).__name__, "capabilities": caps}, indent=2))

        elif args.command == "link":
            scheme, ident = parse_ref(args.ref)
            backend = _backend_for_ref(scheme, ident, repo_root)
            if args.remove:
                backend.unlink(args.ref, args.blocker)
            else:
                backend.link(args.ref, args.blocker)
            print(json.dumps(backend.deps(args.ref).to_dict(), indent=2))

        elif args.command == "deps":
            scheme, ident = parse_ref(args.ref)
            backend = _backend_for_ref(scheme, ident, repo_root)
            print(json.dumps(backend.deps(args.ref).to_dict(), indent=2))

        elif args.command == "ready":
            backend = get_tracker(args.target, repo_root=repo_root)
            issues = backend.ready(args.query)
            print(json.dumps([issue.to_dict() for issue in issues], indent=2))

        elif args.command == "claim":
            scheme, ident = parse_ref(args.ref)
            backend = _backend_for_ref(scheme, ident, repo_root)
            if args.release:
                held = backend.release(args.ref, args.agent_id)
            else:
                held = backend.claim(args.ref, args.agent_id)
            print(json.dumps({"ref": args.ref, "agent_id": args.agent_id, "held": held}, indent=2))
            return 0 if held else 1

        elif args.command == "members":
            backend = get_tracker(args.target, repo_root=repo_root)
            issues = backend.members(args.epic)
            print(json.dumps([i.to_dict() for i in issues], indent=2))

        else:  # pragma: no cover - argparse enforces valid subcommands
            print(f"Unknown command: {args.command}", file=sys.stderr)
            return 1

    except _mirror_error() as exc:
        # GitHub took the write; the task service did not (#498).
        print(f"Error: {exc}", file=sys.stderr)
        return 3
    except (ValueError, FileNotFoundError, NotImplementedError, TypeError, LookupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except _service_error() as exc:
        print(f"Error: task service: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Error: gh command failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
