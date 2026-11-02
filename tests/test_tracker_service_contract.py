"""The round-trip contract against a real task service (chief-wiggum#498).

The fake in fake_task_service.py is what every other test runs against; this
file checks the fake's shapes are the service's. Set ``CW_TASK_SERVICE_BIN``
to a service binary that offers, besides ``mcp --agent NAME --db PATH``:

- ``env add`` and ``agent add`` (an environment, an agent, a human), and
- ``mirror --repo OWNER/REPO --api-url http://127.0.0.1:PORT --db PATH``.

The service runs on a scratch database, and its mirror writes to a local fake
of the GitHub REST API backed by the same store CW's ``gh`` fake reads, so
nothing leaves the machine. The flow: create, claim, status, comment,
decision and complete through the service; mirror; then read back from
GitHub and from the service and compare.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import tracker
from chief_wiggum.task_service import ServiceState, StdioTransport, TaskServiceError
from test_tracker_mcp import _round_trip
from test_tracker_work_verbs import RecordingGh
from tracker import IssueDraft
from tracker_mcp import McpTaskBackend

BIN = os.environ.get("CW_TASK_SERVICE_BIN", "")
REPO = "acme/app"

pytestmark = pytest.mark.skipif(
    not (BIN and Path(BIN).is_file()), reason="CW_TASK_SERVICE_BIN not set to a service binary"
)

_USER = {"login": "mirror-bot", "id": 1, "node_id": "u", "avatar_url": "http://x",
         "gravatar_id": "", "url": "http://x", "html_url": "http://x",
         "followers_url": "http://x", "following_url": "http://x", "gists_url": "http://x",
         "starred_url": "http://x", "subscriptions_url": "http://x",
         "organizations_url": "http://x", "repos_url": "http://x", "events_url": "http://x",
         "received_events_url": "http://x", "type": "User", "site_admin": False}


class GithubApi:
    """The slice of the GitHub REST API an issue mirror uses, over a FakeGh store."""

    def __init__(self, gh: RecordingGh):
        self.gh = gh
        self.comment_ids: dict[int, list[int]] = {}
        self.next_comment = 9000
        self.log: list[tuple[str, str]] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _reply(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                api.log.append((method, self.path))
                code, obj = api.route(method, self.path.split("?")[0], body)
                self._reply(code, obj)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PATCH(self):
                self._handle("PATCH")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _issue(self, n):
        d = self.gh.issues[REPO][n]
        return {"id": 1000 + n, "node_id": "i", "number": n, "title": d["title"],
                "body": d["body"], "state": d["state"], "labels": d["labels"],
                "url": f"{self.url}/repos/{REPO}/issues/{n}",
                "html_url": f"https://github.com/{REPO}/issues/{n}",
                "repository_url": "http://x", "labels_url": "http://x",
                "comments_url": "http://x", "events_url": "http://x", "user": _USER,
                "assignees": [], "author_association": "OWNER", "locked": False,
                "comments": len(self.gh.comments.get((REPO, n), [])),
                "created_at": "2026-11-03T00:00:00Z", "updated_at": "2026-11-03T00:00:00Z"}

    def _comment(self, n, i):
        cid = self.comment_ids[n][i]
        return {"id": cid, "node_id": "c", "url": "http://x",
                "html_url": f"https://github.com/{REPO}/issues/{n}#issuecomment-{cid}",
                "body": self.gh.comments[(REPO, n)][i], "user": _USER,
                "created_at": "2026-11-03T00:00:00Z", "updated_at": "2026-11-03T00:00:00Z",
                "issue_url": "http://x", "author_association": "OWNER"}

    def route(self, method, path, body):
        issues = self.gh.issues.setdefault(REPO, {})
        if path == f"/repos/{REPO}/issues":
            if method == "POST":
                n = self.gh._next_number.get(REPO, 1)
                self.gh._next_number[REPO] = n + 1
                issues[n] = {"number": n, "title": body["title"], "body": body.get("body") or "",
                             "state": "open", "labels": [], "assignees": [], "milestone": None,
                             "url": f"https://github.com/{REPO}/issues/{n}"}
                return 201, self._issue(n)
            return 200, [self._issue(n) for n in issues]
        if m := re.fullmatch(rf"/repos/{REPO}/issues/(\d+)", path):
            n = int(m.group(1))
            if n not in issues:
                return 404, {"message": "Not Found"}
            if method == "PATCH":
                if "state" in body:
                    issues[n]["state"] = body["state"]
                for name in ("title", "body"):
                    if name in body:
                        issues[n][name] = body[name]
            return 200, self._issue(n)
        if m := re.fullmatch(rf"/repos/{REPO}/issues/(\d+)/comments", path):
            n = int(m.group(1))
            comments = self.gh.comments.setdefault((REPO, n), [])
            ids = self.comment_ids.setdefault(n, [])
            if method == "POST":
                self.next_comment += 1
                comments.append(body["body"])
                ids.append(self.next_comment)
                return 201, self._comment(n, len(comments) - 1)
            return 200, [self._comment(n, i) for i in range(len(comments))]
        if m := re.fullmatch(rf"/repos/{REPO}/issues/comments/(\d+)", path):
            cid = int(m.group(1))
            for n, ids in self.comment_ids.items():
                if cid in ids:
                    i = ids.index(cid)
                    if method == "PATCH":
                        self.gh.comments[(REPO, n)][i] = body["body"]
                    return 200, self._comment(n, i)
        return 404, {"message": "Not Found"}


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-user"))
    db = str(tmp_path / "service.db")

    def operator(*argv):
        subprocess.run([BIN, *argv, "--db", db], check=True, capture_output=True, text=True)

    operator("env", "add", "--id", "cw-test", "--runtime", "claude", "--billing",
             "subscription", "--provider", "anthropic", "--capacity", "2")
    operator("agent", "add", "--name", "cw-agent", "--environment", "cw-test")
    operator("agent", "add", "--name", "maintainer", "--human")
    gh = RecordingGh()
    api = GithubApi(gh)
    transports: list[StdioTransport] = []

    def mirror():
        # A placeholder token for the local fake API, not a credential: it
        # keeps the binary from asking gh for the operator's real one.
        result = subprocess.run(
            [BIN, "mirror", "--repo", REPO, "--api-url", api.url, "--db", db],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "GITHUB_TOKEN": "local-fake-api"},
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)

    def backend(read="github", *, state_name="state.json"):
        transport = StdioTransport(
            [BIN, "mcp", "--agent", "cw-agent", "--zone", "Australia/Melbourne", "--db", db])
        transports.append(transport)
        return McpTaskBackend(
            REPO, transport=transport, github=tracker.GithubBackend(REPO, runner=gh),
            config={"agent": "cw-agent", "environment": "cw-test", "read": read},
            state=ServiceState(tmp_path / state_name), notify=lambda _m: None, mirror=mirror)

    yield backend, gh, api
    for transport in transports:
        transport.close()
    api.server.shutdown()


def test_round_trip_through_the_real_service(service):
    backend, gh, api = service
    writer = backend()
    ref = _round_trip(writer)
    assert ref == f"gh:{REPO}#1"
    report = writer.compare(ref)
    assert report["mismatches"] == [], json.dumps(report, indent=2)
    assert report["github"]["state"] == "closed"
    # Every GitHub write was the service's mirror: CW made none of its own.
    assert [c for c in gh.calls if c[1:3] in (["issue", "comment"], ["issue", "close"],
                                               ["issue", "create"])] == []
    # The two read sources agree, from a machine that never saw the writes.
    reader = backend("service", state_name="reader.json")
    from_service, from_github = reader.get(ref), writer.get(ref)
    assert (from_service.ref, from_service.title, from_service.state) == (
        from_github.ref, from_github.title, from_github.state)


def test_the_mirror_is_idempotent_and_the_comparison_bites(service):
    backend, gh, api = service
    writer = backend()
    ref = _round_trip(writer)
    before = [list(v) for v in gh.comments.values()]
    writer.run_mirror()
    assert [list(v) for v in gh.comments.values()] == before
    gh.comments[(REPO, 1)].pop(0)
    assert writer.compare(ref)["mismatches"]


def test_refusals_carry_the_services_reasons(service):
    backend, _, _ = service
    writer = backend()
    ref = writer.create(IssueDraft(title="T"))
    with pytest.raises(TaskServiceError, match="InvalidComment"):
        writer.comment(ref, "  ")
    with pytest.raises(TaskServiceError, match="UnknownHuman"):
        writer.log_decision(ref, tracker.Decision(
            what="x", decided_by="stranger", provenance=tracker.Provenance("human", "s")))
    assert writer.claim(ref, "w") is True
    other = backend(state_name="other.json")
    assert other.claim(ref, "w2") is False  # TaskHeld
