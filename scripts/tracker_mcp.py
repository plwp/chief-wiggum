"""The tracker's ``mcp`` backend: writes go to a task service, which mirrors
them to GitHub (chief-wiggum#498).

- **Writes go to the service only** (create, comment, status, complete,
  decision, claim). CW never writes the same thing to GitHub itself: after a
  write it runs the configured mirror command (``mcp.mirror_command``, the
  service operator's idempotent ``mirror --repo <owner>/<repo>``), and the
  service writes GitHub. With no mirror command configured, GitHub shows the
  write the next time the operator mirrors, and CW says so.
- **Reads come from one configured source**, ``mcp.read``: ``github``
  (default) reads the mirrored issues as every skill read them before;
  ``service`` reads the service's task records.
- **Fields the service does not hold stay GitHub's**: an issue's body,
  labels, assignee and milestone (``update``, ``group``), and dependency edges
  (``link``, ``deps``) are written and read on GitHub. A created issue's body
  and labels are applied once the mirror has created the issue.
- **A task the service does not hold** (an issue older than the adoption) is
  written on GitHub as the ``github`` backend would, with a notice.

``compare(ref)`` reads a task back from both GitHub and the service and lists
every place they disagree: the round-trip contract.

Service tool names, argument shapes and result parsing live in
``chief_wiggum.task_service.ServiceMapping`` and nowhere else.
"""

from __future__ import annotations

import atexit
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifacts  # noqa: E402
import tracker as _t  # noqa: E402
from chief_wiggum.task_service import (  # noqa: E402
    ClaimRefused,
    ServiceMapping,
    ServiceState,
    ServiceTask,
    StdioTransport,
    TaskServiceError,
    ToolUnavailable,
    Transport,
    state_path,
)

MIRROR_MODES = ("strict", "best_effort")
READ_SOURCES = ("github", "service")


class MirrorError(RuntimeError):
    """The task service took the write; mirroring it to GitHub failed."""


class NotVisibleOnGitHub(LookupError):
    """A service task with no mirrored issue, read from GitHub."""


class NotHeld(TaskServiceError):
    """Status and completion come from the run's holder; this caller holds none."""


@dataclass
class Target:
    """A ref resolved against the service: its key, id, and issue if mirrored."""

    key: str
    task_id: str
    issue: str | None  # gh:owner/repo#N


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


class McpTaskBackend:
    """A task service for writes, GitHub kept current by the service's mirror."""

    def __init__(
        self,
        repo: str,
        *,
        transport: Transport,
        github: _t.GithubBackend | None = None,
        config: dict | None = None,
        state: ServiceState | None = None,
        mapping: ServiceMapping | None = None,
        notify: Callable[[str], None] = _stderr,
        mirror: Callable[[], Any] | None = None,
    ):
        self.repo = repo
        self.github = github or _t.GithubBackend(repo)
        self.transport = transport
        self.config = dict(config or {})
        self.mirror_mode = self.config.get("mirror", "strict")
        if self.mirror_mode not in MIRROR_MODES:
            raise ValueError(f"mcp.mirror must be one of {MIRROR_MODES}, got {self.mirror_mode!r}")
        self.read_source = self.config.get("read", "github")
        if self.read_source not in READ_SOURCES:
            raise ValueError(f"mcp.read must be one of {READ_SOURCES}, got {self.read_source!r}")
        self.caller = self.config.get("agent") or "chief-wiggum"
        self.state = state or ServiceState(state_path(artifacts.user_dir(), repo))
        self.mapping = mapping or ServiceMapping()
        self.notify = notify
        self.run_mirror = mirror
        self._lag_noted = False

    # --- what the service can do --------------------------------------------

    def supports(self, verb: str) -> bool:
        spec = self.mapping.TOOLS[verb]
        schema = self.transport.tools().get(spec.tool)
        if schema is None:
            return False
        properties = (schema or {}).get("properties", {})
        return all(name in properties for name in spec.requires)

    def capabilities(self) -> frozenset[str]:
        caps = set(self.github.capabilities())
        if self.supports("claim"):
            caps.add(_t.CAP_CLAIM)
        return frozenset(caps)

    def service_gaps(self) -> list[str]:
        """Verbs the service does not serve, by tool name."""
        return [
            f"{verb} ({spec.tool})" for verb, spec in self.mapping.TOOLS.items()
            if not self.supports(verb)
        ]

    def _call(self, verb: str, arguments: dict) -> Any:
        if not self.supports(verb):
            raise ToolUnavailable(f"the task service has no {self.mapping.TOOLS[verb].tool}")
        return self.transport.call(self.mapping.TOOLS[verb].tool, arguments)

    # --- resolving refs against the service ------------------------------------

    def _read(self, ident: str) -> ServiceTask | None:
        """The service's record of a task (by key or id), or None."""
        try:
            result = self._call("read", self.mapping.read_args(ident))
        except TaskServiceError as exc:
            if self.mapping.is_not_found(str(exc)):
                return None
            raise
        return self.mapping.task_from(result)

    def _remember(self, task: ServiceTask) -> Target:
        issue = self.mapping.issue_ref(task.issue)
        self.state.remember(task.key, task_id=task.task_id, issue=issue)
        if issue:
            self._apply_pending(task.key, issue)
        return Target(task.key, task.task_id, issue or self.state.task(task.key).get("issue"))

    def target(self, ref: str) -> Target | None:
        """The service task a ref names, or None if the service does not hold it."""
        scheme, ident = _t.parse_ref(ref)
        if scheme == "task":
            record = self.state.task(ident)
            if record.get("task_id") and record.get("issue"):
                return Target(ident, record["task_id"], record["issue"])
            task = self._read(ident)
            if task is None:
                raise LookupError(f"the task service has no task {ident}")
            return self._remember(task)
        owner_repo, number = self.github._gh_ref(ref)
        key = self.state.key_for_issue(ref)
        if key and self.state.task(key).get("task_id"):
            return Target(key, self.state.task(key)["task_id"], ref)
        for candidate in self.mapping.candidate_keys(owner_repo, number):
            task = self._read(candidate)
            if task is not None and task.issue in (None, f"{owner_repo}#{number}"):
                self.state.remember(task.key, task_id=task.task_id, issue=ref)
                return Target(task.key, task.task_id, ref)
        return self._scan_for_issue(ref)

    def _scan_for_issue(self, ref: str) -> Target | None:
        """A task the service created and mirrored, seen first on GitHub.

        The service is not looked up by issue, so its tasks not yet mapped
        here are read once each and every mapping found is kept.
        """
        found = None
        for row in self.mapping.tasks_from(self._call("list", self.mapping.list_args())):
            if self.state.task(row.key).get("issue"):
                continue
            task = self._read(row.task_id)
            if task is None or not self.mapping.issue_ref(task.issue):
                continue
            target = self._remember(task)
            if target.issue == ref:
                found = target
        return found

    def resolve_ref(self, ref: str) -> str:
        """A ``task:<key>`` ref -> its mirrored issue; anything else unchanged."""
        scheme, ident = _t.parse_ref(ref)
        if scheme != "task":
            return ref
        target = self.target(ref)
        if target is None or target.issue is None:
            raise NotVisibleOnGitHub(
                f"{ident} has no GitHub issue yet: the service has not mirrored it"
                " (run the mirror, or set mcp.read to service)"
            )
        return target.issue

    def _ref_for(self, task: ServiceTask) -> str:
        """The ref a skill sees for a service task: its issue, else task:<key>."""
        issue = self.mapping.issue_ref(task.issue) or self.state.task(task.key).get("issue")
        return issue or f"task:{task.key}"

    def _apply_pending(self, key: str, issue: str) -> None:
        """Body, labels, assignee and epic of a created issue: GitHub-only
        fields, applied once the mirror has created the issue."""
        pending = self.state.task(key).get("pending")
        if not pending:
            return
        fields: dict[str, Any] = {}
        if pending.get("body"):
            mirrored = self.github.get(issue).body
            fields["body"] = pending["body"] + ("\n\n" + mirrored if mirrored else "")
        if pending.get("labels"):
            fields["labels"] = pending["labels"]
        if pending.get("assignee"):
            fields["assignee"] = pending["assignee"]
        if fields:
            self.github.update(issue, fields)
        if pending.get("epic"):
            self.github.group([issue], pending["epic"])
        self.state.forget(key, "pending")

    # --- the mirror ---------------------------------------------------------------

    def _mirrored(self) -> None:
        """After a service write: run the mirror so GitHub catches up."""
        if self.run_mirror is None:
            if not self._lag_noted:
                self.notify(
                    "tracker: no mcp.mirror_command: GitHub shows this write once the"
                    " service operator runs the mirror"
                )
                self._lag_noted = True
            return
        try:
            self.run_mirror()
        except Exception as exc:  # noqa: BLE001 - any mirror failure is reported the same way
            message = f"the task service took the write; mirroring it to GitHub failed: {exc}"
            if self.mirror_mode == "strict":
                raise MirrorError(message) from exc
            self.notify(f"tracker: warning: {message}")

    def _not_held(self, ref: str, verb: str) -> None:
        self.notify(f"tracker: {ref}: the task service does not hold this task; {verb} written on GitHub")

    # --- reads ---------------------------------------------------------------------

    def _issue_from_task(self, task: ServiceTask) -> _t.Issue:
        ref = self._ref_for(task)
        return _t.Issue(
            ref=ref,
            title=task.title,
            body=f"Done when: {task.done_when}" if task.done_when else "",
            state="closed" if task.closed else "open",
            labels=[],
            assignee=None,
            epic=task.lane,
            url_or_path=(
                f"https://github.com/{ref.removeprefix('gh:').replace('#', '/issues/')}"
                if ref.startswith("gh:") else task.key
            ),
        )

    def _service_task(self, ref: str) -> ServiceTask:
        target = self.target(ref)
        if target is None:
            raise LookupError(f"{ref}: the task service does not hold this task")
        task = self._read(target.task_id)
        if task is None:
            raise LookupError(f"{ref}: the task service has no task {target.key}")
        return task

    def get(self, ref: str) -> _t.Issue:
        if self.read_source == "service":
            return self._issue_from_task(self._service_task(ref))
        return self.github.get(self.resolve_ref(ref))

    def _service_list(self, state: str | None = None) -> list[_t.Issue]:
        tasks = self.mapping.tasks_from(self._call("list", self.mapping.list_args(state)))
        return [self._issue_from_task(task) for task in tasks]

    def list(self, query=None) -> list[_t.Issue]:
        if self.read_source == "service":
            return [i for i in self._service_list() if _t._matches_query(i, query)]
        return self.github.list(query)

    def ready(self, query=None) -> list[_t.Issue]:
        if self.read_source == "service":
            return [i for i in self._service_list("ready") if _t._matches_query(i, query)]
        return self.github.ready(query)

    def read_context(self, ref: str) -> dict:
        if self.read_source != "service":
            return self.github.read_context(self.resolve_ref(ref))
        task = self._service_task(ref)
        return {
            "title": task.title,
            "body": f"Done when: {task.done_when}" if task.done_when else "",
            "author": {"login": task.created_by},
            "labels": [],
            "assignees": [],
            "milestone": {"title": task.lane} if task.lane else None,
            "comments": [
                {"author": {"login": note.get("author")}, "body": note.get("body", "")}
                for note in task.notes
            ],
            "state": task.state,
            "key": task.key,
            "provenance": {"class": task.provenance_class, "source": task.provenance_source},
            "status": task.status,
            "decisions": task.decisions,
        }

    # --- GitHub-only fields ----------------------------------------------------------

    def members(self, epic_name: str) -> list[_t.Issue]:
        return self.github.members(epic_name)

    def deps(self, ref: str) -> _t.Dependencies:
        return self.github.deps(self.resolve_ref(ref))

    def update(self, ref: str, fields: dict) -> _t.Issue:
        return self.github.update(self.resolve_ref(ref), fields)

    def group(self, refs: list[str], epic_name: str) -> None:
        self.github.group([self.resolve_ref(r) for r in refs], epic_name)

    def link(self, ref: str, blocked_by: str) -> None:
        self.github.link(self.resolve_ref(ref), self.resolve_ref(blocked_by))

    def unlink(self, ref: str, blocked_by: str) -> None:
        self.github.unlink(self.resolve_ref(ref), self.resolve_ref(blocked_by))

    # --- writes: the service, then its mirror ------------------------------------------

    def _agent_provenance(self) -> _t.Provenance:
        """A create with no stated provenance is an agent proposal, said so.

        The service requires provenance on every task; inventing a stronger
        class than agent would launder the request.
        """
        return _t.Provenance(
            "agent", f"{self.caller} via tracker.py create, {date.today().isoformat()}",
            actor=self.caller,
        )

    def create(self, draft: _t.IssueDraft) -> str:
        provenance = draft.provenance or self._agent_provenance()
        result = self._call("create", self.mapping.create_args(draft, provenance, self.config))
        task = self.mapping.task_from(result)
        pending = {
            name: value for name, value in (
                ("body", draft.body), ("labels", list(draft.labels)),
                ("assignee", draft.assignee), ("epic", draft.epic),
            ) if value
        }
        self.state.remember(
            task.key, task_id=task.task_id, provenance=provenance.to_dict(),
            pending=pending or None,
        )
        self._mirrored()
        mirrored = self._read(task.task_id)
        if mirrored is not None:
            task = mirrored
            self._remember(task)
        return self._ref_for(task)

    def comment(self, ref: str, body: str) -> None:
        target = self.target(ref)
        if target is None:
            self._not_held(ref, "comment")
            self.github.comment(ref, body)
            return
        self._call("comment", self.mapping.comment_args(target.key, body))
        self._mirrored()

    def _held(self, target: Target, ref: str, holder: str | None) -> dict:
        run = self.state.run(target.key)
        if run is None:
            raise NotHeld(
                f"{ref}: the task service takes status and completion from the task's"
                " holder only, and no run is held here; claim it first (tracker.py claim)"
            )
        if holder and run.get("holder") != holder:
            raise NotHeld(f"{ref}: held by {run.get('holder')!r}, not {holder!r}")
        return run

    def set_status(self, ref: str, status: str, *, holder: str, note: str = "") -> None:
        _t._validate_status(status)
        target = self.target(ref)
        if target is None:
            self._not_held(ref, "status")
            self.github.set_status(ref, status, holder=holder, note=note)
            return
        run = self._held(target, ref, holder)
        self._call("status", self.mapping.status_args(target.task_id, run["run_id"], status, note))
        self._mirrored()

    def complete(self, ref: str, summary: str, *, holder: str | None = None) -> None:
        target = self.target(ref)
        if target is None:
            self._not_held(ref, "completion")
            self.github.complete(ref, summary, holder=holder)
            return
        run = self._held(target, ref, holder)
        self._call("complete", self.mapping.complete_args(target.task_id, run["run_id"], summary))
        self.state.drop(target.key)
        self._mirrored()

    def log_decision(self, ref: str, decision: _t.Decision) -> None:
        target = self.target(ref)
        if target is None:
            self._not_held(ref, "decision")
            self.github.log_decision(ref, decision)
            return
        self._call("decision", self.mapping.decision_args(target.task_id, decision, self.caller))
        self._mirrored()

    # --- claims ------------------------------------------------------------------------

    def claim(self, ref: str, agent_id: str) -> bool:
        if not self.supports("claim"):
            raise _t.UnsupportedCapability(
                f"the task service has no targeted claim ({self.mapping.TOOLS['claim'].tool}"
                " naming a task); an untargeted claim takes the next ready task, which may"
                " not be this one"
            )
        target = self.target(ref)
        if target is None:
            raise ToolUnavailable(f"{ref}: the task service does not hold this task, so it cannot be claimed")
        held = self.state.run(target.key)
        if held is not None and held.get("holder") == agent_id:
            return True
        # The service checks that the claiming agent is its authenticated
        # caller, so the configured service identity claims; agent_id is the
        # CW-side holder (a wave or worker) recorded locally.
        try:
            result = self._call("claim", self.mapping.claim_args(target.task_id, self.caller, self.config))
        except TaskServiceError as exc:
            if self.mapping.is_lost_claim(str(exc)):
                return False
            raise ClaimRefused(f"claim on {ref} refused: {exc}") from exc
        run = self.mapping.claim_from(result)
        if not run.get("run_id"):
            raise ClaimRefused(f"claim on {ref} returned no run: {result!r}")
        run["holder"] = agent_id
        self.state.hold(target.key, run)
        return True

    def _held_run(self, ref: str, agent_id: str | None) -> tuple[Target | None, dict | None]:
        target = self.target(ref)
        if target is None:
            return None, None
        run = self.state.run(target.key)
        if run is None or (agent_id and run.get("holder") != agent_id):
            return target, None
        return target, run

    def release(self, ref: str, agent_id: str) -> bool:
        target, run = self._held_run(ref, agent_id)
        if run is None:
            return False
        for lease_id in run.get("lease_ids") or []:
            self._call("release", self.mapping.lease_args(lease_id))
        self.state.drop(target.key)
        return True

    def heartbeat(self, ref: str, agent_id: str | None = None) -> _t.Lease:
        """Renew every lease the held run carries (the service expires them)."""
        _, run = self._held_run(ref, agent_id)
        if run is None:
            raise LookupError(f"{ref}: no run held here{f' by {agent_id}' if agent_id else ''}")
        for lease_id in run.get("lease_ids") or []:
            self._call("heartbeat", self.mapping.lease_args(lease_id))
        return self.lease(ref)

    def lease(self, ref: str) -> _t.Lease:
        target, run = self._held_run(ref, None)
        shown = (target.issue if target else None) or ref
        if run is None:
            return _t.Lease(ref=shown, holder=None, state="unclaimed")
        return _t.Lease(
            ref=shown,
            holder=run.get("holder"),
            state="active",
            lease_id=run.get("lease_id"),
            run_id=run.get("run_id"),
            expires_at=run.get("expires_at"),
        )

    # --- reports ----------------------------------------------------------------------

    def service_report(self, ref: str) -> dict:
        target = self.target(ref)
        if target is None:
            return {"held_by_service": False}
        record = dict(self.state.task(target.key))
        provenance = record.get("provenance") or {}
        record.update({
            "held_by_service": True,
            "key": target.key,
            "task_id": target.task_id,
            "issue": target.issue,
            "agent_proposal": provenance.get("class") == "agent",
            "lease": self.lease(ref).to_dict(),
        })
        return record

    def compare(self, ref: str) -> dict:
        """Read a task back from the service and from GitHub; list disagreements.

        The round-trip contract: after the mirror, GitHub carries the
        service's title and open/closed state, and every comment, decision
        (what and provenance source), status field and completion summary
        the service recorded appears in the issue's comments.
        """
        task = self._service_task(ref)
        issue_ref = self.mapping.issue_ref(task.issue)
        report: dict[str, Any] = {"key": task.key, "issue": issue_ref, "mismatches": []}
        missing = report["mismatches"]
        if issue_ref is None:
            missing.append("no GitHub issue: the service has not mirrored this task")
            return report
        issue = self.github.get(issue_ref)
        comments = [c.get("body", "") for c in self.github.read_context(issue_ref).get("comments", [])]

        def on_github(text: str, what: str) -> None:
            if text and not any(text in body for body in comments):
                missing.append(f"{what} not on GitHub: {text!r}")

        if issue.title != task.title:
            missing.append(f"title: service {task.title!r}, GitHub {issue.title!r}")
        if (issue.state == "closed") != task.closed:
            missing.append(f"state: service {task.state!r}, GitHub {issue.state!r}")
        for note in task.notes:
            on_github(note.get("body", ""), "comment")
        for decision in task.decisions:
            on_github(decision.get("what", ""), "decision")
            on_github(decision.get("provenance_source", ""), "decision provenance")
        for name in ("done", "in_progress", "blocked_on", "next"):
            value = task.status.get(name)
            if isinstance(value, str):
                on_github(value, f"status {name}")
        for summary in task.summaries():
            on_github(summary, "completion summary")
        report["service"] = {
            "state": task.state, "comments": len(task.notes),
            "decisions": len(task.decisions), "summaries": task.summaries(),
        }
        report["github"] = {"state": issue.state, "comments": len(comments)}
        return report


def _mirror_runner(config: dict, repo: str) -> Callable[[], str] | None:
    """The configured mirror command, ``{repo}`` filled in, as a callable."""
    command = config.get("mirror_command")
    if not command:
        return None
    if isinstance(command, str):
        command = command.split()
    argv = [str(part).replace("{repo}", repo) for part in command]
    timeout = float(config.get("mirror_timeout_seconds", 300))

    def run() -> str:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(
                f"{' '.join(argv[:2])} exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[-500:]}"
            )
        return result.stdout

    return run


def build(repo: str, config: dict, *, runner=None) -> McpTaskBackend:
    """Build the backend from the resolved ``mcp`` config block."""
    command = config.get("command")
    if isinstance(command, str):
        command = [command]
    transport = StdioTransport(command or [], timeout=float(config.get("timeout_seconds", 60)))
    atexit.register(transport.close)
    return McpTaskBackend(
        repo, transport=transport, github=_t.GithubBackend(repo, runner=runner), config=config,
        mirror=_mirror_runner(config, repo),
    )
