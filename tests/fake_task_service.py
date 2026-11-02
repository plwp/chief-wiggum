"""A fake MCP task service for the tracker's mcp backend tests (#498).

Matches the service's tool shapes as checked against a running service on
2026-11-03: create_task, claim_task (named by ``task``, id or key),
log_work, complete_task, comment, log_decision, list_tasks, task,
heartbeat and release. Rows carry ``id``; ``task`` returns
``{task, notes, decisions, runs, status, deps, resources, labels, issue}``.

It enforces the service's rules, refusing with the service's reasons and
writing nothing on a refusal:

- create: ``MissingProvenance``, ``InvalidTask``, ``UnknownDependency``,
  ``UnknownResource``; a task with an unfinished dependency is ``blocked``;
- claim: the claiming agent is the caller (``AgentMismatch``), a named claim
  never answers "none" (``TaskNotFound``, ``TaskHeld: <agent>``,
  ``TaskNotReady: <state>``), and every class, ``agent`` included, is
  claimable at once; leases include ``slot:env:<environment>``;
- log_work and complete_task: the run's holder only (``RunOwner``);
- comment: any registered agent on any task, no state change
  (``TaskNotFound``, ``InvalidComment: text must be nonblank``);
- log_decision: provenance required; ``decided_by`` is the caller or a
  registered ``human:<name>`` it relays (``DecidedByMismatch``,
  ``UnknownHuman: <name>``), and relayed approval words are refused
  (``RelayedApprovalRefused``); ``task_id`` must be the task's id;
- missing or unexpected arguments are JSON-RPC -32602;
- paged reads (default, ``paged=False`` for the older unpaged service):
  ``list_tasks`` answers ``{tasks, next_cursor, total}``, ``limit`` rows a
  page (50), compact rows unless ``full: true``; ``task`` answers the newest
  20 notes (oldest first within the page, ``notes_limit`` 1 to 100) with
  ``notes_total`` and a ``notes_before`` cursor, null at the oldest page, that
  ``task`` accepts to page back; limit outside 1 to 200 and unissued cursors
  are -32602 (chief-wiggum#506).

``mirror(repo, gh)`` is the operator's mirror command: it writes the
service's tasks, comments, decisions, status and completion to a FakeGh
store (``issues`` and ``comments``), idempotently, in the service's own
comment formats (a hidden marker, then the text).

Usable in process (it is a Transport) or as a stdio MCP server:

    python3 fake_task_service.py --state STATE.json [--caller NAME]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from chief_wiggum.task_service import InvalidArguments, TaskServiceError  # noqa: E402

PROVENANCE = ["legal", "human", "customer", "agent"]
ACTORS = ["lead", "manager", "worker", "browser", "human"]
NOW = "2026-11-03T01:00:00Z"
MARK = "<!-- task-service:mirror"


class Refusal(TaskServiceError):
    """A service refusal: ``{"reason"}``, isError, nothing written."""


BadArguments = InvalidArguments


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


S = {"type": "string"}
SN = {"anyOf": [{"type": "string"}, {"type": "null"}]}
LIST = {"type": "array", "items": S}
LISTN = {"anyOf": [LIST, {"type": "null"}]}
# dtd-cns docs/mcp-tools.md, Ticket #54: strict, no trimming or case folding.
CITE_RE = re.compile(r"[a-z0-9-]+/REQ-[0-9]+@[0-9a-f]{64}")
BOOL = {"type": "boolean"}

TOOLS = {
    "list_tasks": _obj({"state": SN, "status_by": SN, "actor": SN, "lane": SN, "priority": SN,
                        "blocked_on": SN, "stale": {}, "unprovenanced": {}}, []),
    "task": _obj({"task_id": S, "key": S}, []),
    "create_task": _obj({
        "title": S, "actor": {"enum": ACTORS}, "priority": {"enum": ["P1", "P2", "P3"]},
        "done_when": S, "provenance_class": {"enum": PROVENANCE}, "provenance_source": S,
        "lane": SN, "requires": LISTN, "deps": LISTN, "resources": LISTN, "cites": LISTN,
        "repo": SN,
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
    "comment": _obj({"task": S, "text": S, "links": LISTN}, ["task", "text"]),
    "log_decision": _obj({
        "scope": {"enum": ["task", "governor", "cap"]}, "task_id": SN, "decided_by": S,
        "channel": S, "what": S, "provenance_class": {"enum": PROVENANCE},
        "provenance_source": S, "approval_words": SN, "matched_words": SN, "effect": {},
    }, ["scope", "decided_by", "channel", "what", "provenance_class", "provenance_source"]),
    "heartbeat": _obj({"lease_id": S}, ["lease_id"]),
    "release": _obj({"lease_id": S}, ["lease_id"]),
}


PAGED_TOOLS = {
    "list_tasks": _obj({**TOOLS["list_tasks"]["properties"], "cursor": SN,
                        "limit": {"type": "integer"}, "full": BOOL}, []),
    "task": _obj({"task_id": S, "key": S, "notes_before": SN, "notes_limit": {"type": "integer"}}, []),
}
COMPACT = ("id", "key", "title", "state", "priority", "actor", "lane", "provenance_class",
           "created_by", "repo")


_REPO_PART = re.compile(r"[A-Za-z0-9._-]+")


def valid_repo(repo: str) -> bool:
    """<owner>/<repo>: one slash, nonempty parts of [A-Za-z0-9._-], neither "." nor ".."."""
    parts = repo.split("/")
    return len(parts) == 2 and all(
        _REPO_PART.fullmatch(p) and p not in (".", "..") for p in parts
    )


def _empty_db() -> dict:
    return {"tasks": {}, "runs": {}, "leases": {}, "comments": [], "decisions": [],
            "run_notes": [], "deps": {}, "mirrors": {}, "seq": 0,
            "agents": [], "humans": ["maintainer"], "resources": []}


class FakeTaskService:
    def __init__(
        self, *, caller: str = "cw-agent", state_file: Path | None = None,
        db: dict | None = None, paged: bool = True, page_size: int = 50, notes_page: int = 20,
    ):
        self.paged, self.page_size, self.notes_page = paged, page_size, notes_page
        self.caller = caller
        self.state_file = state_file
        if db is not None:
            self.db = db
        elif state_file and state_file.is_file():
            self.db = json.loads(state_file.read_text())
        else:
            self.db = _empty_db()
        for name in (caller, "other"):
            if name not in self.db["agents"]:
                self.db["agents"].append(name)
        self.calls: list[tuple[str, dict]] = []
        self._tools = {**TOOLS, **PAGED_TOOLS} if paged else TOOLS

    def as_caller(self, name: str) -> FakeTaskService:
        """The same service, another authenticated agent."""
        return FakeTaskService(caller=name, state_file=self.state_file, db=self.db, paged=self.paged,
                               page_size=self.page_size, notes_page=self.notes_page)

    # --- Transport -----------------------------------------------------------

    def tools(self) -> dict[str, dict]:
        return json.loads(json.dumps(self._tools))

    def call(self, name: str, arguments: dict) -> Any:
        schema = self.tools().get(name)
        if schema is None:
            raise BadArguments(f"-32602 unknown tool {name}")
        missing = [k for k in schema["required"] if k not in arguments]
        extra = [k for k in arguments if k not in schema["properties"]]
        if missing or extra:
            raise BadArguments(f"-32602 {name}: missing {missing}, unexpected {extra}")
        self.calls.append((name, dict(arguments)))
        self._load()  # a database: the mirror may have written since the last call
        snapshot = json.dumps(self.db)
        try:
            result = getattr(self, f"_{name}")(arguments)
        except TaskServiceError:
            self.db.clear()
            self.db.update(json.loads(snapshot))  # a refusal writes nothing
            raise
        self._save()
        return result

    def _load(self) -> None:
        if self.state_file and self.state_file.is_file():
            self.db.clear()
            self.db.update(json.loads(self.state_file.read_text()))

    def _save(self) -> None:
        if self.state_file:
            self.state_file.write_text(json.dumps(self.db))

    # --- helpers ---------------------------------------------------------------

    def _next(self, prefix: str) -> str:
        self.db["seq"] += 1
        return f"{prefix}{self.db['seq']:04d}"

    def _lookup(self, name: str) -> dict | None:
        if name in self.db["tasks"]:
            return self.db["tasks"][name]
        for task in self.db["tasks"].values():
            if task["key"] == name:
                return task
        return None

    def _find(self, name: str) -> dict:
        task = self._lookup(name or "")
        if task is None:
            raise Refusal("TaskNotFound")
        return task

    def _holder_run(self, task_id: str, run_id: str) -> dict:
        run = self.db["runs"].get(run_id)
        if run is None or run["task_id"] != task_id or run["ended_at"] is not None:
            raise Refusal("RunOwner")
        if run["agent"] != self.caller:
            raise Refusal("RunOwner")
        return run

    def _live_run(self, task_id: str) -> dict | None:
        for run in self.db["runs"].values():
            if run["task_id"] == task_id and run["ended_at"] is None:
                return run
        return None

    def add_service_task(self, title: str, *, key: str | None = None, source: str = "local",
                         repo: str | None = None) -> dict:
        """A task created on the service side, not through the adapter."""
        row = self._create_task({
            "title": title, "actor": "worker", "priority": "P2", "done_when": "x",
            "provenance_class": "human", "provenance_source": "test", "repo": repo,
        })["task"]
        if key:
            row["key"] = key
            row["source"] = source
        self._save()
        return row

    def record(self, name: str) -> dict:
        """The ``task`` read, without logging a call."""
        return self._task({"key": name} if "#" in name else {"task_id": name})

    # --- tools -------------------------------------------------------------------

    def _create_task(self, a: dict) -> dict:
        repo = a.get("repo")
        if repo is not None:
            if not isinstance(repo, str):
                raise BadArguments("-32602 create_task: repo must be a string")
            if not valid_repo(repo):
                raise Refusal("InvalidRepo: repo must be <owner>/<repo>")
        if not (a.get("provenance_source") or "").strip():
            raise Refusal("MissingProvenance: provenance_source")
        for cite in a.get("cites") or []:
            if not CITE_RE.fullmatch(cite):
                raise Refusal(f"InvalidCite: {cite} is not <slug>/REQ-<n>@<body_sha256>")
        if not a["title"].strip():
            raise Refusal("InvalidTask: title must be nonblank")
        if not a["done_when"].strip():
            raise Refusal("InvalidTask: done_when must be nonblank")
        deps = []
        for dep in a.get("deps") or []:
            found = self._lookup(dep)
            if found is None:
                raise Refusal(f"UnknownDependency: {dep}")
            deps.append(found["id"])
        for resource in a.get("resources") or []:
            if resource not in self.db["resources"]:
                raise Refusal(f"UnknownResource: {resource}")
        task_id = self._next("T")
        local_n = sum(1 for t in self.db["tasks"].values() if t["key"].startswith("local#")) + 1
        blocked = any(self.db["tasks"][d]["state"] != "done" for d in deps)
        row = {
            "id": task_id, "key": f"local#{local_n}", "source": "local",
            "title": a["title"], "actor": a["actor"], "priority": a["priority"],
            "done_when": a["done_when"], "state": "blocked" if blocked else "ready",
            "created_by": self.caller, "provenance_class": a["provenance_class"],
            "provenance_source": a["provenance_source"], "lane": a.get("lane"),
            "repo": repo, "requires": a.get("requires"), "cites": list(a.get("cites") or []),
            "closed_at": None,
            "status_done": None, "status_in_progress": None, "status_next": None,
            "status_blocked_on": None, "status_by": None, "status_at": None,
        }
        self.db["tasks"][task_id] = row
        self.db["deps"][task_id] = deps
        return {"task": row}

    def _task(self, a: dict) -> dict:
        if bool(a.get("task_id")) == bool(a.get("key")):
            raise BadArguments("-32602 task: provide exactly one of task_id or key")
        task = self._find(a.get("task_id") or a.get("key") or "")
        tid = task["id"]
        runs = [
            {"run": run, "notes": [n for n in self.db["run_notes"] if n["run_id"] == run["id"]]}
            for run in self.db["runs"].values() if run["task_id"] == tid
        ]
        status = (
            {"done": task["status_done"], "in_progress": task["status_in_progress"],
             "blocked_on": task["status_blocked_on"], "next": task["status_next"],
             "by": task["status_by"], "at": task["status_at"]}
            if task["status_at"] else {"none": "no status yet"}
        )
        mirror = self.db["mirrors"].get(tid)
        notes = [c for c in self.db["comments"] if c["task_id"] == tid]
        paging: dict[str, Any] = {}
        if self.paged:
            end = len(notes)
            limit = a.get("notes_limit") or 20
            if not 1 <= limit <= 100:
                raise BadArguments("-32602 task: notes_limit must be 1 to 100")
            limit = min(limit, self.notes_page)  # the service also caps a page by size
            if a.get("notes_before"):
                end = next((i for i, n in enumerate(notes) if n["id"] == a["notes_before"]), None)
                if end is None:
                    raise BadArguments("-32602 task: notes_before was not issued by the server")
            shown = notes[max(0, end - limit):end]
            older = notes[:max(0, end - limit)]
            paging = {"notes_total": len(notes),
                      "notes_before": shown[0]["id"] if shown and older else None}
            notes = shown
        return {
            **paging,
            "task": task,
            "notes": notes,
            "decisions": [d for d in self.db["decisions"] if d["task_id"] == tid],
            "runs": runs,
            "status": status,
            "deps": [self.db["tasks"][d]["key"] for d in self.db["deps"].get(tid, [])],
            "resources": [],
            "labels": [],
            "issue": mirror["issue"] if mirror else None,
        }

    def _list_tasks(self, a: dict) -> dict:
        rows = [t for t in self.db["tasks"].values()
                if not a.get("state") or t["state"] == a["state"]]
        if not self.paged:
            return {"tasks": rows}
        if not 1 <= (a.get("limit") or 1) <= 200:
            raise BadArguments("-32602 list_tasks: limit must be 1 to 200")
        cursor = a.get("cursor")
        if cursor is not None and not (str(cursor).isdigit() and int(cursor) <= len(rows)):
            raise BadArguments("-32602 list_tasks: cursor was not issued by the server")
        start = int(cursor or 0)
        size = min(a.get("limit") or self.page_size, self.page_size)  # the service caps a page
        page = rows[start:start + size]
        if not a.get("full"):
            page = [{k: t[k] for k in COMPACT} for t in page]
        more = start + size < len(rows)
        return {"tasks": page, "next_cursor": str(start + size) if more else None,
                "total": len(rows)}

    def _claim_task(self, a: dict) -> dict:
        if a["agent"] != self.caller:
            raise Refusal("AgentMismatch")
        if a.get("task") is None:
            ready = [t for t in self.db["tasks"].values() if t["state"] == "ready"]
            if not ready:
                return {"task": None, "run": None, "leases": [], "reminder": "none"}
            task = ready[0]
        else:
            task = self._find(a["task"])
        held = self._live_run(task["id"])
        if held is not None:
            raise Refusal(f"TaskHeld: {held['agent']}")
        if task["state"] != "ready":
            raise Refusal(f"TaskNotReady: {task['state']}")
        run_id = self._next("R")
        run = {"id": run_id, "task_id": task["id"], "agent": a["agent"], "actor": task["actor"],
               "started_at": NOW, "ended_at": None, "outcome": None, "summary": None, "links": None}
        lease_id = self._next("L")
        lease = {"id": lease_id, "run_id": run_id, "task_id": task["id"], "owner": a["agent"],
                 "resource": f"slot:env:{a['environment']}", "acquired_at": NOW,
                 "heartbeat_at": NOW, "ttl_secs": 300, "released_at": None, "beats": 0}
        self.db["runs"][run_id] = run
        self.db["leases"][lease_id] = lease
        task["state"] = "doing"
        return {"task": task, "run": run, "leases": [lease],
                "reminder": "Report status with log_work status when you pause or complete."}

    def _log_work(self, a: dict) -> dict:
        task = self._find(a["task_id"])
        self._holder_run(task["id"], a["run_id"])
        note = {"id": self._next("N"), "run_id": a["run_id"], "at": NOW, "text": a["text"],
                "links": json.dumps(a["links"])}
        self.db["run_notes"].append(note)
        status = a.get("status")
        if status:
            if status.get("blocked_on") == "":
                raise BadArguments("-32602 log_work: blocked_on must be nonblank or null")
            task.update({"status_done": status["done"], "status_in_progress": status["in_progress"],
                         "status_next": status["next"], "status_blocked_on": status.get("blocked_on"),
                         "status_by": self.caller, "status_at": NOW})
        return {"ok": True}

    def _complete_task(self, a: dict) -> dict:
        task = self._find(a["task_id"])
        run = self._holder_run(task["id"], a["run_id"])
        run.update({"ended_at": NOW, "outcome": "done", "summary": a["summary"],
                    "links": json.dumps(a["links"])})
        for lease in self.db["leases"].values():
            if lease["run_id"] == a["run_id"]:
                lease["released_at"] = NOW
        task["state"] = "done"
        task["closed_at"] = NOW
        for other in self.db["tasks"].values():  # unblock dependants
            if other["state"] == "blocked" and all(
                self.db["tasks"][d]["state"] == "done" for d in self.db["deps"].get(other["id"], [])
            ):
                other["state"] = "ready"
        return {"ok": True}

    def _comment(self, a: dict) -> dict:
        if self.caller not in self.db["agents"]:
            raise Refusal("UnknownAgent")
        task = self._find(a["task"])
        if not a["text"].strip():
            raise Refusal("InvalidComment: text must be nonblank")
        note = {"id": self._next("C"), "task_id": task["id"], "author": self.caller,
                "body": a["text"], "links": list(a.get("links") or []), "created_at": NOW,
                "source": "comment"}
        self.db["comments"].append(note)
        return {"note": note}

    def _log_decision(self, a: dict) -> dict:
        if not (a.get("provenance_source") or "").strip():
            raise Refusal("MissingProvenance: provenance_source")
        relayed_by = None
        if a["decided_by"].startswith("human:"):
            name = a["decided_by"].removeprefix("human:")
            if name not in self.db["humans"]:
                raise Refusal(f"UnknownHuman: {name}")
            if a.get("approval_words"):
                raise Refusal("RelayedApprovalRefused")
            relayed_by = self.caller
        elif a["decided_by"] != self.caller:
            raise Refusal("DecidedByMismatch")
        if a["scope"] == "task":
            if not a.get("task_id") or a["task_id"] not in self.db["tasks"]:
                raise BadArguments("-32602 an argument names a row that does not exist")
        decision = {k: a.get(k) for k in TOOLS["log_decision"]["properties"]}
        decision.update({"id": self._next("D"), "at": NOW, "relayed_by": relayed_by})
        self.db["decisions"].append(decision)
        return {"ok": True}

    def _live_lease(self, lease_id: str) -> dict:
        lease = self.db["leases"].get(lease_id)
        if lease is None or lease["released_at"] is not None:
            raise Refusal(f"LeaseNotLive: {lease_id}")
        if lease["owner"] != self.caller:
            raise Refusal("LeaseOwner")
        return lease

    def _heartbeat(self, a: dict) -> dict:
        self._live_lease(a["lease_id"])["beats"] += 1
        return {"ok": True}

    def _release(self, a: dict) -> dict:
        lease = self._live_lease(a["lease_id"])
        lease["released_at"] = NOW
        run = self.db["runs"][lease["run_id"]]
        if run["ended_at"] is None:
            run.update({"ended_at": NOW, "outcome": "released"})
            self.db["tasks"][run["task_id"]]["state"] = "ready"
        return {"ok": True}

    # --- the operator's mirror command ---------------------------------------------

    def mirror(self, repo: str, gh: Any) -> list[str]:
        """Write the service's tasks to GitHub (a FakeGh store), idempotently."""
        written: list[str] = []
        prefix = repo.split("/", 1)[-1] + "#"
        for task in self.db["tasks"].values():
            tid = task["id"]
            record = self.db["mirrors"].get(tid)
            if record is None:
                if task["source"] == "github" and task["key"].startswith(prefix):
                    number = int(task["key"][len(prefix):])
                elif task["source"] == "local" and task.get("repo") == repo:
                    number = gh._next_number.get(repo, 1)
                    gh._next_number[repo] = number + 1
                    gh.issues.setdefault(repo, {})[number] = {
                        "number": number, "title": task["title"], "state": "open",
                        "body": (f"{MARK} issue -->\n\n- Done when: {task['done_when']}\n"
                                 f"- Provenance: {task['provenance_class']}, "
                                 f"\"{task['provenance_source']}\"\n"
                                 f"- Created by: {task['created_by']}\n\n"
                                 f"Created in the task service as {task['key']}"),
                        "labels": [], "assignees": [], "milestone": None,
                        "url": f"https://github.com/{repo}/issues/{number}",
                    }
                    written.append(f"{task['key']}: created issue {repo}#{number}")
                else:
                    continue
                record = {"issue": f"{repo}#{number}", "writes": [], "status_index": None}
                self.db["mirrors"][tid] = record
            number = int(record["issue"].rpartition("#")[2])
            comments = gh.comments.setdefault((repo, number), [])
            for note in [c for c in self.db["comments"] if c["task_id"] == tid]:
                if f"comment:{note['id']}" in record["writes"]:
                    continue
                links = f"\n\nLinks: {', '.join(note['links'])}" if note["links"] else ""
                comments.append(f"{MARK} comment {note['id']} -->\n**Comment from "
                                f"{note['author']}**\n\n{note['body']}{links}")
                record["writes"].append(f"comment:{note['id']}")
                written.append(f"{task['key']}: posted comment {note['id']}")
            for d in [d for d in self.db["decisions"] if d["task_id"] == tid]:
                if f"decision:{d['id']}" in record["writes"]:
                    continue
                relay = f", relayed by {d['relayed_by']}" if d["relayed_by"] else ""
                comments.append(f"{MARK} decision {d['id']} -->\n**Decision ({d['decided_by']}"
                                f"{relay})**\n\n{d['what']}\n\nProvenance: "
                                f"{d['provenance_class']}, \"{d['provenance_source']}\"")
                record["writes"].append(f"decision:{d['id']}")
                written.append(f"{task['key']}: posted decision {d['id']}")
            if task["status_at"]:
                body = (f"{MARK} status -->\n**Status** from {task['status_by']} at "
                        f"{task['status_at']}\n\n- Done: {task['status_done']}\n"
                        f"- In progress: {task['status_in_progress']}\n"
                        f"- Blocked on: {task['status_blocked_on'] or 'nothing'}\n"
                        f"- Next: {task['status_next']}")
                if record["status_index"] is None:
                    comments.append(body)
                    record["status_index"] = len(comments) - 1
                    written.append(f"{task['key']}: posted status")
                elif comments[record["status_index"]] != body:
                    comments[record["status_index"]] = body
                    written.append(f"{task['key']}: edited status")
            if task["state"] == "done" and "closed" not in record["writes"]:
                [run] = [r for r in self.db["runs"].values()
                         if r["task_id"] == tid and r["outcome"] == "done"][-1:]
                links = json.loads(run["links"] or "[]")
                comments.append(f"{MARK} closing -->\n**Done** by {run['agent']}, run "
                                f"{run['id']}\n\n{run['summary']}"
                                + (f"\n\nLinks: {', '.join(links)}" if links else ""))
                gh.issues[repo][number]["state"] = "closed"
                record["writes"].append("closed")
                written.append(f"{task['key']}: closed issue")
        self._save()
        return written


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
                reply["error"] = {"code": -32602, "message": str(exc).removeprefix("-32602 ")}
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
    args = parser.parse_args()
    serve(FakeTaskService(caller=args.caller, state_file=Path(args.state)))
