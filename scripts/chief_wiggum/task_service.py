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
        raise TaskServiceError("; ".join(texts) or "tool call failed")
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
    # e.g. a targeted claim needs claim_task to accept a task_id.
    requires: tuple[str, ...] = ()


_GH_KEY_RE = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+)$")
_LOCAL_KEY_RE = re.compile(r"^local#\d+$")


class ServiceMapping:
    """Tracker verbs -> service tool calls. Swap names and shapes here only."""

    TOOLS: dict[str, ToolSpec] = {
        "read": ToolSpec("task"),
        "list": ToolSpec("list_tasks"),
        "status": ToolSpec("log_work"),
        "complete": ToolSpec("complete_task"),
        "decision": ToolSpec("log_decision"),
        "release": ToolSpec("release"),
        "create": ToolSpec("create_task", pending=True),
        "claim": ToolSpec("claim_task", pending=True, requires=("task_id",)),
        "comment": ToolSpec("comment_task", pending=True),
    }

    # CW status -> the service's run-status note. The service's log_work
    # status is a {done, in_progress, next, blocked_on} note written by the
    # run's holder, not a state enum.
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
        return {"task_id": task_id, "run_id": run_id, "text": body, "links": [link]}

    def comment_args(self, task_id: str, body: str, author: str | None) -> dict:
        args = {"task_id": task_id, "text": body}
        if author:
            args["author"] = author
        return args

    def complete_args(self, task_id: str, run_id: str, summary: str, link: str) -> dict:
        return {"task_id": task_id, "run_id": run_id, "summary": summary, "links": [link]}

    def decision_args(self, task_id: str, decision: Any) -> dict:
        channel = decision.channel or "cw-tracker"
        if decision.relayed_by:
            # The service has no relay field yet; the channel names the relay.
            channel = f"{channel} (relayed by {decision.relayed_by})"
        return {
            "scope": "task",
            "task_id": task_id,
            "decided_by": decision.decided_by,
            "channel": channel,
            "what": decision.what,
            "provenance_class": decision.provenance.klass,
            "provenance_source": decision.provenance.source,
            "approval_words": decision.approval_words,
        }

    def create_args(self, key: str, draft: Any, link: str) -> dict:
        args: dict[str, Any] = {
            "key": key,
            "title": draft.title,
            "body": draft.body or "",
            "labels": list(draft.labels),
            "links": [link],
        }
        if draft.provenance is not None:
            args["provenance_class"] = draft.provenance.klass
            args["provenance_source"] = draft.provenance.source
            if draft.provenance.actor:
                args["created_by"] = draft.provenance.actor
        return args

    def claim_args(self, task_id: str, agent: str, config: dict) -> dict:
        return {
            "task_id": task_id,
            "agent": agent,
            "environment": config.get("environment", "chief-wiggum"),
            "state": "GREEN",
            "stale": False,
            "ceiling": False,
            "capabilities": list(config.get("capabilities", [])),
        }

    def read_args(self, key: str) -> dict:
        return {"key": key}

    def release_args(self, lease_id: str) -> dict:
        return {"lease_id": lease_id}

    # --- results ---

    @staticmethod
    def _get(result: Any, *names: str) -> Any:
        if not isinstance(result, dict):
            return None
        for name in names:
            if result.get(name) is not None:
                return result[name]
        for nested in ("task", "run", "lease"):
            inner = result.get(nested)
            if isinstance(inner, dict):
                for name in names:
                    if inner.get(name) is not None:
                        return inner[name]
        return None

    def task_id_from(self, result: Any) -> str | None:
        value = self._get(result, "task_id", "id")
        return str(value) if value is not None else None

    def key_from(self, result: Any) -> str | None:
        value = self._get(result, "key")
        return str(value) if value is not None else None

    def claim_from(self, result: Any) -> dict:
        return {
            "run_id": self._get(result, "run_id"),
            "lease_id": self._get(result, "lease_id"),
            "expires_at": self._get(result, "expires_at"),
            "fencing_token": self._get(result, "fencing_token"),
            "epoch": self._get(result, "epoch") or 0,
        }

    REFUSAL_MARKERS = ("held by", "already claimed", "not ready", "ClaimRefused")

    def is_claim_refusal(self, message: str) -> bool:
        return any(marker.lower() in message.lower() for marker in self.REFUSAL_MARKERS)

    # --- keys: service task keys <-> GitHub refs, in one place ---

    @staticmethod
    def default_key(owner_repo: str, number: int) -> str:
        return f"{owner_repo}#{number}"

    @staticmethod
    def gh_ref_for_key(key: str) -> str | None:
        """A GitHub ref for a service key, or None for a local-only task.

        ``local#<n>`` keys are tasks created on the service that are not on
        GitHub until the service mirrors them; they are not visible to the
        GitHub-reading skills, so there is no ref to give.
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
