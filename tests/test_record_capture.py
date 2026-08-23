"""Tests for the capture recorder (chief-wiggum#351)."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "record_capture.py"
sys.path.insert(0, str(REPO / "scripts"))

from record_capture import _parse_headers, _redact_request_headers, capture  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    def _reply(self):
        # Drain the request body before replying. A handler that answers
        # without consuming it leaves bytes in the socket, and the client sees
        # ConnectionResetError instead of the response — a flake that only
        # shows up under load.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path == "/boom":
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"unavailable"}')
            return
        if self.path == "/text":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"plain body")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Trace-Id", "abc123")
        self.end_headers()
        self.wfile.write(b'{"opens":"9am"}')

    do_GET = _reply
    do_POST = _reply

    def log_message(self, *args, **kwargs):
        pass


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# --- secrets never enter a committed capture ----------------------------------

def test_request_header_values_are_never_recorded():
    """A capture is a committed artifact. An Authorization header written into
    one is a credential leak."""
    out = _redact_request_headers({"Authorization": "Bearer sk-live-secret",
                                   "X-Api-Key": "hunter2"})
    assert out == {"Authorization": "<not recorded>", "X-Api-Key": "<not recorded>"}
    assert "sk-live-secret" not in json.dumps(out)
    assert "hunter2" not in json.dumps(out)


def test_every_request_header_is_dropped_not_just_obvious_ones(server):
    """Guessing which custom header carries a credential is exactly the guess
    that leaks one."""
    record = capture(server + "/", "GET",
                     {"X-Company-Internal-Thing": "s3cr3t"}, None, "SCP")
    assert "s3cr3t" not in json.dumps(record)
    assert record["request"]["headers"]["X-Company-Internal-Thing"] == "<not recorded>"


def test_a_secret_in_the_body_is_not_stored_verbatim(server):
    """The body is recorded as present/absent only, never its content."""
    record = capture(server + "/", "POST", {}, b'{"password":"hunter2"}', "SCP")
    assert record["request"]["body_present"] is True
    assert "hunter2" not in json.dumps(record)


# --- provenance the gate requires ---------------------------------------------

def test_the_capture_carries_both_provenance_keys(server):
    """Without captured_at/source, check_fixture_provenance reports
    `unattributed` — the recorder must not produce a file its sibling gate
    rejects."""
    record = capture(server + "/venue", "GET", {}, None, "SCP")
    assert record["captured_at"]
    assert record["source"] == f"GET {server}/venue"
    assert record["system"] == "SCP"


def test_a_recorded_capture_satisfies_the_provenance_gate(tmp_path, server):
    """End-to-end between the two halves of #351."""
    sys.path.insert(0, str(REPO / "scripts"))
    from check_fixture_provenance import RECORDED, check

    src = tmp_path / "repo"
    (src / "testdata").mkdir(parents=True)
    record = capture(server + "/", "GET", {}, None, "SCP")
    (src / "testdata" / "scp.json").write_text(json.dumps(record))
    (src / "fixture.go").write_text(
        "// @cw-fixture SCP capture=testdata/scp.json\nfunc f() {}\n")

    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "contracts.json").write_text(json.dumps({"entities": [{
        "name": "B", "operations": [{"name": "c", "method": "GET", "path": "/x",
                                     "external": True, "external_system": "SCP"}]}]}))

    report = check([epic], src)
    assert [s.state for s in report.systems] == [RECORDED]
    assert report.outcome == "pass"


# --- what it records ----------------------------------------------------------

def test_response_headers_are_kept_in_full(server):
    """They are what the double must reproduce."""
    record = capture(server + "/", "GET", {}, None, "SCP")
    assert record["response"]["headers"]["X-Trace-Id"] == "abc123"


def test_a_json_body_is_parsed(server):
    record = capture(server + "/", "GET", {}, None, "SCP")
    assert record["response"]["body_kind"] == "json"
    assert record["response"]["body"] == {"opens": "9am"}


def test_a_non_json_body_is_kept_as_text(server):
    record = capture(server + "/text", "GET", {}, None, "SCP")
    assert record["response"]["body_kind"] == "text"
    assert record["response"]["body"] == "plain body"


def test_an_error_response_is_still_a_real_interaction(server):
    """The double has to reproduce error shapes too."""
    record = capture(server + "/boom", "GET", {}, None, "SCP")
    assert record["response"]["status"] == 503
    assert record["response"]["body"] == {"error": "unavailable"}


# --- header parsing -----------------------------------------------------------

def test_header_parsing():
    assert _parse_headers(["A: 1", "B:2"]) == {"A": "1", "B": "2"}


@pytest.mark.parametrize("bad", ["noconvon", ": novalue"])
def test_malformed_headers_are_rejected(bad):
    with pytest.raises(ValueError):
        _parse_headers([bad])


# --- CLI ----------------------------------------------------------------------

def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


def test_cli_writes_the_capture_and_prints_the_annotation(tmp_path, server):
    out = tmp_path / "captures" / "scp.json"
    result = _run("--url", server + "/", "--system", "SCP", "--out", str(out))
    assert result.returncode == 0
    assert out.is_file()
    assert "@cw-fixture SCP capture=" in result.stdout
    record = json.loads(out.read_text())
    assert record["captured_at"] and record["source"]


def test_cli_writes_nothing_when_the_request_fails(tmp_path):
    out = tmp_path / "capture.json"
    result = _run("--url", "http://127.0.0.1:1/nothing", "--system", "SCP",
                  "--out", str(out))
    assert result.returncode == 1
    assert not out.exists(), (
        "a capture that records a failure to connect is not a capture of the system")


def test_cli_malformed_header_is_a_usage_error(tmp_path, server):
    result = _run("--url", server + "/", "--system", "SCP",
                  "--out", str(tmp_path / "c.json"), "--header", "nocolon")
    assert result.returncode == 2


def test_cli_missing_body_file_is_a_usage_error(tmp_path, server):
    result = _run("--url", server + "/", "--system", "SCP",
                  "--out", str(tmp_path / "c.json"), "--body", "@/nope/nope.json")
    assert result.returncode == 2
