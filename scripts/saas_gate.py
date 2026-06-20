#!/usr/bin/env python3
"""SaaS non-functional-requirements gate (#2).

Validates common SaaS NFRs — security headers + CSRF posture, auth rate-limiting,
tenant isolation, performance, observability (health + structured logging) —
against a running app, and reports actionable pass/fail per category.

Results use five statuses so the gate never claims more than it proved:
``pass`` / ``fail`` / ``warn`` / ``skipped`` / ``not_applicable``. ``/close-epic``
fails only on a real ``fail`` (real evidence or an explicit contract). All
runtime I/O (the HTTP getter, user factory, resource fetcher, log sample) is
injectable, so the check logic is hermetically testable.

Run standalone or as a `/close-epic` gate:
    python3 scripts/saas_gate.py --repo . --base-url http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

PASS, FAIL, WARN, SKIPPED, NA = "pass", "fail", "warn", "skipped", "not_applicable"

# http_get(url) -> (status_code, headers_lower_dict, body_text)
HttpGet = Callable[[str], tuple[int, dict, str]]


@dataclass
class Finding:
    category: str
    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SaasGateReport:
    base_url: str | None = None
    stack: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def add(self, *args, **kwargs) -> None:
        self.findings.append(Finding(*args, **kwargs))

    @property
    def ok(self) -> bool:
        return not any(f.status == FAIL for f in self.findings)

    def by_category(self) -> dict[str, list[Finding]]:
        out: dict[str, list[Finding]] = {}
        for f in self.findings:
            out.setdefault(f.category, []).append(f)
        return out

    def to_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "stack": self.stack,
            "ok": self.ok,
            "counts": {s: sum(1 for f in self.findings if f.status == s) for s in (PASS, FAIL, WARN, SKIPPED, NA)},
            "findings": [f.to_dict() for f in self.findings],
        }

    def render_markdown(self) -> str:
        marks = {PASS: "✓", FAIL: "✗", WARN: "⚠", SKIPPED: "–", NA: "·"}
        lines = ["# SaaS NFR Gate", "", f"Status: {'PASS' if self.ok else 'FAIL'}", f"Stack: {', '.join(self.stack) or 'unknown'}", ""]
        for category, items in self.by_category().items():
            lines.append(f"## {category}")
            for f in items:
                lines.append(f"- {marks.get(f.status, '?')} **{f.name}** ({f.status}): {f.detail}")
            lines.append("")
        return "\n".join(lines) + "\n"


# --- stack detection --------------------------------------------------------


def detect_stack(repo: str | Path) -> list[str]:
    root = Path(repo)
    stack = []
    if (root / "go.mod").is_file():
        stack.append("go")
    if (root / "package.json").is_file():
        stack.append("node")
    if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file() or (root / "requirements.txt").is_file():
        stack.append("python")
    return stack


# --- pure checks ------------------------------------------------------------

REQUIRED_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": None,
}


def _hget(headers: dict, name: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def check_security_headers(headers: dict, *, https: bool = False) -> list[Finding]:
    """Classify response security headers (pure)."""
    findings: list[Finding] = []
    csp = _hget(headers, "content-security-policy")
    xcto = _hget(headers, "x-content-type-options")
    findings.append(Finding(
        "security", "x-content-type-options",
        PASS if (xcto or "").lower() == "nosniff" else FAIL,
        xcto or "missing (want nosniff)",
    ))
    findings.append(Finding(
        "security", "content-security-policy",
        PASS if csp else FAIL, csp or "missing",
    ))
    if csp and ("unsafe-inline" in csp or "default-src *" in csp.replace("'", "")):
        findings.append(Finding("security", "csp-strength", WARN, "weak CSP (unsafe-inline / default-src *)"))
    xfo = _hget(headers, "x-frame-options")
    frame_ancestors = bool(csp and "frame-ancestors" in csp)
    findings.append(Finding(
        "security", "clickjacking",
        PASS if (xfo or frame_ancestors) else FAIL,
        xfo or ("CSP frame-ancestors" if frame_ancestors else "no X-Frame-Options or CSP frame-ancestors"),
    ))
    findings.append(Finding(
        "security", "referrer-policy",
        PASS if _hget(headers, "referrer-policy") else FAIL,
        _hget(headers, "referrer-policy") or "missing",
    ))
    hsts = _hget(headers, "strict-transport-security")
    if https:
        findings.append(Finding("security", "hsts", PASS if hsts else FAIL, hsts or "missing on HTTPS"))
    elif not hsts:
        findings.append(Finding("security", "hsts", WARN, "no HSTS (base URL is not HTTPS)"))
    return findings


def check_csrf(set_cookie_headers: list[str], *, auth_mode: str = "cookie", https: bool = False) -> Finding:
    """CSRF posture from Set-Cookie (cookie auth) or N/A for bearer/API auth."""
    if auth_mode == "bearer":
        return Finding("security", "csrf", NA, "bearer/API auth — CSRF tokens not applicable")
    if not set_cookie_headers:
        return Finding("security", "csrf", WARN, "no session cookie observed; cannot assess SameSite")
    problems = []
    for cookie in set_cookie_headers:
        low = cookie.lower()
        if "samesite" not in low:
            problems.append("missing SameSite")
        if "httponly" not in low:
            problems.append("missing HttpOnly")
        if https and "secure" not in low:
            problems.append("missing Secure on HTTPS")
    if any("samesite" in p.lower() for p in problems):
        return Finding("security", "csrf", FAIL, "; ".join(sorted(set(problems))))
    if problems:
        return Finding("security", "csrf", WARN, "; ".join(sorted(set(problems))))
    return Finding("security", "csrf", PASS, "session cookie has SameSite + HttpOnly")


def check_structured_logging(log_lines: list[str]) -> Finding:
    if not log_lines:
        return Finding("observability", "structured-logging", SKIPPED, "no log sample provided")
    parsed = 0
    for line in log_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return Finding("observability", "structured-logging", FAIL, f"non-JSON log line: {line[:60]!r}")
        if not isinstance(obj, dict) or not ({"level", "severity"} & set(obj)):
            return Finding("observability", "structured-logging", WARN, "JSON logs lack a level/severity field")
        parsed += 1
    return Finding("observability", "structured-logging", PASS, f"{parsed} structured log line(s)")


# --- runtime checks (injectable HTTP) ---------------------------------------


def check_health(http_get: HttpGet, base_url: str, path: str = "/health") -> Finding:
    url = base_url.rstrip("/") + path
    try:
        status, _, _ = http_get(url)
    except Exception as exc:  # noqa: BLE001
        return Finding("observability", "health", FAIL, f"{url} unreachable: {exc}")
    return Finding("observability", "health", PASS if status == 200 else FAIL, f"GET {path} -> {status}")


def check_rate_limit(
    http_get: HttpGet, base_url: str, path: str = "/login",
    *, attempts: int = 20, required: bool = False,
) -> Finding:
    url = base_url.rstrip("/") + path
    saw_429 = False
    retry_after = False
    for _ in range(attempts):
        try:
            status, headers, _ = http_get(url)
        except Exception as exc:  # noqa: BLE001
            return Finding("security", "rate-limit", WARN, f"probe error: {exc}")
        if status == 429:
            saw_429 = True
            retry_after = _hget(headers, "retry-after") is not None
            break
    if saw_429:
        detail = "429 observed" + ("" if retry_after else " (no Retry-After header)")
        return Finding("security", "rate-limit", PASS if retry_after else WARN, detail)
    status_label = FAIL if required else WARN
    return Finding("security", "rate-limit", status_label, f"no 429 in {attempts} requests to {path}")


def check_tenant_isolation(
    make_user: Callable[[], object],
    create_resource: Callable[[object], object],
    fetch_resource: Callable[[object, object], int],
) -> Finding:
    """Two users; user B must not fetch user A's resource (injectable)."""
    try:
        user_a = make_user()
        user_b = make_user()
        resource = create_resource(user_a)
        status = fetch_resource(user_b, resource)
    except Exception as exc:  # noqa: BLE001
        return Finding("isolation", "tenant-isolation", SKIPPED, f"could not run isolation check: {exc}")
    if status in (401, 403, 404):
        return Finding("isolation", "tenant-isolation", PASS, f"cross-tenant fetch denied ({status})")
    return Finding("isolation", "tenant-isolation", FAIL, f"cross-tenant fetch allowed ({status}) — data leak")


# --- default HTTP getter ----------------------------------------------------


def default_http_get(url: str, *, timeout: float = 10.0) -> tuple[int, dict, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "chief-wiggum-saas-gate"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - gate probes a user-supplied URL
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(8192).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, ""


# --- orchestration ----------------------------------------------------------


def run_gate(
    repo: str | Path,
    base_url: str | None,
    *,
    http_get: HttpGet = default_http_get,
    auth_mode: str = "cookie",
    health_path: str = "/health",
    rate_limit_path: str = "/login",
    rate_limit_required: bool = False,
    require_https: bool = False,
    log_sample: list[str] | None = None,
) -> SaasGateReport:
    report = SaasGateReport(base_url=base_url, stack=detect_stack(repo))
    if base_url:
        https = base_url.startswith("https://") or require_https
        try:
            _, headers, _ = http_get(base_url)
            set_cookie = [v for k, v in headers.items() if k.lower() == "set-cookie"]
            for f in check_security_headers(headers, https=https):
                report.findings.append(f)
            report.findings.append(check_csrf(set_cookie, auth_mode=auth_mode, https=https))
        except Exception as exc:  # noqa: BLE001
            report.add("security", "headers", SKIPPED, f"could not fetch {base_url}: {exc}")
        report.findings.append(check_health(http_get, base_url, health_path))
        report.findings.append(check_rate_limit(http_get, base_url, rate_limit_path, required=rate_limit_required))
    else:
        report.add("security", "headers", SKIPPED, "no --base-url; runtime checks skipped")
    report.findings.append(check_structured_logging(log_sample or []))
    report.add("isolation", "tenant-isolation", SKIPPED, "needs a live multi-user app; run via the /saas-gate skill")
    report.add("performance", "response-time", SKIPPED, "needs a representative deployment; run via the /saas-gate skill")
    report.add("data-integrity", "audit-trail/soft-delete", SKIPPED, "code-level; verify in review / a follow-up check")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SaaS NFR validation gate")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base-url")
    parser.add_argument("--auth-mode", choices=["cookie", "bearer"], default="cookie")
    parser.add_argument("--health-path", default="/health")
    parser.add_argument("--rate-limit-path", default="/login")
    parser.add_argument("--rate-limit-required", action="store_true")
    parser.add_argument("--require-https", action="store_true")
    parser.add_argument("--log-sample", help="File with sample log lines for structured-logging check")
    parser.add_argument("--gate", action="store_true", help="Exit 1 if any check failed")
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true")
    out.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)

    log_sample = None
    if args.log_sample and Path(args.log_sample).exists():
        log_sample = Path(args.log_sample).read_text().splitlines()

    report = run_gate(
        args.repo, args.base_url, auth_mode=args.auth_mode, health_path=args.health_path,
        rate_limit_path=args.rate_limit_path, rate_limit_required=args.rate_limit_required,
        require_https=args.require_https, log_sample=log_sample,
    )

    if args.markdown:
        print(report.render_markdown())
    else:
        print(json.dumps(report.to_dict(), indent=2))
    return 1 if (args.gate and not report.ok) else 0


if __name__ == "__main__":
    sys.exit(main())
