"""Tests for the SaaS NFR gate (#2)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import saas_gate as sg

# --- stack detection --------------------------------------------------------


def test_detect_stack(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "package.json").write_text("{}")
    assert set(sg.detect_stack(tmp_path)) == {"go", "node"}


# --- security headers (pure) ------------------------------------------------


GOOD_HEADERS = {
    "content-security-policy": "default-src 'self'; frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


def _status(findings, name):
    return next(f.status for f in findings if f.name == name)


def test_security_headers_all_good():
    fs = sg.check_security_headers(GOOD_HEADERS)
    assert _status(fs, "x-content-type-options") == sg.PASS
    assert _status(fs, "content-security-policy") == sg.PASS
    assert _status(fs, "clickjacking") == sg.PASS
    assert _status(fs, "referrer-policy") == sg.PASS


def test_security_headers_missing_fail():
    fs = sg.check_security_headers({})
    assert _status(fs, "x-content-type-options") == sg.FAIL
    assert _status(fs, "content-security-policy") == sg.FAIL
    assert _status(fs, "clickjacking") == sg.FAIL


def test_weak_csp_warns():
    fs = sg.check_security_headers({**GOOD_HEADERS, "content-security-policy": "default-src 'self' 'unsafe-inline'"})
    assert any(f.name == "csp-strength" and f.status == sg.WARN for f in fs)


def test_clickjacking_via_csp_frame_ancestors():
    h = {**GOOD_HEADERS}
    del h["x-frame-options"]
    assert _status(sg.check_security_headers(h), "clickjacking") == sg.PASS  # frame-ancestors present


def test_hsts_warn_on_http_fail_on_https():
    assert _status(sg.check_security_headers({}, https=False), "hsts") == sg.WARN
    assert _status(sg.check_security_headers({}, https=True), "hsts") == sg.FAIL


# --- CSRF (pure) ------------------------------------------------------------


def test_csrf_cookie_good():
    f = sg.check_csrf(["sid=abc; HttpOnly; SameSite=Lax"], auth_mode="cookie")
    assert f.status == sg.PASS


def test_csrf_missing_samesite_fails():
    f = sg.check_csrf(["sid=abc; HttpOnly"], auth_mode="cookie")
    assert f.status == sg.FAIL


def test_csrf_bearer_not_applicable():
    assert sg.check_csrf([], auth_mode="bearer").status == sg.NA


def test_csrf_no_cookie_warns():
    assert sg.check_csrf([], auth_mode="cookie").status == sg.WARN


# --- structured logging (pure) ----------------------------------------------


def test_structured_logging_pass():
    assert sg.check_structured_logging(['{"level":"info","msg":"x"}']).status == sg.PASS


def test_structured_logging_non_json_fails():
    assert sg.check_structured_logging(["plain text log"]).status == sg.FAIL


def test_structured_logging_no_level_warns():
    assert sg.check_structured_logging(['{"msg":"x"}']).status == sg.WARN


def test_structured_logging_empty_skipped():
    assert sg.check_structured_logging([]).status == sg.SKIPPED


# --- runtime checks (injected getter) ---------------------------------------


def test_health_pass_and_fail():
    assert sg.check_health(lambda u: (200, {}, ""), "http://x").status == sg.PASS
    assert sg.check_health(lambda u: (503, {}, ""), "http://x").status == sg.FAIL


def test_health_unreachable_fails():
    def boom(u):
        raise OSError("refused")

    assert sg.check_health(boom, "http://x").status == sg.FAIL


def test_rate_limit_pass_on_429_with_retry_after():
    assert sg.check_rate_limit(lambda u: (429, {"retry-after": "5"}, ""), "http://x").status == sg.PASS


def test_rate_limit_warn_on_429_without_retry_after():
    assert sg.check_rate_limit(lambda u: (429, {}, ""), "http://x").status == sg.WARN


def test_rate_limit_warn_when_no_limiter():
    assert sg.check_rate_limit(lambda u: (200, {}, ""), "http://x", attempts=5).status == sg.WARN


def test_rate_limit_fail_when_required():
    assert sg.check_rate_limit(lambda u: (200, {}, ""), "http://x", attempts=5, required=True).status == sg.FAIL


def test_tenant_isolation_pass_and_fail():
    ok = sg.check_tenant_isolation(lambda: object(), lambda u: "r", lambda u, r: 403)
    assert ok.status == sg.PASS
    leak = sg.check_tenant_isolation(lambda: object(), lambda u: "r", lambda u, r: 200)
    assert leak.status == sg.FAIL


# --- report + CLI -----------------------------------------------------------


def test_report_ok_only_on_no_fail():
    r = sg.SaasGateReport()
    r.add("security", "x", sg.WARN)
    assert r.ok is True
    r.add("security", "y", sg.FAIL)
    assert r.ok is False


def test_run_gate_without_base_url_skips_runtime(tmp_path):
    r = sg.run_gate(tmp_path, None)
    json.loads(json.dumps(r.to_dict()))
    assert any(f.status == sg.SKIPPED for f in r.findings)


def test_cli_markdown(tmp_path, capsys):
    rc = sg.main(["--repo", str(tmp_path), "--markdown"])
    assert rc == 0
    assert "# SaaS NFR Gate" in capsys.readouterr().out


# --- hermetic integration against a real local HTTP server ------------------


class _Handler(BaseHTTPRequestHandler):
    hits = {"login": 0}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            return
        if self.path == "/login":
            _Handler.hits["login"] += 1
            if _Handler.hits["login"] > 3:
                self.send_response(429)
                self.send_header("Retry-After", "5")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            return
        # root: serve good security headers
        self.send_response(200)
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Set-Cookie", "sid=abc; HttpOnly; SameSite=Lax")
        self.end_headers()


@pytest.fixture()
def live_server():
    _Handler.hits["login"] = 0
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_integration_against_real_http_server(live_server):
    r = sg.run_gate(".", live_server, rate_limit_path="/login")
    statuses = {f.name: f.status for f in r.findings}
    assert statuses["x-content-type-options"] == sg.PASS
    assert statuses["clickjacking"] == sg.PASS
    assert statuses["csrf"] == sg.PASS
    assert statuses["health"] == sg.PASS
    assert statuses["rate-limit"] == sg.PASS  # 429 + Retry-After observed
    assert r.ok is True
