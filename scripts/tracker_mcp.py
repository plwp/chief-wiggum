"""The tracker's ``mcp`` backend: GitHub stays the source of truth, a task
service is mirrored (chief-wiggum#498).

Until the service mirrors its own writes to GitHub, this backend:

- **reads from GitHub** (get, list, members, ready, deps, context), so every
  skill sees exactly what it saw before;
- **writes to GitHub first, then the service** (create, comment, status,
  complete, decision). A GitHub write that fails stops there. A service write
  that fails after GitHub succeeded raises :class:`MirrorError` (``mirror:
  strict``, the default) or warns (``mirror: best_effort``);
- **claims on the service only.** GitHub has no compare-and-set, so it holds
  no claims (#371); the service's claim is the real one.

A verb whose service tool is not exposed yet (today: a comment outside a
run) is a declared gap, not a failure: the GitHub write happens, the mirror
is skipped with a notice, and ``capabilities()`` reports what the service can
actually do. A task the service does not know (an issue
older than the adoption) is skipped the same way.

Service tool names and argument shapes live in
``chief_wiggum.task_service.ServiceMapping`` and nowhere else.
"""

from __future__ import annotations

import atexit
import sys
from datetime import date
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifacts  # noqa: E402
import tracker as _t  # noqa: E402
from chief_wiggum.task_service import (  # noqa: E402
    ClaimRefused,
    ServiceMapping,
    ServiceState,
    StdioTransport,
    TaskServiceError,
    ToolUnavailable,
    Transport,
    state_path,
)

MIRROR_MODES = ("strict", "best_effort")


class MirrorError(RuntimeError):
    """GitHub took the write; the task service did not."""


class NotVisibleOnGitHub(LookupError):
    """A service-only task (``local#<n>``) that GitHub-reading skills cannot see."""


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


class McpTaskBackend:
    """GitHub for reads and writes of record, a task service mirrored."""

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
    ):
        self.repo = repo
        self.github = github or _t.GithubBackend(repo)
        self.transport = transport
        self.config = dict(config or {})
        self.mirror_mode = self.config.get("mirror", "strict")
        if self.mirror_mode not in MIRROR_MODES:
            raise ValueError(f"mcp.mirror must be one of {MIRROR_MODES}, got {self.mirror_mode!r}")
        self.state = state or ServiceState(state_path(artifacts.user_dir(), repo))
        self.mapping = mapping or ServiceMapping()
        self.notify = notify

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
        """Verbs the service does not serve yet, by tool name."""
        return [
            f"{verb} ({spec.tool})" for verb, spec in self.mapping.TOOLS.items()
            if not self.supports(verb)
        ]

    # --- keys ----------------------------------------------------------------

    def _gh(self, ref: str) -> tuple[str, int]:
        return self.github._gh_ref(self.resolve_ref(ref))

    def resolve_ref(self, ref: str) -> str:
        """A ``task:<key>`` ref -> its GitHub ref; anything else unchanged."""
        scheme, ident = _t.parse_ref(ref)
        if scheme != "task":
            return ref
        mapped = self.state.ref_for_key(ident) or self.mapping.gh_ref_for_key(ident)
        if mapped is None:
            raise NotVisibleOnGitHub(
                f"{ident} is a service-only task: it is not on GitHub until the"
                " service mirrors it, so GitHub-reading skills cannot see it"
            )
        return mapped

    def service_key(self, ref: str) -> str:
        ref = self.resolve_ref(ref)
        recorded = self.state.task(ref).get("key")
        if recorded:
            return recorded
        owner_repo, number = self.github._gh_ref(ref)
        return self.mapping.default_key(owner_repo, number)

    def _task_id(self, ref: str) -> str | None:
        """The service's task id for a ref, or None if the service lacks it."""
        ref = self.resolve_ref(ref)
        recorded = self.state.task(ref).get("task_id")
        if recorded:
            return recorded
        key = self.service_key(ref)
        try:
            result = self.transport.call(self.mapping.TOOLS["read"].tool, self.mapping.read_args(key))
        except TaskServiceError:
            return None
        task_id = self.mapping.task_id_from(result)
        if task_id:
            self.state.remember_task(ref, key=key, task_id=task_id)
        return task_id

    def _link(self, ref: str) -> str:
        owner_repo, number = self._gh(ref)
        return f"https://github.com/{owner_repo}/issues/{number}"

    # --- mirroring -------------------------------------------------------------

    def _skip(self, ref: str, why: str) -> None:
        self.notify(f"tracker: {ref}: task service not updated: {why}")

    def _fail(self, ref: str, verb: str, exc: Exception) -> None:
        message = f"{ref}: GitHub updated, task service {verb} failed: {exc}"
        if self.mirror_mode == "strict":
            raise MirrorError(message) from exc
        self.notify(f"tracker: warning: {message}")

    def _mirror(self, ref: str, verb: str, build: Callable[[str], dict | None]) -> Any:
        if not self.supports(verb):
            self._skip(ref, f"the service has no {self.mapping.TOOLS[verb].tool} yet")
            return None
        task_id = self._task_id(ref)
        if task_id is None:
            self._skip(ref, f"the service has no task {self.service_key(ref)}")
            return None
        arguments = build(task_id)
        if arguments is None:
            return None
        try:
            return self.transport.call(self.mapping.TOOLS[verb].tool, arguments)
        except TaskServiceError as exc:
            self._fail(ref, verb, exc)
            return None

    def _held_run(self, ref: str, verb: str, holder: str | None) -> dict | None:
        run = self.state.run(self.resolve_ref(ref))
        if run is None:
            self._fail(ref, verb, TaskServiceError(
                "the service takes status and completion from the task's holder only;"
                " claim it first (tracker.py claim)"
            ))
            return None
        if holder and run.get("holder") != holder:
            self._fail(ref, verb, TaskServiceError(
                f"held by {run.get('holder')!r}, not {holder!r}"
            ))
            return None
        return run

    # --- reads: GitHub -----------------------------------------------------------

    def get(self, ref: str) -> _t.Issue:
        return self.github.get(self.resolve_ref(ref))

    def list(self, query=None) -> list[_t.Issue]:
        return self.github.list(query)

    def members(self, epic_name: str) -> list[_t.Issue]:
        return self.github.members(epic_name)

    def read_context(self, ref: str) -> dict:
        return self.github.read_context(self.resolve_ref(ref))

    def deps(self, ref: str) -> _t.Dependencies:
        return self.github.deps(self.resolve_ref(ref))

    def ready(self, query=None) -> list[_t.Issue]:
        return self.github.ready(query)

    # --- writes with no service verb: GitHub only ------------------------------

    def update(self, ref: str, fields: dict) -> _t.Issue:
        return self.github.update(self.resolve_ref(ref), fields)

    def group(self, refs: list[str], epic_name: str) -> None:
        self.github.group([self.resolve_ref(r) for r in refs], epic_name)

    def link(self, ref: str, blocked_by: str) -> None:
        self.github.link(self.resolve_ref(ref), blocked_by)

    def unlink(self, ref: str, blocked_by: str) -> None:
        self.github.unlink(self.resolve_ref(ref), blocked_by)

    # --- writes: GitHub, then the service ---------------------------------------

    def create(self, draft: _t.IssueDraft) -> str:
        ref = self.github.create(draft)
        owner_repo, number = self.github._gh_ref(ref)
        key = self.mapping.default_key(owner_repo, number)
        provenance = draft.provenance or self._agent_provenance()
        self.state.remember_task(ref, key=key, provenance=provenance.to_dict())
        if not self.supports("create"):
            self._skip(ref, f"the service has no {self.mapping.TOOLS['create'].tool} yet")
            return ref
        try:
            result = self.transport.call(
                self.mapping.TOOLS["create"].tool,
                self.mapping.create_args(draft, self._link(ref), provenance, self.config),
            )
        except TaskServiceError as exc:
            self._fail(ref, "create", exc)
            return ref
        self.state.remember_task(
            ref,
            key=self.mapping.key_from(result) or key,
            task_id=self.mapping.task_id_from(result),
        )
        return ref

    def _agent_provenance(self) -> _t.Provenance:
        """A create with no stated provenance is an agent proposal, said so.

        The service requires provenance on every task; inventing a stronger
        class than agent would launder the request.
        """
        agent = self.config.get("agent") or "chief-wiggum"
        return _t.Provenance(
            "agent", f"{agent} via tracker.py create, {date.today().isoformat()}", actor=agent
        )

    def comment(self, ref: str, body: str) -> None:
        ref = self.resolve_ref(ref)
        self.github.comment(ref, body)
        run = self.state.run(ref)
        if run is not None and self.supports("status"):
            # A held run takes the comment as a run note (log_work).
            self._mirror(ref, "status", lambda task_id: self.mapping.comment_with_run_args(
                task_id, run["run_id"], body, self._link(ref)))
            return
        author = self.config.get("agent")
        self._mirror(ref, "comment", lambda task_id: self.mapping.comment_args(task_id, body, author))

    def set_status(self, ref: str, status: str, *, holder: str, note: str = "") -> None:
        ref = self.resolve_ref(ref)
        self.github.set_status(ref, status, holder=holder, note=note)
        run = self._held_run(ref, "status", holder)
        if run is None:
            return
        self._mirror(ref, "status", lambda task_id: self.mapping.status_args(
            task_id, run["run_id"], status, note, self._link(ref)))

    def complete(self, ref: str, summary: str, *, holder: str | None = None) -> None:
        ref = self.resolve_ref(ref)
        self.github.complete(ref, summary, holder=holder)
        run = self._held_run(ref, "complete", holder)
        if run is None:
            return
        self._mirror(ref, "complete", lambda task_id: self.mapping.complete_args(
            task_id, run["run_id"], summary, self._link(ref)))
        self.state.drop(ref)

    def log_decision(self, ref: str, decision: _t.Decision) -> None:
        ref = self.resolve_ref(ref)
        self.github.log_decision(ref, decision)
        self._mirror(ref, "decision", lambda task_id: self.mapping.decision_args(task_id, decision))

    # --- claims: the service only ---------------------------------------------------

    def claim(self, ref: str, agent_id: str) -> bool:
        ref = self.resolve_ref(ref)
        held = self.state.run(ref)
        if held is not None and held.get("holder") == agent_id:
            return True
        if not self.supports("claim"):
            raise _t.UnsupportedCapability(
                f"the task service has no targeted claim ({self.mapping.TOOLS['claim'].tool}"
                " naming a task); an untargeted claim takes the next ready task, which may"
                " not be this one"
            )
        task_id = self._task_id(ref)
        if task_id is None:
            raise ToolUnavailable(f"the task service has no task {self.service_key(ref)} to claim")
        # The service checks that the claiming agent is its authenticated
        # caller, so the configured service identity claims; agent_id is the
        # CW-side holder (a wave or worker) recorded locally.
        service_agent = self.config.get("agent") or agent_id
        try:
            result = self.transport.call(
                self.mapping.TOOLS["claim"].tool,
                self.mapping.claim_args(task_id, service_agent, self.config),
            )
        except TaskServiceError as exc:
            if self.mapping.is_lost_claim(str(exc)):
                return False
            raise ClaimRefused(f"claim on {ref} refused: {exc}") from exc
        run = self.mapping.claim_from(result)
        if not run.get("run_id"):
            raise ClaimRefused(f"claim on {ref} returned no run: {result!r}")
        run["holder"] = agent_id
        self.state.hold(ref, run)
        return True

    def release(self, ref: str, agent_id: str) -> bool:
        ref = self.resolve_ref(ref)
        run = self.state.run(ref)
        if run is None or run.get("holder") != agent_id:
            return False
        if self.supports("release"):
            for lease_id in run.get("lease_ids") or ([run["lease_id"]] if run.get("lease_id") else []):
                self.transport.call(
                    self.mapping.TOOLS["release"].tool, self.mapping.lease_args(lease_id)
                )
        self.state.drop(ref)
        return True

    def heartbeat(self, ref: str, agent_id: str | None = None) -> _t.Lease:
        """Renew every lease the held run carries (the service expires them)."""
        ref = self.resolve_ref(ref)
        run = self.state.run(ref)
        if run is None or (agent_id and run.get("holder") != agent_id):
            raise LookupError(f"{ref}: no run held here{f' by {agent_id}' if agent_id else ''}")
        for lease_id in run.get("lease_ids") or ([run["lease_id"]] if run.get("lease_id") else []):
            self.transport.call(
                self.mapping.TOOLS["heartbeat"].tool, self.mapping.lease_args(lease_id)
            )
        return self.lease(ref)

    def lease(self, ref: str) -> _t.Lease:
        ref = self.resolve_ref(ref)
        run = self.state.run(ref)
        if run is None:
            return _t.Lease(ref=ref, holder=None, state="unclaimed")
        return _t.Lease(
            ref=ref,
            holder=run.get("holder"),
            state="active",
            lease_id=run.get("lease_id"),
            run_id=run.get("run_id"),
            expires_at=run.get("expires_at"),
            epoch=int(run.get("epoch") or 0),
            fencing_token=run.get("fencing_token"),
        )


def build(repo: str, config: dict, *, runner=None) -> McpTaskBackend:
    """Build the backend from the resolved ``mcp`` config block."""
    command = config.get("command")
    if isinstance(command, str):
        command = [command]
    transport = StdioTransport(command or [], timeout=float(config.get("timeout_seconds", 60)))
    atexit.register(transport.close)
    return McpTaskBackend(
        repo, transport=transport, github=_t.GithubBackend(repo, runner=runner), config=config
    )
