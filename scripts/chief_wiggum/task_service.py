"""Client for an MCP task service, the tracker's ``mcp`` backend (chief-wiggum#498).

A task service holds claims, run notes, statuses and a decision log with
provenance, and mirrors its own writes to GitHub. CW stays service-neutral:
everything that names a service tool, shapes its arguments or parses its
results lives in :class:`ServiceMapping`, the only thing to edit when the
service's shapes change.

Three pieces:

- a **transport** (``tools()`` and ``call(name, arguments)``); the shipped one
  speaks MCP over stdio to a configured server command;
- the **mapping** from tracker verbs to tool calls, and from tool results
  back to tasks, runs and leases;
- a **state file** recording, per target, each service key's task id and
  mirrored issue, and which run this machine holds. Separate CLI invocations
  share it, so ``claim`` in one step and ``complete`` in a later one see the
  same run.

No secrets pass through here: the server command is launched as configured
and authenticates itself.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

MCP_PROTOCOL_VERSION = "2025-06-18"


class TaskServiceError(RuntimeError):
    """The service answered a tool call with an error."""


class ToolUnavailable(TaskServiceError):
    """The service does not (yet) expose the tool a verb maps to."""


class ClaimRefused(TaskServiceError):
    """The service declined a targeted claim for a reason other than "held"."""


class InvalidArguments(TaskServiceError):
    """JSON-RPC -32602: the arguments do not match the tool's schema."""


# --- transport ---------------------------------------------------------------


class Transport(Protocol):
    def tools(self) -> dict[str, dict]:
        """Tool name -> input JSON schema."""

    def call(self, name: str, arguments: dict) -> Any:
        """Call a tool. Returns its structured result; raises TaskServiceError."""


def result_payload(result: dict) -> Any:
    """Unwrap an MCP ``tools/call`` result into a value, or raise."""
    texts = [
        item.get("text", "") for item in result.get("content") or []
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    if result.get("isError"):
        structured = result.get("structuredContent")
        reason = structured.get("reason") if isinstance(structured, dict) else None
        raise TaskServiceError(reason or "; ".join(texts) or "tool call failed")
    if "structuredContent" in result:
        return result["structuredContent"]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            return texts[0]
    return texts


class StdioTransport:
    """MCP over stdio: newline-delimited JSON-RPC 2.0 with a spawned server."""

    def __init__(self, command: list[str], *, timeout: float = 60.0):
        if not command:
            raise ValueError("the mcp tracker backend needs mcp.command in its config")
        self.command = list(command)
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._next_id = 0
        self._tools: dict[str, dict] | None = None

    def _start(self) -> subprocess.Popen:
        if self._proc is None:
            self._proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
            self._request("initialize", {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "chief-wiggum-tracker", "version": "1"},
            })
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self._proc

    def _send(self, message: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()

    def _request(self, method: str, params: dict) -> Any:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            line = proc.stdout.readline()
            if not line:
                raise TaskServiceError(f"task service exited during {method}")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # a server's stray log line is not a protocol message
            if message.get("id") != request_id:
                continue  # notifications and other traffic
            if "error" in message:
                error = message["error"]
                text = f"{method}: {error.get('message', error) if isinstance(error, dict) else error}"
                if isinstance(error, dict) and error.get("code") == -32602:
                    raise InvalidArguments(f"-32602 {text}")
                raise TaskServiceError(text)
            return message.get("result")

    def tools(self) -> dict[str, dict]:
        if self._tools is None:
            self._start()
            result = self._request("tools/list", {}) or {}
            self._tools = {
                tool["name"]: tool.get("inputSchema") or {} for tool in result.get("tools", [])
            }
        return self._tools

    def call(self, name: str, arguments: dict) -> Any:
        self._start()
        return result_payload(self._request("tools/call", {"name": name, "arguments": arguments}) or {})

    def close(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()
            self._proc = None


# --- the mapping: the one place that knows the service's tool shapes ---------


@dataclass(frozen=True)
class ToolSpec:
    tool: str
    # Properties the tool's input schema must have for the verb to be served,
    # e.g. a targeted claim needs claim_task to accept a task.
    requires: tuple[str, ...] = ()


ACTORS = ("lead", "manager", "worker", "browser", "human")
PRIORITIES = ("P1", "P2", "P3")

# What a created task is done when, if the target does not configure one: the
# service holds no issue body, so it points at the body.
DEFAULT_DONE_WHEN = "the acceptance criteria in the issue body are met"


@dataclass
class ServiceTask:
    """One task as the service's ``task`` read returns it."""

    task_id: str
    key: str
    title: str
    state: str                      # ready | doing | blocked | held | done
    done_when: str = ""
    created_by: str | None = None
    provenance_class: str | None = None
    provenance_source: str | None = None
    lane: str | None = None
    source: str | None = None       # local | github
    issue: str | None = None        # "owner/repo#N" once mirrored
    notes: list[dict] = field(default_factory=list)       # comments
    decisions: list[dict] = field(default_factory=list)
    status: dict = field(default_factory=dict)
    deps: list[str] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    notes_total: int | None = None   # all notes the service holds, when it says
    notes_before: str | None = None  # cursor to the notes older than those in ``notes``

    @property
    def closed(self) -> bool:
        return self.state == "done"

    def summaries(self) -> list[str]:
        return [
            str(r["run"]["summary"]) for r in self.runs
            if isinstance(r.get("run"), dict) and r["run"].get("summary")
        ]


@dataclass
class TaskPage:
    """One answer of ``list_tasks``: rows, and where the next page starts."""

    tasks: list[ServiceTask]
    next_cursor: str | None = None
    total: int | None = None
    paged: bool = False


class ServiceMapping:
    """Tracker verbs -> service tool calls. Swap names and shapes here only.

    Shapes as the service exposes them on 2026-11-03, checked against a
    running service: every row's id is ``id``, ``log_decision`` takes the
    task's id (not its key), a comment answers ``{"note"}``, and the ``task``
    read carries the mirrored issue as ``issue: "owner/repo#N"``.
    """

    TOOLS: dict[str, ToolSpec] = {
        "read": ToolSpec("task"),
        "list": ToolSpec("list_tasks"),
        "create": ToolSpec("create_task"),
        "claim": ToolSpec("claim_task", requires=("task",)),
        "status": ToolSpec("log_work", requires=("status",)),
        "comment": ToolSpec("comment", requires=("task", "text")),
        "complete": ToolSpec("complete_task"),
        "decision": ToolSpec("log_decision"),
        "heartbeat": ToolSpec("heartbeat"),
        "release": ToolSpec("release"),
    }

    # A claim refusal that means "not yours right now": the caller skips the
    # task. Every other refusal (identity, actor, capability, capacity, hours,
    # unknown task) is a configuration problem and raises.
    LOST_CLAIM_REASONS = ("TaskHeld", "TaskNotReady", "TaskStateConflict")

    # list_tasks pages {tasks, next_cursor, total} (limit 50, compact rows unless
    # full: true), and task returns the newest 20 notes (oldest first within the
    # page) with notes_total and a notes_before cursor, null at the oldest page (chief-wiggum#506). Each is sent only when the tool's
    # schema declares it, so a service still on the unpaged shapes keeps working.
    LIST_PAGING_ARGS = ("cursor", "limit", "full")
    LIST_PAGE_SIZE = 50
    NOTES_CURSOR = "notes_before"
    NOTES_LIMIT = "notes_limit"   # 1 to 100, default 20: ask for the most per page
    NOTES_PAGE_SIZE = 100
    LIST_MAX_LIMIT = 200

    # A read that finds nothing.
    NOT_FOUND_REASONS = ("TaskNotFound",)

    # --- arguments ---

    def create_args(self, draft: Any, provenance: Any, config: dict) -> dict:
        actor = config.get("actor", "worker")
        priority = config.get("priority", "P2")
        if actor not in ACTORS:
            raise ValueError(f"mcp.actor must be one of {ACTORS}, got {actor!r}")
        if priority not in PRIORITIES:
            raise ValueError(f"mcp.priority must be one of {PRIORITIES}, got {priority!r}")
        args: dict[str, Any] = {
            "title": draft.title,
            "actor": actor,
            "priority": priority,
            "done_when": config.get("done_when") or DEFAULT_DONE_WHEN,
            "provenance_class": provenance.klass,
            "provenance_source": provenance.source,
        }
        lane = config.get("lane")
        if lane:
            args["lane"] = lane
        if config.get("requires"):
            args["requires"] = list(config["requires"])
        return args

    def claim_args(self, task: str, agent: str, config: dict) -> dict:
        return {
            "task": task,
            "agent": agent,
            "environment": config.get("environment", "chief-wiggum"),
            "state": config.get("claim_state", "GREEN"),
            "stale": False,
            "ceiling": False,
            "capabilities": list(config.get("capabilities", [])),
        }

    def status_args(self, task_id: str, run_id: str, status: str, note: str) -> dict:
        """CW status -> a log_work status note from the run's holder.

        log_work's status is {done, in_progress, next, blocked_on}, not a
        state enum: the CW status picks the field the note goes in.
        """
        said = note or status.replace("_", " ")
        return {
            "task_id": task_id,
            "run_id": run_id,
            "text": f"status: {status}" + (f": {note}" if note else ""),
            "links": [],
            "status": {
                "done": "",
                "in_progress": said if status == "in_progress" else "",
                "next": said if status == "ready" else "",
                "blocked_on": said if status == "blocked" else None,
            },
        }

    @staticmethod
    def comment_args(key: str, body: str) -> dict:
        return {"task": key, "text": body}

    @staticmethod
    def complete_args(task_id: str, run_id: str, summary: str) -> dict:
        return {"task_id": task_id, "run_id": run_id, "summary": summary or "done", "links": []}

    @staticmethod
    def decision_args(task_id: str, decision: Any, caller: str) -> dict:
        """A CW decision -> log_decision, which checks who decided.

        ``decided_by`` must be the caller or a registered ``human:<name>``
        the caller relays. A human decision is relayed as ``human:<name>``;
        the service refuses relayed approval words, so they travel in the
        provenance source instead. A decision by anyone else is logged by
        the caller on their behalf, said so in the channel.
        """
        channel = decision.channel or "cw-tracker"
        source = decision.provenance.source
        approval = decision.approval_words
        decided_by = decision.decided_by
        if decided_by.startswith("human:") or (
            decision.provenance.klass == "human" and decided_by != caller
        ):
            decided_by = "human:" + decided_by.removeprefix("human:")
            channel = f"{channel} (relayed by {decision.relayed_by or caller})"
            if approval and approval not in source:
                source = f'{source} (approval words: "{approval}")'
            approval = None
        elif decided_by != caller:
            channel = f"{channel} (for {decided_by})"
            decided_by = caller
        args = {
            "scope": "task",
            "task_id": task_id,
            "decided_by": decided_by,
            "channel": channel,
            "what": decision.what,
            "provenance_class": decision.provenance.klass,
            "provenance_source": source,
        }
        if approval:
            args["approval_words"] = approval
        return args

    @staticmethod
    def read_args(
        ident: str, *, notes_before: str | None = None, notes_limit: int | None = None
    ) -> dict:
        """``task`` takes exactly one of task_id or key; keys carry a ``#``."""
        args: dict[str, Any] = {"key": ident} if "#" in ident else {"task_id": ident}
        if notes_before:
            args[ServiceMapping.NOTES_CURSOR] = notes_before
        if notes_limit:
            args[ServiceMapping.NOTES_LIMIT] = notes_limit
        return args

    @staticmethod
    def list_args(
        state: str | None = None, *, cursor: str | None = None,
        accepts: Iterable[str] = (),
    ) -> dict:
        """CW reads full rows (done_when, provenance, lane), so ``full: true``."""
        accepts = set(accepts)
        args: dict[str, Any] = {"state": state} if state else {}
        if "full" in accepts:
            args["full"] = True
        if "limit" in accepts:
            args["limit"] = ServiceMapping.LIST_PAGE_SIZE
        if cursor and "cursor" in accepts:
            args["cursor"] = cursor
        return args

    @staticmethod
    def lease_args(lease_id: str) -> dict:
        return {"lease_id": lease_id}

    # --- results ---

    @staticmethod
    def _id(row: Any) -> str | None:
        if isinstance(row, dict):
            value = row.get("id", row.get("task_id"))
            return str(value) if value is not None else None
        return None

    def task_from(self, result: Any) -> ServiceTask:
        """A ``task`` read, a create reply, or a bare row -> ServiceTask."""
        record = result if isinstance(result, dict) else {}
        row = record.get("task") if isinstance(record.get("task"), dict) else record
        deps = [
            str(d.get("key") or d.get("id")) if isinstance(d, dict) else str(d)
            for d in record.get("deps") or []
        ]
        return ServiceTask(
            task_id=self._id(row) or "",
            key=str(row.get("key") or self._id(row) or ""),
            title=row.get("title") or "",
            state=row.get("state") or "ready",
            done_when=row.get("done_when") or "",
            created_by=row.get("created_by"),
            provenance_class=row.get("provenance_class"),
            provenance_source=row.get("provenance_source"),
            lane=row.get("lane"),
            source=row.get("source"),
            issue=record.get("issue"),
            notes=[n for n in record.get("notes") or [] if isinstance(n, dict)],
            decisions=[d for d in record.get("decisions") or [] if isinstance(d, dict)],
            status=record.get("status") if isinstance(record.get("status"), dict) else {},
            deps=deps,
            runs=[r for r in record.get("runs") or [] if isinstance(r, dict)],
            notes_total=record.get("notes_total") if isinstance(record.get("notes_total"), int) else None,
            notes_before=(
                str(record["notes_before"]) if record.get("notes_before") not in (None, "") else None
            ),
        )

    def page_from(self, result: Any) -> TaskPage:
        """A ``list_tasks`` answer, paged ({tasks, next_cursor, total}) or the
        old unpaged shape (``{tasks}`` or a bare list): detected, not configured."""
        rows = result.get("tasks") if isinstance(result, dict) else result
        paged = isinstance(result, dict) and ("next_cursor" in result or "total" in result)
        cursor = result.get("next_cursor") if isinstance(result, dict) else None
        total = result.get("total") if isinstance(result, dict) else None
        return TaskPage(
            tasks=[self.task_from(row) for row in rows or [] if isinstance(row, dict)],
            next_cursor=str(cursor) if cursor not in (None, "") else None,
            total=total if isinstance(total, int) else None,
            paged=paged,
        )

    def tasks_from(self, result: Any) -> list[ServiceTask]:
        return self.page_from(result).tasks

    def claim_from(self, result: Any) -> dict:
        """{task, run, leases, reminder} -> the run and leases to hold."""
        run = result.get("run") if isinstance(result, dict) else None
        leases = result.get("leases") if isinstance(result, dict) else None
        leases = [lease for lease in (leases or []) if isinstance(lease, dict)]
        lease_ids = [self._id(lease) for lease in leases if self._id(lease)]
        expiries = [e for e in (_lease_expiry(lease) for lease in leases) if e]
        return {
            "run_id": self._id(run),
            "lease_id": lease_ids[0] if lease_ids else None,
            "lease_ids": lease_ids,
            "resources": [lease["resource"] for lease in leases if lease.get("resource")],
            "expires_at": min(expiries) if expiries else None,
        }

    @staticmethod
    def refusal_reason(message: str) -> str:
        """The leading ``Reason`` word of a refusal (``TaskHeld: agent-x``)."""
        return message.strip().split(":", 1)[0].strip()

    def is_lost_claim(self, message: str) -> bool:
        return self.refusal_reason(message) in self.LOST_CLAIM_REASONS

    def is_not_found(self, message: str) -> bool:
        return self.refusal_reason(message) in self.NOT_FOUND_REASONS

    # --- keys and issues: service keys <-> GitHub refs, in one place ---

    @staticmethod
    def candidate_keys(owner_repo: str, number: int) -> list[str]:
        """Keys the service may know a GitHub issue by: an imported issue is
        keyed ``<repo>#N``; ``owner/repo#N`` is accepted too."""
        repo = owner_repo.split("/", 1)[-1]
        return [f"{repo}#{number}", f"{owner_repo}#{number}"]

    @staticmethod
    def issue_ref(issue: str | None) -> str | None:
        """The service's ``owner/repo#N`` mirror record -> a ``gh:`` ref."""
        if issue and _ISSUE_RE.match(issue):
            return f"gh:{issue}"
        return None

    @staticmethod
    def is_local_key(key: str) -> bool:
        return bool(_LOCAL_KEY_RE.match(key))


_ISSUE_RE = re.compile(r"^[\w.-]+/[\w.-]+#\d+$")
_LOCAL_KEY_RE = re.compile(r"^local#\d+$")


def _lease_expiry(lease: dict) -> str | None:
    if lease.get("expires_at"):
        return str(lease["expires_at"])
    beat, ttl = lease.get("heartbeat_at"), lease.get("ttl_secs")
    if not beat or ttl is None:
        return None
    try:
        at = datetime.fromisoformat(str(beat).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (at + timedelta(seconds=int(ttl))).isoformat().replace("+00:00", "Z")


# --- per-target state ----------------------------------------------------------


class ServiceState:
    """Service key -> task id and issue, and the runs this machine holds."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = {"keys": {}, "runs": {}}
        if path.is_file():
            loaded = json.loads(path.read_text())
            self.data["keys"].update(loaded.get("keys", {}))
            self.data["runs"].update(loaded.get("runs", {}))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def task(self, key: str) -> dict:
        return self.data["keys"].get(key, {})

    def remember(self, key: str, **fields: Any) -> None:
        record = self.data["keys"].setdefault(key, {})
        record.update({k: v for k, v in fields.items() if v is not None})
        self.save()

    def forget(self, key: str, name: str) -> None:
        if self.data["keys"].get(key, {}).pop(name, None) is not None:
            self.save()

    def key_for_issue(self, gh_ref: str) -> str | None:
        for key, record in self.data["keys"].items():
            if record.get("issue") == gh_ref:
                return key
        return None

    def run(self, key: str) -> dict | None:
        return self.data["runs"].get(key)

    def hold(self, key: str, run: dict) -> None:
        self.data["runs"][key] = run
        self.save()

    def drop(self, key: str) -> None:
        if self.data["runs"].pop(key, None) is not None:
            self.save()


def state_path(user_dir: Path, owner_repo: str) -> Path:
    owner, _, repo = owner_repo.partition("/")
    return user_dir / "state" / "tracker" / owner / f"{repo}.json"
