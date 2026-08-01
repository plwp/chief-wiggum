"""Deterministic local HTTP fixture server for saas_gate gate-validation trials.

`saas_gate.py`'s runtime checks probe a *live* URL — a non-deterministic
dependency (TLS config, load-balancer headers, WAF behaviour vary per
deployment). CTR-fh-044 requires the gate-validation record to pin a
**fixture/recorded target**, not a live URL, so `clean_corpus_runs` are
reproducible. This module is that fixture target: a stdlib-only
`http.server.HTTPServer` bound to an ephemeral `127.0.0.1` port that serves
DETERMINISTIC, per-scenario, scripted responses for `/`, `/health`, and
`/login`.

Each gate-validation trial starts this server on a scripted scenario, then runs
the REAL `scripts/saas_gate.py` CLI as a subprocess against
`http://127.0.0.1:<port>` — exercising the gate's full runtime path (real
`urllib.request`, real HTTP, real header parsing), not just its pure functions.

No new dependencies: `http.server` + `threading` only, mirroring the threaded
`HTTPServer` fixtures already in `tests/test_saas_gate.py`.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

# Each scenario scripts the three paths saas_gate probes:
#   root_headers  — (name, value) pairs sent on GET "/" (order preserved; a name
#                   may repeat, e.g. multiple Set-Cookie)
#   health_status — status code returned on GET "/health"
#   login_mode    — "429_after_3" (rate-limited: 200 x3 then 429 + Retry-After)
#                   or "always_200" (no limiter)
#
# A "healthy" security-header set (recognized by check_security_headers with no
# FAIL over HTTP): CSP present with a non-wildcard frame-ancestors, XCTO=nosniff,
# X-Frame-Options=DENY, Referrer-Policy present, and a session cookie with
# SameSite=Lax + HttpOnly (check_csrf PASS).
_HEALTHY_HEADERS = [
    ("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Set-Cookie", "session=abc123; HttpOnly; SameSite=Lax"),
]

SCENARIOS: dict[str, dict] = {
    # Clean corpus: every runtime channel healthy.
    "clean": {
        "root_headers": list(_HEALTHY_HEADERS),
        "health_status": 200,
        "login_mode": "429_after_3",
    },
    # direct: the textbook "missing security header" claim — CSP and
    # X-Content-Type-Options omitted entirely -> two FAIL findings -> fire.
    "missing_headers": {
        "root_headers": [
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Set-Cookie", "session=abc123; HttpOnly; SameSite=Lax"),
        ],
        "health_status": 200,
        "login_mode": "429_after_3",
    },
    # evasion-omission: the CSRF defect (SameSite is not Lax/Strict) is dressed
    # up with stray whitespace `SameSite = None` a naive substring check might
    # miss — the structural _cookie_attrs parser must still FAIL it -> fire.
    "csrf_samesite_none_spaced": {
        "root_headers": [
            ("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Set-Cookie", "session=abc123; HttpOnly; SameSite = None"),
        ],
        "health_status": 200,
        "login_mode": "429_after_3",
    },
    # evasion-config-indirection: an OTHERWISE-healthy response whose security
    # header NAMES are served lower-cased (HTTP header names are
    # case-insensitive). The gate's case-insensitive _hget must still recognize
    # them as present+healthy -> no FAIL -> no-fire (casing indirection does not
    # evade detection nor cause a spurious finding).
    "headers_lowercased": {
        "root_headers": [
            ("content-security-policy", "default-src 'self'; frame-ancestors 'none'"),
            ("x-content-type-options", "nosniff"),
            ("x-frame-options", "DENY"),
            ("referrer-policy", "no-referrer"),
            ("set-cookie", "session=abc123; HttpOnly; SameSite=Lax"),
        ],
        "health_status": 200,
        "login_mode": "429_after_3",
    },
    # evasion-sampling-gap: /login never returns 429. Absent
    # --rate-limit-required, check_rate_limit marks this WARN, not FAIL, by
    # design — a certified non-finding, not a scanner miss -> no-fire.
    "no_rate_limit": {
        "root_headers": list(_HEALTHY_HEADERS),
        "health_status": 200,
        "login_mode": "always_200",
    },
}


def _make_handler(scenario: str) -> type[BaseHTTPRequestHandler]:
    scn = SCENARIOS[scenario]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence the default stderr access log
            pass

        def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
            if self.path == "/health":
                self.send_response(scn["health_status"])
                self.end_headers()
                return
            if self.path == "/login":
                if scn["login_mode"] == "429_after_3":
                    self.server.login_hits += 1  # type: ignore[attr-defined]
                    if self.server.login_hits > 3:  # type: ignore[attr-defined]
                        self.send_response(429)
                        self.send_header("Retry-After", "5")
                        self.end_headers()
                        return
                self.send_response(200)
                self.end_headers()
                return
            # root "/": serve the scenario's security headers
            self.send_response(200)
            for name, value in scn["root_headers"]:
                self.send_header(name, value)
            self.end_headers()

    return Handler


@contextlib.contextmanager
def fixture_server(scenario: str) -> Iterator[str]:
    """Run the scripted scenario on an ephemeral 127.0.0.1 port.

    Yields the base URL (``http://127.0.0.1:<port>``) for the duration of the
    context, then shuts the server down.
    """
    if scenario not in SCENARIOS:
        raise KeyError(f"unknown scenario {scenario!r}; known: {sorted(SCENARIOS)}")
    server = HTTPServer(("127.0.0.1", 0), _make_handler(scenario))
    server.login_hits = 0  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
