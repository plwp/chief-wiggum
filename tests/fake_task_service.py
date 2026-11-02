"""A fake MCP task service for the tracker's mcp backend tests (#498).

Implements the tool shapes the service owner confirmed on 2026-11-03:
list_tasks, task, create_task, claim_task (targeted by ``task``, or the next
ready task without it), log_work, complete_task, log_decision, heartbeat and
release. ``comment_task`` (a comment outside a run) is not built on the
service yet; ``with_comment_task=True`` adds it to exercise that path.

It enforces the rules the adapter must respect: provenance on every create
and decision, a claiming agent equal to the authenticated caller, run notes
and completion from the run's holder only, one live claim per task, and
refusals as ``{"reason"}`` with nothing written.

Usable in process (it is a Transport) or as a stdio MCP server:

    python3 fake_task_service.py --state STATE.json [--caller NAME] [--with-comment-task]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from chief_wiggum.task_service import TaskServiceError  # noqa: E402

PROVENANCE = ["legal", "human", "customer", "agent"]
ACTORS = ["lead", "manager", "worker", "browser", "human"]


class Refusal(TaskServiceError):
    """A service refusal: ``{"reason"}``, isError, nothing written."""


class BadArguments(TaskServiceError):
    """JSON-RPC -32602."""


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


S = {"type": "string"}
SN = {"anyOf": [{"type": "string"}, {"type": "null"}]}
LIST = {"type": "array", "items": S}
LISTN = {"anyOf": [LIST, {"type": "null"}]}
BOOL = {"type": "boolean"}

TOOLS = {
    "list_tasks": _obj({"state": SN, "status_by": SN, "actor": SN}, []),
    "task": _obj({"task_id": S, "key": S}, []),
    "create_task": _obj({
        "title": S, "actor": {"enum": ACTORS}, "priority": {"enum": ["P1", "P2", "P3"]},
        "done_when": S, "provenance_class": {"enum": PROVENANCE}, "provenance_source": S,
        "lane": SN, "requires": LISTN, "deps": LISTN, "resources": LISTN,
    }, ["title", "actor", "priority", "done_when", "provenance_class", "provenance_source"]),
    "claim_task": _obj({
        "environment": S, "agent": S, "state": {"enum": ["GREEN", "AMBER", "RED"]},
        "stale": BOOL, "ceiling": BOOL, "capabilities": LIST, "task": SN,
    }, ["environment", "agent", "state", "stale", "ceiling", "capabilities"]),
    "log_work": _obj({
        "task_id": S, "run_id": S, "text": S, "links": LIST,
        "status": _obj({"done": S, "in_progress": S, "next": S, "blocked_on": SN},
                       ["done", "in_progress", "next"]),
    }, ["task_id", "run_id", "text", "links"]),
    "complete_task": _obj({"task_id": S, "run_id": S, "summary": S, "links": LIST},
                          ["task_id", "run_id", "summary", "links"]),
    "log_decision": _obj({
        "scope": {"enum": ["task", "governor", "cap"]}, "task_id": SN, "decided_by": S,
        "channel": S, "what": S, "provenance_class": {"enum": PROVENANCE},
        "provenance_source": S, "approval_words": SN, "matched_words": SN, "effect": {},
    }, ["scope", "decided_by", "channel", "what", "provenance_class", "provenance_source"]),
    "heartbeat": _obj({"lease_id": S}, ["lease_id"]),
    "release": _obj({"lease_id": S}, ["lease_id"]),
}

COMMENT_TASK = _obj({"task_id": S, "text": S, "author": S}, ["task_id", "text"])


class FakeTaskService:
    def __init__(
        self, *, caller: str = "cw-agent", with_comment_task: bool = False,
        state_file: Path | None = None,
    ):
        self.caller = caller
        self.with_comment_task = with_comment_task
        self.state_file = state_file
        self.db: dict[str, Any] = {"tasks": {}, "runs": {}, "leases": {}, "decisions": [], "seq": 0}
        if state_file and state_file.is_file():
            self.db = json.loads(state_file.read_text())
        self.calls: list[tuple[str, dict]] = []

    # --- Transport -----------------------------------------------------------

    def tools(self) -> dict[str, dict]:
        tools = json.loads(json.dumps(TOOLS))
        if self.with_comment_task:
            tools["comment_task"] = json.loads(json.dumps(COMMENT_TASK))
        return tools

    def call(self, name: str, arguments: dict) -> Any:
        schema = self.tools().get(name)
        if schema is None:
            raise BadArguments(f"unknown tool {name}")
        missing = [k for k in schema["required"] if k not in arguments]
        extra = [k for k in arguments if k not in schema["properties"]]
        if missing or extra:
            raise BadArguments(f"-32602 {name}: missing {missing}, unexpected {extra}")
        self.calls.append((name, dict(arguments)))
        snapshot = json.dumps(self.db)
        try:
            result = getattr(self, f"_{name}")(arguments)
        except TaskServiceError:
            self.db = json.loads(snapshot)  # a refusal writes nothing
            raise
        if self.state_file:
            self.state_file.write_text(json.dumps(self.db))
        return result

    # --- helpers ---------------------------------------------------------------

    def _next(self, prefix: str) -> str:
        self.db["seq"] += 1
        return f"{prefix}-{self.db['seq']}"

    def _lookup(self, name: str) -> dict | None:
        if name in self.db["tasks"]:
            return self.db["tasks"][name]
        for task in self.db["tasks"].values():
            if task["key"] == name:
                return task
        return None

    def _find(self, task_id: str) -> dict:
        task = self._lookup(task_id or "")
        if task is None:
            raise Refusal(f"TaskNotFound: {task_id}")
        return task

    def _holder_run(self, task_id: str, run_id: str) -> dict:
        run = self.db["runs"].get(run_id)
        if run is None or run["task_id"] != task_id or run["state"] != "active":
            raise Refusal(f"NotHolder: run {run_id} does not hold {task_id}")
        return run

    def add_service_task(self, title: str, *, key: str | None = None) -> dict:
        """A task created on the service side, not through the adapter."""
        row = self._create_task({
            "title": title, "actor": "worker", "priority": "P2", "done_when": "x",
            "provenance_class": "human", "provenance_source": "test",
        })["task"]
        if key:
            row["key"] = key
        return row

    # --- tools -------------------------------------------------------------------

    def _create_task(self, a: dict) -> dict:
        if not (a.get("provenance_source") or "").strip():
            raise Refusal("MissingProvenance: provenance_source")
        if not a["title"].strip():
            raise Refusal("InvalidTask: title must be nonblank")
        if not a["done_when"].strip():
            raise Refusal("InvalidTask: done_when must be nonblank")
        deps = []
        for dep in a.get("deps") or []:
            found = self._lookup(dep)
            if found is None:
                raise Refusal(f"UnknownDependency: {dep}")
            deps.append(found["task_id"])
        task_id = self._next("task")
        local_n = sum(1 for t in self.db["tasks"].values() if t["source"] == "local") + 1
        blocked = any(self.db["tasks"][d]["state"] != "done" for d in deps)
        row = {
            "task_id": task_id, "key": f"local#{local_n}", "source": "local",
            "title": a["title"], "actor": a["actor"], "priority": a["priority"],
            "done_when": a["done_when"], "state": "blocked" if blocked else "ready",
            "created_by": self.caller, "provenance_class": a["provenance_class"],
            "provenance_source": a["provenance_source"], "lane": a.get("lane"),
            "deps": deps, "notes": [], "comments": [],
        }
        self.db["tasks"][task_id] = row
        return {"task": row}

    def _task(self, a: dict) -> dict:
        return {"task": self._find(a.get("task_id") or a.get("key") or "")}

    def _list_tasks(self, a: dict) -> dict:
        return {"tasks": [t for t in self.db["tasks"].values()
                          if not a.get("state") or t["state"] == a["state"]]}

    def _claim_task(self, a: dict) -> dict:
        if a["agent"] != self.caller:
            raise Refusal(f"ActorMismatch: {a['agent']}")
        if a.get("task") is None:
            ready = [t for t in self.db["tasks"].values() if t["state"] == "ready"]
            if not ready:
                return {"task": None, "run": None, "leases": [], "reminder": "none"}
            task = ready[0]
        else:
            task = self._find(a["task"])
        for run in self.db["runs"].values():
            if run["task_id"] == task["task_id"] and run["state"] == "active":
                raise Refusal(f"TaskHeld: {run['agent']}")
        if task["state"] != "ready":
            raise Refusal(f"TaskNotReady: {task['state']}")
        run_id = self._next("run")
        leases = []
        for resource in (f"task:{task['task_id']}", f"slot:env:{a['environment']}"):
            lease_id = self._next("lease")
            self.db["leases"][lease_id] = {"run_id": run_id, "resource": resource, "beats": 0,
                                           "state": "active"}
            leases.append({"lease_id": lease_id, "resource": resource,
                           "expires_at": "2026-11-03T12:00:00Z"})
        self.db["runs"][run_id] = {"task_id": task["task_id"], "agent": a["agent"], "state": "active"}
        task["state"] = "doing"
        return {"task": task, "run": {"run_id": run_id, "agent": a["agent"]},
                "leases": leases, "reminder": "heartbeat every 60s"}

    def _log_work(self, a: dict) -> dict:
        self._holder_run(a["task_id"], a["run_id"])
        task = self._find(a["task_id"])
        task["notes"].append({"run_id": a["run_id"], "text": a["text"], "status": a.get("status")})
        return {"ok": True}

    def _complete_task(self, a: dict) -> dict:
        run = self._holder_run(a["task_id"], a["run_id"])
        run["state"] = "done"
        for lease in self.db["leases"].values():
            if lease["run_id"] == a["run_id"]:
                lease["state"] = "released"
        task = self._find(a["task_id"])
        task["state"] = "done"
        task["summary"] = a["summary"]
        return {"task": task}

    def _log_decision(self, a: dict) -> dict:
        if not (a.get("provenance_source") or "").strip():
            raise Refusal("MissingProvenance: provenance_source")
        if a["scope"] == "task":
            self._find(a.get("task_id") or "")
        self.db["decisions"].append(dict(a))
        return {"decision_id": self._next("dec")}

    def _comment_task(self, a: dict) -> dict:
        self._find(a["task_id"])["comments"].append({"text": a["text"], "author": a.get("author")})
        return {"ok": True}

    def _live_lease(self, lease_id: str) -> dict:
        lease = self.db["leases"].get(lease_id)
        if lease is None or lease["state"] != "active":
            raise Refusal(f"LeaseNotLive: {lease_id}")
        return lease

    def _heartbeat(self, a: dict) -> dict:
        self._live_lease(a["lease_id"])["beats"] += 1
        return {"lease_id": a["lease_id"], "expires_at": "2026-11-03T12:05:00Z"}

    def _release(self, a: dict) -> dict:
        lease = self._live_lease(a["lease_id"])
        lease["state"] = "released"
        run = self.db["runs"][lease["run_id"]]
        if run["state"] == "active":
            run["state"] = "released"
            self.db["tasks"][run["task_id"]]["state"] = "ready"
        return {"ok": True}


def serve(service: FakeTaskService) -> None:
    """Minimal MCP stdio server around the fake."""
    for line in sys.stdin:
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            continue
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if method == "initialize":
            reply["result"] = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                               "serverInfo": {"name": "fake-task-service", "version": "0"}}
        elif method == "tools/list":
            reply["result"] = {"tools": [{"name": n, "inputSchema": s}
                                         for n, s in service.tools().items()]}
        elif method == "tools/call":
            params = message["params"]
            try:
                value = service.call(params["name"], params.get("arguments") or {})
                reply["result"] = {"content": [{"type": "text", "text": json.dumps(value)}],
                                   "structuredContent": value}
            except BadArguments as exc:
                reply["error"] = {"code": -32602, "message": str(exc)}
            except TaskServiceError as exc:
                reply["result"] = {"content": [{"type": "text", "text": str(exc)}],
                                   "structuredContent": {"reason": str(exc)}, "isError": True}
        else:
            reply["error"] = {"code": -32601, "message": method}
        print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--caller", default="cw-agent")
    parser.add_argument("--with-comment-task", action="store_true")
    args = parser.parse_args()
    serve(FakeTaskService(caller=args.caller, with_comment_task=args.with_comment_task,
                          state_file=Path(args.state)))
