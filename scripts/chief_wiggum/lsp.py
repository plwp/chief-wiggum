"""Minimal LSP client for semantic code intelligence (#35).

Speaks LSP over JSON-RPC/stdio to a language server (``gopls`` for Go first) so a
workflow can query ground-truth semantic facts — go-to-definition, references,
hover types, and live diagnostics — instead of only shelling out to a linter at
gate time.

The wire framing is pure and unit-tested independently of any subprocess; the
client lifecycle drives a real server. The server is configured by a small
:class:`LspServer` record so other servers (pyright, rust-analyzer) can slot in
behind the same API later — gopls is just the first.

Positions are LSP UTF-16 code units (0-based line/character), per the spec.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

CONTENT_LENGTH = "content-length"


class LspError(RuntimeError):
    """Raised on protocol/transport errors."""


# --- pure wire framing ------------------------------------------------------


def encode_message(obj: dict) -> bytes:
    """Encode a JSON-RPC object with an LSP ``Content-Length`` header."""
    body = json.dumps(obj).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def _content_length(header_block: str) -> int | None:
    for line in header_block.split("\r\n"):
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip().lower() == CONTENT_LENGTH:
            try:
                n = int(value.strip())
            except ValueError:
                return None
            return n if n >= 0 else None
    return None


class MessageBuffer:
    """Accumulates raw bytes and yields complete JSON-RPC messages.

    Handles partial reads (a message split across chunks), multiple messages in
    one chunk, and preserves an incomplete trailing message for the next push.
    """

    def __init__(self) -> None:
        self._buf = b""

    def push(self, chunk: bytes) -> list[dict]:
        self._buf += chunk
        messages: list[dict] = []
        while True:
            sep = self._buf.find(b"\r\n\r\n")
            if sep == -1:
                break
            header = self._buf[:sep].decode("ascii", errors="replace")
            length = _content_length(header)
            if length is None:
                raise LspError(f"missing/invalid Content-Length in header: {header!r}")
            start = sep + 4
            if len(self._buf) - start < length:
                break  # payload not fully arrived yet
            payload = self._buf[start: start + length]
            self._buf = self._buf[start + length:]
            messages.append(json.loads(payload.decode("utf-8")))
        return messages


# --- server config ----------------------------------------------------------


@dataclass(frozen=True)
class LspServer:
    name: str
    command: tuple[str, ...]
    language_id: str
    extensions: tuple[str, ...]


GOPLS = LspServer(name="gopls", command=("gopls",), language_id="go", extensions=(".go",))
# pyright (pyright-langserver --stdio) — Python LSP, installable via npm.
PYRIGHT = LspServer(
    name="pyright",
    command=("pyright-langserver", "--stdio"),
    language_id="python",
    extensions=(".py", ".pyi"),
)
SERVERS = {GOPLS.name: GOPLS, PYRIGHT.name: PYRIGHT}


def server_for_file(path: str | Path) -> LspServer | None:
    suffix = Path(path).suffix
    for server in SERVERS.values():
        if suffix in server.extensions:
            return server
    return None


def server_available(server: LspServer) -> bool:
    return shutil.which(server.command[0]) is not None


def path_to_uri(path: str | Path) -> str:
    return Path(path).resolve().as_uri()


# --- client -----------------------------------------------------------------

Spawner = Callable[..., subprocess.Popen]


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: object = None


class LspClient:
    """Drives a language server over stdio. Use as a context manager."""

    def __init__(
        self,
        server: LspServer,
        root_path: str | Path,
        *,
        spawner: Spawner = subprocess.Popen,
        timeout: float = 30.0,
    ) -> None:
        self.server = server
        self.root_path = Path(root_path).resolve()
        self._spawner = spawner
        self._timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._buf = MessageBuffer()
        self._reader: threading.Thread | None = None
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._diagnostics: dict[str, list[dict]] = {}
        self._diag_seen: set[str] = set()
        self._diag_seq = 0  # bumped on every publishDiagnostics (settle tracking)
        self._closed = False  # set when the transport dies (reader exits)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

    # -- transport --
    def start(self) -> None:
        self._proc = self._spawner(
            list(self.server.command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.root_path),
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # Drain stderr so a chatty server (gopls logs there) can't block on a full pipe.
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        stdout = self._proc.stdout
        # read1() returns as soon as *some* data is available (read() would block
        # until the full count or EOF, deadlocking on a single small response).
        reader = getattr(stdout, "read1", None) or stdout.read
        try:
            while True:
                chunk = reader(65536)
                if not chunk:
                    break
                for msg in self._buf.push(chunk):
                    self._dispatch(msg)
        except (LspError, ValueError, OSError):
            pass
        finally:
            # EOF or transport error: wake every waiter rather than stranding it
            # until its own timeout.
            self._fail_all_pending("server closed the connection")

    def _fail_all_pending(self, reason: str) -> None:
        with self._cond:
            self._closed = True
            for pending in self._pending.values():
                if not pending.event.is_set():
                    pending.error = {"message": reason}
                    pending.event.set()

    def _drain_stderr(self) -> None:
        if not self._proc or not self._proc.stderr:
            return
        for _ in iter(lambda: self._proc.stderr.read1(65536) if hasattr(self._proc.stderr, "read1") else self._proc.stderr.read(65536), b""):
            pass

    def _dispatch(self, msg: dict) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            with self._cond:
                pending = self._pending.get(msg["id"])
                if pending:
                    pending.result = msg.get("result")
                    pending.error = msg.get("error")
                    pending.event.set()
        elif msg.get("method") == "textDocument/publishDiagnostics":
            params = msg.get("params", {})
            uri = params.get("uri")
            if uri is not None:
                with self._cond:
                    self._diagnostics[uri] = params.get("diagnostics", [])
                    self._diag_seen.add(uri)
                    self._diag_seq += 1
                    self._cond.notify_all()

    def _send(self, obj: dict) -> None:
        if not self._proc or not self._proc.stdin:
            raise LspError("server not started")
        self._proc.stdin.write(encode_message(obj))
        self._proc.stdin.flush()

    def _request(self, method: str, params: dict, *, timeout: float | None = None) -> object:
        with self._cond:
            if self._closed:
                raise LspError(f"{method} failed: server closed the connection")
            self._next_id += 1
            req_id = self._next_id
            pending = _Pending()
            self._pending[req_id] = pending
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        try:
            if not pending.event.wait(timeout or self._timeout):
                raise LspError(f"timeout waiting for {method}")
        finally:
            with self._cond:
                self._pending.pop(req_id, None)
        if pending.error:
            raise LspError(f"{method} failed: {pending.error}")
        return pending.result

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # -- lifecycle --
    def initialize(self) -> dict:
        root_uri = self.root_path.as_uri()
        result = self._request("initialize", {
            "processId": None,
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": self.root_path.name}],
            "capabilities": {
                "workspace": {"workspaceFolders": True},
                "textDocument": {
                    "definition": {},
                    "references": {},
                    "hover": {"contentFormat": ["plaintext", "markdown"]},
                    "publishDiagnostics": {"relatedInformation": True},
                },
            },
            "initializationOptions": {},
        })
        self._notify("initialized", {})
        return result  # type: ignore[return-value]

    def did_open(self, path: str | Path, text: str | None = None) -> None:
        p = Path(path)
        if text is None:
            text = p.read_text()
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": path_to_uri(p),
                "languageId": self.server.language_id,
                "version": 1,
                "text": text,
            }
        })

    # -- queries --
    def _loc_request(self, method: str, path, line, col, extra=None) -> list[dict]:
        params = {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": col},
        }
        if extra:
            params.update(extra)
        result = self._request(method, params)
        if result is None:
            return []
        items = result if isinstance(result, list) else [result]
        return [_location_to_dict(i) for i in items if i]

    def definition(self, path, line, col) -> list[dict]:
        return self._loc_request("textDocument/definition", path, line, col)

    def references(self, path, line, col, *, include_declaration: bool = True) -> list[dict]:
        return self._loc_request(
            "textDocument/references", path, line, col,
            extra={"context": {"includeDeclaration": include_declaration}},
        )

    def hover(self, path, line, col) -> dict:
        result = self._request("textDocument/hover", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": col},
        })
        if not result:
            return {"signature": None}
        contents = result.get("contents") if isinstance(result, dict) else None
        return {"signature": _hover_text(contents)}

    def diagnostics(self, path, *, timeout: float | None = None, settle: float = 0.6) -> list[dict]:
        """Return the latest diagnostics published for ``path``.

        Diagnostics arrive asynchronously after didOpen, and a server may publish
        an initial empty/stale set before analysis completes. So this waits for
        the first publish, then for the stream to *settle* (no new publish for
        ``settle`` seconds) before returning the latest — bounded by the deadline.
        """
        uri = path_to_uri(path)
        end = time.monotonic() + (timeout or self._timeout)
        with self._cond:
            self._cond.wait_for(lambda: uri in self._diag_seen, timeout=max(0.0, end - time.monotonic()))
            while True:
                seq = self._diag_seq
                remaining = min(settle, end - time.monotonic())
                if remaining <= 0:
                    break
                # Wait for a *newer* publish; if none arrives within the settle
                # window, the diagnostics have settled.
                if not self._cond.wait_for(lambda s=seq: self._diag_seq > s, timeout=remaining):
                    break
            raw = list(self._diagnostics.get(uri, []))
        return [_diagnostic_to_dict(d) for d in raw]

    def shutdown(self) -> None:
        try:
            if self._proc and self._proc.poll() is None:
                self._request("shutdown", {}, timeout=5)
                self._notify("exit", {})
        except LspError:
            pass
        finally:
            self._fail_all_pending("client shutting down")
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                        try:
                            self._proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            pass

    def __enter__(self) -> LspClient:
        self.start()
        try:
            self.initialize()
        except BaseException:
            # Never leak the server process if the handshake fails.
            self.shutdown()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()


# --- result normalization ---------------------------------------------------


def _location_to_dict(loc: dict) -> dict:
    # LSP Location or LocationLink.
    uri = loc.get("uri") or loc.get("targetUri")
    rng = loc.get("range") or loc.get("targetSelectionRange") or loc.get("targetRange") or {}
    start = rng.get("start", {})
    path = _uri_to_path(uri) if uri else None
    return {"file": path, "line": start.get("line"), "col": start.get("character"), "uri": uri}


def _diagnostic_to_dict(d: dict) -> dict:
    severities = {1: "error", 2: "warning", 3: "information", 4: "hint"}
    start = d.get("range", {}).get("start", {})
    return {
        "line": start.get("line"),
        "col": start.get("character"),
        "severity": severities.get(d.get("severity"), "unknown"),
        "message": d.get("message", ""),
        "source": d.get("source"),
    }


def _hover_text(contents) -> str | None:
    if contents is None:
        return None
    if isinstance(contents, str):
        return contents.strip() or None
    if isinstance(contents, dict):
        return (contents.get("value") or "").strip() or None
    if isinstance(contents, list):
        parts = [_hover_text(c) for c in contents]
        joined = "\n".join(p for p in parts if p)
        return joined or None
    return None


def _uri_to_path(uri: str) -> str:
    if uri.startswith("file://"):
        from urllib.parse import unquote, urlparse

        return unquote(urlparse(uri).path)
    return uri
