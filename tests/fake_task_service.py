"""A fake MCP task service for the tracker's mcp backend tests (#498).

Implements the tool shapes the service exposes today (list_tasks, task,
claim_task, log_work, complete_task, log_decision, release) and, unless
``today=True``, the ones it is adding (create_task, targeted claim via
claim_task's task_id, comment_task). It enforces the rules the adapter must
respect: run notes and completion only from the run's holder, a decision
needs one of the four provenance classes and a source, one live claim per
task.

Usable in process (it is a Transport) or as a stdio MCP server:

    python3 fake_task_service.py --state STATE.json [--today]
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


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


S = {"type": "string"}
SN = {"anyOf": [{"type": "string"}, {"type": "null"}]}
LINKS = {"type": "array", "items": S}

TODAY_TOOLS = {
    "list_tasks": _obj({"state": SN, "status_by": SN}, []),
    "task": _obj({"task_id": S, "key": S}, []),
    "claim_task": _obj({
        "agent": S, "environment": S, "state": {"enum": ["GREEN", "AMBER", "RED"]},
        "stale": {"type": "boolean"}, "ceiling": {"type": "boolean"},
        "capabilities": {"type": "array", "items": S},
    }, ["environment", "agent", "state", "stale", "ceiling", "capabilities"]),
    "log_work": _obj({
        "task_id": S, "run_id": S, "text": S, "links": LINKS,
        "status": _obj({"done": S, "in_progress": S, "next": S, "blocked_on": SN},
                       ["done", "in_progress", "next"]),
    }, ["task_id", "run_id", "text", "links"]),
    "complete_task": _obj({"task_id": S, "run_id": S, "summary": S, "links": LINKS},
                          ["task_id", "run_id", "summary", "links"]),
    "log_decision": _obj({
        "scope": {"enum": ["task", "governor", "cap"]}, "task_id": SN, "decided_by": S,
        "channel": S, "what": S, "provenance_class": {"enum": PROVENANCE},
        "provenance_source": S, "approval_words": SN,
    }, ["scope", "decided_by", "channel", "what", "provenance_class", "provenance_source"]),
    "release": _obj({"lease_id": S}, ["lease_id"]),
}

ADDED_TOOLS = {
    "create_task": _obj({
        "key": S, "title": S, "body": S, "labels": LINKS, "links": LINKS,
        "provenance_class": {"enum": PROVENANCE}, "provenance_source": S, "created_by": S,
    }, ["title"]),
    "comment_task": _obj({"task_id": S, "text": S, "author": S}, ["task_id", "text"]),
}


class FakeTaskService:
    def __init__(self, *, today: bool = False, state_file: Path | None = None):
        self.today = today
        self.state_file = state_file
        self.db: dict[str, Any] = {"tasks": {}, "runs": {}, "decisions": [], "seq": 0}
        if state_file and state_file.is_file():
            self.db = json.loads(state_file.read_text())
        self.calls: list[tuple[str, dict]] = []

    # --- Transport ---------------------------------------------------------

    def tools(self) -> dict[str, dict]:
        tools = json.loads(json.dumps(TODAY_TOOLS))
        if not self.today:
            tools.update(json.loads(json.dumps(ADDED_TOOLS)))
            tools["claim_task"]["properties"]["task_id"] = S
        return tools

    def call(self, name: str, arguments: dict) -> Any:
        if name not in self.tools():
            raise TaskServiceError(f"unknown tool {name}")
        self.calls.append((name, dict(arguments)))
        result = getattr(self, f"_{name}")(arguments)
        if self.state_file:
            self.state_file.write_text(json.dumps(self.db))
        return result

    # --- helpers -------------------------------------------------------------

    def _next(self, prefix: str) -> str:
        self.db["seq"] += 1
        return f"{prefix}-{self.db['seq']}"

    def _find(self, task_id: str) -> dict:
        task = self.db["tasks"].get(task_id)
        if task is None:
            raise TaskServiceError(f"no task {task_id}")
        return task

    def _holder_run(self, task_id: str, run_id: str) -> dict:
        run = self.db["runs"].get(run_id)
        if run is None or run["task_id"] != task_id or run["state"] != "active":
            raise TaskServiceError(f"run {run_id} is not the live holder of {task_id}")
        return run

    def add_service_task(self, title: str, key: str | None = None) -> dict:
        """A task created on the service side, not through the adapter."""
        return self._create_task({"title": title, **({"key": key} if key else {})})

    # --- tools -------------------------------------------------------------------

    def _create_task(self, a: dict) -> dict:
        task_id = self._next("task")
        local_n = sum(1 for t in self.db["tasks"].values() if t["key"].startswith("local#")) + 1
        task = {
            "task_id": task_id,
            "key": a.get("key") or f"local#{local_n}",
            "title": a["title"],
            "state": "ready",
            "provenance_class": a.get("provenance_class"),
            "provenance_source": a.get("provenance_source"),
            "created_by": a.get("created_by"),
            "notes": [],
            "comments": [],
        }
        self.db["tasks"][task_id] = task
        return {"task_id": task_id, "key": task["key"]}

    def _task(self, a: dict) -> dict:
        if "task_id" in a:
            return self._find(a["task_id"])
        for task in self.db["tasks"].values():
            if task["key"] == a.get("key"):
                return task
        raise TaskServiceError(f"no task with key {a.get('key')}")

    def _list_tasks(self, a: dict) -> list:
        return [t for t in self.db["tasks"].values() if not a.get("state") or t["state"] == a["state"]]

    def _claim_task(self, a: dict) -> dict:
        task_id = a.get("task_id")
        if task_id is None:  # today's claim: next ready task
            ready = [t for t in self.db["tasks"].values() if t["state"] == "ready"]
            if not ready:
                raise TaskServiceError("no ready task")
            task_id = ready[0]["task_id"]
        task = self._find(task_id)
        if task["state"] == "done":
            raise TaskServiceError(f"{task_id} is not ready")
        for run in self.db["runs"].values():
            if run["task_id"] == task_id and run["state"] == "active":
                raise TaskServiceError(f"{task_id} is held by {run['agent']}")
        run_id, lease_id = self._next("run"), self._next("lease")
        self.db["runs"][run_id] = {
            "task_id": task_id, "agent": a["agent"], "state": "active", "lease_id": lease_id,
        }
        task["state"] = "doing"
        return {"task_id": task_id, "run_id": run_id, "lease_id": lease_id,
                "expires_at": "2026-11-03T12:00:00Z"}

    def _log_work(self, a: dict) -> dict:
        self._holder_run(a["task_id"], a["run_id"])
        task = self._find(a["task_id"])
        task["notes"].append({"run_id": a["run_id"], "text": a["text"], "status": a.get("status")})
        if (a.get("status") or {}).get("blocked_on"):
            task["state"] = "blocked"
        return {"ok": True}

    def _complete_task(self, a: dict) -> dict:
        run = self._holder_run(a["task_id"], a["run_id"])
        run["state"] = "released"
        task = self._find(a["task_id"])
        task["state"] = "done"
        task["summary"] = a["summary"]
        return {"ok": True}

    def _log_decision(self, a: dict) -> dict:
        if a.get("provenance_class") not in PROVENANCE or not a.get("provenance_source"):
            raise TaskServiceError("log_decision requires provenance_class and provenance_source")
        if a["scope"] == "task":
            self._find(a.get("task_id") or "")
        self.db["decisions"].append(dict(a))
        return {"decision_id": self._next("dec")}

    def _comment_task(self, a: dict) -> dict:
        self._find(a["task_id"])["comments"].append({"text": a["text"], "author": a.get("author")})
        return {"ok": True}

    def _release(self, a: dict) -> dict:
        for run in self.db["runs"].values():
            if run["lease_id"] == a["lease_id"] and run["state"] == "active":
                run["state"] = "released"
                self._find(run["task_id"])["state"] = "ready"
                return {"ok": True}
        raise TaskServiceError(f"no live lease {a['lease_id']}")


def serve(service: FakeTaskService) -> None:
    """Minimal MCP stdio server around the fake."""
    for line in sys.stdin:
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            continue
        if method == "initialize":
            result: Any = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "fake-task-service", "version": "0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": n, "inputSchema": s} for n, s in service.tools().items()]}
        elif method == "tools/call":
            params = message["params"]
            try:
                value = service.call(params["name"], params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(value)}],
                          "structuredContent": value if isinstance(value, dict) else {"items": value}}
            except TaskServiceError as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": request_id,
                              "error": {"code": -32601, "message": method}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--today", action="store_true")
    args = parser.parse_args()
    serve(FakeTaskService(today=args.today, state_file=Path(args.state)))
