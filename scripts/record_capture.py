#!/usr/bin/env python3
"""Record ONE real interaction with an external system, as a fixture's source.

The helper chief-wiggum#351 asks for. A test double should be DERIVED from a
real captured interaction; this takes the capture and stamps it with the
provenance ``check_fixture_provenance.py`` requires -- when it was taken and
from what.

It is deliberately small. It records an HTTP round-trip, which is what most
external systems are; a WebSocket or streaming engine needs the target's own
harness, and the capture format below is what such a harness should write.

Usage::

    python3 scripts/record_capture.py \\
        --url https://scp.example.com/venue-info \\
        --system SCP \\
        --out testdata/captures/scp-venue-info.json \\
        [--method GET] [--header 'Authorization: Bearer …'] [--body @payload.json]

Then point the double at it::

    // @cw-fixture SCP capture=testdata/captures/scp-venue-info.json

**Secrets never enter the capture.** Request headers are recorded by NAME only,
with values dropped -- an `Authorization` header written into a checked-in
fixture is a credential leak, and captures are meant to be committed. Response
headers are kept in full: they are what the double must reproduce.

Exit codes: 0 recorded, 1 the request failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK = 0
EXIT_REQUEST_FAILED = 1
EXIT_USAGE = 2

# Recorded by name only. A capture is a committed artifact.
SENSITIVE_HINTS = ("authorization", "cookie", "token", "key", "secret", "psk")


def _redact_request_headers(headers: dict[str, str]) -> dict[str, str]:
    """Every request header value is dropped, not just the obviously secret
    ones: a capture is committed, and guessing which custom header carries a
    credential is exactly the guess that leaks one."""
    return {name: "<not recorded>" for name in headers}


def capture(url: str, method: str, headers: dict[str, str],
            body: bytes | None, system: str, timeout: float = 30.0) -> dict:
    request = urllib.request.Request(url, method=method, data=body,
                                     headers={"User-Agent": "chief-wiggum-recorder",
                                              **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = response.status
            resp_headers = dict(response.headers.items())
            payload = response.read()
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx IS a real interaction and worth recording — the double has
        # to reproduce error shapes too.
        status = exc.code
        resp_headers = dict(exc.headers.items()) if exc.headers else {}
        payload = exc.read() if hasattr(exc, "read") else b""

    try:
        parsed = json.loads(payload.decode("utf-8"))
        body_field: object = parsed
        body_kind = "json"
    except (UnicodeDecodeError, json.JSONDecodeError):
        body_field = payload.decode("utf-8", "replace")
        body_kind = "text"

    return {
        # The two keys check_fixture_provenance.py requires. Without them a
        # hand-written file satisfies that gate exactly as well as this does.
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": f"{method} {url}",
        "system": system,
        "request": {
            "method": method,
            "url": url,
            "headers": _redact_request_headers(headers),
            "body_present": body is not None,
        },
        "response": {
            "status": status,
            "headers": resp_headers,
            "body_kind": body_kind,
            "body": body_field,
        },
    }


def _parse_headers(raw: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw or []:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"--header needs 'Name: value', got {item!r}")
        out[name.strip()] = value.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record one real interaction as a fixture's source (chief-wiggum#351)")
    parser.add_argument("--url", required=True)
    parser.add_argument("--system", required=True,
                        help="The external_system name this capture belongs to")
    parser.add_argument("--out", required=True, help="Where to write the capture")
    parser.add_argument("--method", default="GET")
    parser.add_argument("--header", action="append", dest="headers",
                        help="'Name: value' (repeatable). Values are NOT recorded.")
    parser.add_argument("--body", help="Request body, or @path to read from a file")
    args = parser.parse_args(argv)

    try:
        headers = _parse_headers(args.headers)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE

    body: bytes | None = None
    if args.body:
        if args.body.startswith("@"):
            path = Path(args.body[1:])
            if not path.is_file():
                print(f"ERROR: {path} does not exist", file=sys.stderr)
                return EXIT_USAGE
            body = path.read_bytes()
        else:
            body = args.body.encode("utf-8")

    try:
        record = capture(args.url, args.method.upper(), headers, body, args.system)
    except Exception as exc:  # noqa: BLE001 - any transport failure is the same outcome
        print(f"ERROR: the request failed ({type(exc).__name__}: {exc}). "
              f"Nothing was written — a capture that records a failure to connect "
              f"is not a capture of the system.", file=sys.stderr)
        return EXIT_REQUEST_FAILED

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

    print(f"recorded {args.method.upper()} {args.url} -> "
          f"{record['response']['status']}  {out}")
    print(f"  point the double at it:  // @cw-fixture {args.system} capture={args.out}")
    if any(hint in name.lower() for name in headers for hint in SENSITIVE_HINTS):
        print("  (request header values were not recorded — captures are committed)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
