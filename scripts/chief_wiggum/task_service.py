"""Client for an MCP task service, the tracker's ``mcp`` backend (chief-wiggum#498).

A task service holds claims, run notes, statuses and a decision log with
provenance. CW stays service-neutral: everything that names a service tool or
shapes its arguments lives in :class:`ServiceMapping`, so when the service's
real tool names and argument shapes are confirmed, that class is the only
thing to edit.

Three pieces:

- a **transport** (``tools()`` and ``call(name, arguments)``); the shipped one
  speaks MCP over stdio to a configured server command;
- the **mapping** from tracker verbs to tool calls, and from tool results
  back to ids;
- a **state file** recording, per target, which service key and task id each
  GitHub ref maps to and which run this machine holds on it. Separate CLI
  invocations share it, so ``claim`` in one step and ``complete`` in a later
  one see the same run.

No secrets pass through here: the server command is launched as configured
and authenticates itself.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

MCP_PROTOCOL_VERSION = "2025-06-18"


class TaskServiceError(RuntimeError):
    """The service answered a tool call with an error."""


class ToolUnavailable(TaskServiceError):
    """The service does not (yet) expose the tool a verb maps to."""


class ClaimRefused(TaskServiceError):
    """The service declined a targeted claim (someone else holds the task)."""


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
                raise TaskServiceError(f"{method}: {message['error'].get('message', message['error'])}")
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
    # Not in the service yet (#498): the adapter skips the mirror with a
    # notice until the service exposes it.
    pending: bool = False
    # Properties the tool's input schema must have for the verb to be served,
    # e.g. a targeted claim needs claim_task to accept a task.
    requires: tuple[str, ...] = ()


_GH_KEY_RE = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+)$")
_LOCAL_KEY_RE = re.compile(r"^local#\d+$")

ACTORS = ("lead", "manager", "worker", "browser", "human")
PRIORITIES = ("P1", "P2", "P3")


class ServiceMapping:
    """Tracker verbs -> service tool calls. Swap names and shapes here only.

    Shapes as confirmed by the service owner on 2026-11-03.
    """

    TOOLS: dict[str, ToolSpec] = {
        "read": ToolSpec("task"),
        "list": ToolSpec("list_tasks"),
        "create": ToolSpec("create_task"),
        "claim": ToolSpec("claim_task", requires=("task",)),
        "status": ToolSpec("log_work"),
        "complete": ToolSpec("complete_task"),
        "decision": ToolSpec("log_decision"),
        "heartbeat": ToolSpec("heartbeat"),
        "release": ToolSpec("release"),
        # Not built yet on the service: a comment outside a run.
        "comment": ToolSpec("comment_task", pending=True),
    }

    # A claim refusal that means "not yours right now": the caller skips the
    # task. Every other refusal (auth, actor, capability, capacity, hours,
    # unknown task) is a configuration problem and raises.
    LOST_CLAIM_REASONS = ("TaskHeld", "TaskNotReady")

    # CW status -> the service's run-status note. log_work's status is a
    # {done, in_progress, next, blocked_on} note written by the run's holder,
    # not a state enum.
    def status_args(self, task_id: str, run_id: str, status: str, note: str, link: str) -> dict:
        return {
            "task_id": task_id,
            "run_id": run_id,
            "text": f"status: {status}" + (f": {note}" if note else ""),
            "links": [link],
            "status": {
                "done": "",
                "in_progress": note if status == "in_progress" else "",
                "next": note if status == "ready" else "",
                "blocked_on": (note or "unspecified") if status == "blocked" else None,
            },
        }

    def comment_with_run_args(self, task_id: str, run_id: str, body: str, link: str) -> dict:
        return {
            "task_id": task_id, "run_id": run_id, "text": body, "links": [link],
            "status": {"done": "", "in_progress": "", "next": ""},
        }

    def comment_args(self, task_id: str, body: str, author: str | None) -> dict:
        args = {"task_id": task_id, "text": body}
        if author:
            args["author"] = author
        return args

    def complete_args(self, task_id: str, run_id: str, summary: str, link: str) -> dict:
        return {"task_id": task_id, "run_id": run_id, "summary": summary or "done", "links": [link]}

    def decision_args(self, task_id: str, decision: Any) -> dict:
        decided_by = decision.decided_by
        channel = decision.channel or "cw-tracker"
        if decision.relayed_by:
            # A relayed human decision is recorded as human:<name>; the relay
            # is named in the channel.
            if decision.provenance.klass == "human" and not decided_by.startswith("human:"):
                decided_by = f"human:{decided_by}"
            channel = f"{channel} (relayed by {decision.relayed_by})"
        return {
            "scope": "task",
            "task_id": task_id,
            "decided_by": decided_by,
            "channel": channel,
            "what": decision.what,
            "provenance_class": decision.provenance.klass,
            "provenance_source": decision.provenance.source,
            "approval_words": decision.approval_words,
        }

    def create_args(self, draft: Any, link: str, provenance: Any, config: dict) -> dict:
        """create_task takes no body, labels or key: the service mints a
        ``local#<n>`` key, and done_when carries the GitHub link back."""
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
            "done_when": f"{link} is closed",
            "provenance_class": provenance.klass,
            "provenance_source": provenance.source,
        }
        if config.get("lane"):
            args["lane"] = config["lane"]
        return args

    def claim_args(self, task_id: str, agent: str, config: dict) -> dict:
        return {
            "task": task_id,
            "agent": agent,
            "environment": config.get("environment", "chief-wiggum"),
            "state": config.get("claim_state", "GREEN"),
            "stale": False,
            "ceiling": False,
            "capabilities": list(config.get("capabilities", [])),
        }

    def read_args(self, key: str) -> dict:
        return {"key": key}

    def lease_args(self, lease_id: str) -> dict:
        return {"lease_id": lease_id}

    # --- results ---

    @staticmethod
    def _field(obj: Any, *names: str) -> Any:
        if isinstance(obj, dict):
            for name in names:
                if obj.get(name) is not None:
                    return obj[name]
        return None

    def _task_row(self, result: Any) -> Any:
        if isinstance(result, dict) and isinstance(result.get("task"), dict):
            return result["task"]
        return result

    def task_id_from(self, result: Any) -> str | None:
        value = self._field(self._task_row(result), "task_id", "id")
        return str(value) if value is not None else None

    def key_from(self, result: Any) -> str | None:
        value = self._field(self._task_row(result), "key")
        return str(value) if value is not None else None

    def claim_from(self, result: Any) -> dict:
        """{task, run, leases, reminder} -> the run and leases to hold."""
        run = result.get("run") if isinstance(result, dict) else None
        leases = result.get("leases") if isinstance(result, dict) else None
        leases = [lease for lease in (leases or []) if isinstance(lease, dict)]
        lease_ids = [
            str(self._field(lease, "lease_id", "id")) for lease in leases
            if self._field(lease, "lease_id", "id") is not None
        ]
        expiries = [str(lease["expires_at"]) for lease in leases if lease.get("expires_at")]
        run_id = self._field(run, "run_id", "id")
        return {
            "run_id": str(run_id) if run_id is not None else None,
            "lease_id": lease_ids[0] if lease_ids else None,
            "lease_ids": lease_ids,
            "expires_at": min(expiries) if expiries else None,
            "fencing_token": self._field(run, "fencing_token"),
            "epoch": self._field(run, "epoch") or 0,
        }

    def refusal_reason(self, message: str) -> str:
        """The leading ``Reason`` word of a refusal (``TaskHeld: agent-x``)."""
        return message.strip().split(":", 1)[0].strip()

    def is_lost_claim(self, message: str) -> bool:
        return self.refusal_reason(message) in self.LOST_CLAIM_REASONS

    # --- keys: service task keys <-> GitHub refs, in one place ---

    @staticmethod
    def default_key(owner_repo: str, number: int) -> str:
        return f"{owner_repo}#{number}"

    @staticmethod
    def gh_ref_for_key(key: str) -> str | None:
        """A GitHub ref for a service key, or None for a local-only task.

        ``local#<n>`` keys are tasks the service minted; they are not on
        GitHub until the service mirrors them, so unless the state file
        records which issue one was created for, there is no ref to give and
        GitHub-reading skills cannot see the task.
        """
        if _LOCAL_KEY_RE.match(key):
            return None
        match = _GH_KEY_RE.match(key)
        if match:
            return f"gh:{match.group('repo')}#{match.group('number')}"
        return None

    @staticmethod
    def is_local_key(key: str) -> bool:
        return bool(_LOCAL_KEY_RE.match(key))


# --- per-target state ----------------------------------------------------------


class ServiceState:
    """Ref <-> service key/task id, and the runs this machine holds."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = {"tasks": {}, "runs": {}}
        if path.is_file():
            loaded = json.loads(path.read_text())
            self.data["tasks"].update(loaded.get("tasks", {}))
            self.data["runs"].update(loaded.get("runs", {}))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def task(self, ref: str) -> dict:
        return self.data["tasks"].get(ref, {})

    def remember_task(self, ref: str, **fields: Any) -> None:
        record = self.data["tasks"].setdefault(ref, {})
        record.update({k: v for k, v in fields.items() if v is not None})
        self.save()

    def ref_for_key(self, key: str) -> str | None:
        for ref, record in self.data["tasks"].items():
            if record.get("key") == key:
                return ref
        return None

    def run(self, ref: str) -> dict | None:
        return self.data["runs"].get(ref)

    def hold(self, ref: str, run: dict) -> None:
        self.data["runs"][ref] = run
        self.save()

    def drop(self, ref: str) -> None:
        if self.data["runs"].pop(ref, None) is not None:
            self.save()


def state_path(user_dir: Path, owner_repo: str) -> Path:
    owner, _, repo = owner_repo.partition("/")
    return user_dir / "state" / "tracker" / owner / f"{repo}.json"
