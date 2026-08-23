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
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.hashing import scanner_version  # noqa: E402

PASS, FAIL, WARN, SKIPPED, NA = "pass", "fail", "warn", "skipped", "not_applicable"
# #289: distinct from SKIPPED. SKIPPED means "nothing was asked for" (no
# --base-url at all — honest absence, and can never fail the gate by design).
# ERROR means an input the operator explicitly pointed the gate AT (a named
# --base-url) could not be reached — a broken measurement, not silence, and
# it MUST be able to fail --gate.
ERROR = "error"

# The named checks check_security_headers()/check_csrf() normally emit when
# the header probe succeeds — used to report EACH of them as ERROR (rather
# than one opaque aggregate) when the probe itself cannot run at all (#289),
# so the measured denominator stays visible per-check instead of collapsing
# N real checks into a single opaque skip.
HEADER_CSRF_CHECK_NAMES = (
    "x-content-type-options", "content-security-policy", "clickjacking",
    "referrer-policy", "hsts", "csrf",
)

# http_get(url) -> (status_code, headers_lower_dict, body_text)
HttpGet = Callable[[str], tuple[int, dict, str]]
# The PSK probe must send a header, which the plain HttpGet contract has no
# room for. Kept as a separate alias rather than widening HttpGet, so every
# existing check and test fake keeps its current signature.
HttpGetAuth = Callable[..., tuple[int, dict, str]]


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
        # #289: ERROR (a named input the gate could not reach) must be able to
        # fail the gate exactly like FAIL — SKIPPED (nothing was asked for)
        # never does.
        return not any(f.status in (FAIL, ERROR) for f in self.findings)

    def by_category(self) -> dict[str, list[Finding]]:
        out: dict[str, list[Finding]] = {}
        for f in self.findings:
            out.setdefault(f.category, []).append(f)
        return out

    def to_dict(self) -> dict:
        counts = {
            s: sum(1 for f in self.findings if f.status == s)
            for s in (PASS, FAIL, WARN, SKIPPED, NA, ERROR)
        }
        return {
            "base_url": self.base_url,
            "stack": self.stack,
            "ok": self.ok,
            "counts": counts,
            # #289: the measured denominator, printed on every run (green or
            # not) so a collapsed/absent probe is visible without reading
            # every finding by hand.
            "measured": {
                "total_checks": len(self.findings),
                "measured_checks": counts[PASS] + counts[FAIL] + counts[WARN],
                "skipped_checks": counts[SKIPPED],
                "error_checks": counts[ERROR],
                "not_applicable_checks": counts[NA],
            },
            "findings": [f.to_dict() for f in self.findings],
        }

    def render_markdown(self) -> str:
        marks = {PASS: "✓", FAIL: "✗", WARN: "⚠", SKIPPED: "–", NA: "·", ERROR: "‼"}
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
    xfo_ok = (xfo or "").strip().lower() in ("deny", "sameorigin")
    # frame-ancestors must be present and not the wildcard *.
    fa_ok = False
    if csp:
        for directive in csp.split(";"):
            directive = directive.strip()
            if directive.lower().startswith("frame-ancestors"):
                value = directive[len("frame-ancestors"):].strip()
                fa_ok = bool(value) and "*" not in value
    findings.append(Finding(
        "security", "clickjacking",
        PASS if (xfo_ok or fa_ok) else FAIL,
        ("X-Frame-Options=" + xfo if xfo_ok else "")
        or ("CSP frame-ancestors" if fa_ok else (f"unsafe/absent (X-Frame-Options={xfo!r})")),
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


def _cookie_attrs(cookie: str) -> dict[str, str | bool]:
    """Parse a Set-Cookie value's attributes (everything after ``name=value``)."""
    attrs: dict[str, str | bool] = {}
    for part in cookie.split(";")[1:]:
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, _, value = part.partition("=")
            attrs[key.strip().lower()] = value.strip()
        else:
            attrs[part.lower()] = True
    return attrs


def check_csrf(set_cookie_headers: list[str], *, auth_mode: str = "cookie", https: bool = False) -> Finding:
    """CSRF posture from Set-Cookie (cookie auth) or N/A for bearer/API auth.

    Attributes are parsed, not substring-matched: ``SameSite=None`` does NOT
    mitigate CSRF (only ``Lax``/``Strict`` do), so it is treated as a failure.
    """
    if auth_mode == "bearer":
        return Finding("security", "csrf", NA, "bearer/API auth — CSRF tokens not applicable")
    if not set_cookie_headers:
        return Finding("security", "csrf", WARN, "no session cookie observed; cannot assess SameSite")
    fails: list[str] = []
    warns: list[str] = []
    for cookie in set_cookie_headers:
        attrs = _cookie_attrs(cookie)
        samesite = str(attrs.get("samesite", "")).lower()
        if samesite not in ("lax", "strict"):
            fails.append(f"SameSite={samesite or 'missing'} (need Lax/Strict)")
        if "httponly" not in attrs:
            warns.append("missing HttpOnly")
        if https and "secure" not in attrs:
            fails.append("missing Secure on HTTPS")
    if fails:
        return Finding("security", "csrf", FAIL, "; ".join(sorted(set(fails + warns))))
    if warns:
        return Finding("security", "csrf", WARN, "; ".join(sorted(set(warns))))
    return Finding("security", "csrf", PASS, "session cookie(s) have SameSite=Lax/Strict + HttpOnly")


def check_structured_logging(log_lines: list[str]) -> Finding:
    """Scan ALL non-blank log lines; FAIL (non-JSON) takes precedence over WARN."""
    non_blank = [ln.strip() for ln in log_lines if ln.strip()]
    if not non_blank:
        return Finding("observability", "structured-logging", SKIPPED, "no (non-blank) log sample provided")
    fail_detail: str | None = None
    warned = False
    parsed = 0
    for line in non_blank:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            fail_detail = fail_detail or f"non-JSON log line: {line[:60]!r}"
            continue
        if not isinstance(obj, dict) or not ({"level", "severity"} & set(obj)):
            warned = True
            continue
        parsed += 1
    if fail_detail:
        return Finding("observability", "structured-logging", FAIL, fail_detail)
    if warned:
        return Finding("observability", "structured-logging", WARN, "some JSON logs lack a level/severity field")
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


def check_secret_hygiene(secrets: dict[str, bytes] | None) -> list[Finding]:
    """Flag provisioned secrets whose bytes carry trailing whitespace (#370).

    A secret created with `echo` gains a trailing 0x0a, and the runtime
    injects the bytes verbatim. That one byte is enough to make a service
    structurally unreachable rather than merely misconfigured:

    - an HTTP header value can never end in a newline, so a PSK compared
      untrimmed rejects EVERY possible caller;
    - an HMAC computed over the wrong bytes never verifies;
    - Go's net/http hard-rejects newlines in outbound header values, so a
      Bearer token with one cannot even be sent;
    - and the quiet one: a mode flag read as `live\\n` fails an equality test
      against "live" and silently falls back to test billing.

    The real incident ran a production endpoint that no caller could reach for
    a month, because staging's copy of the secret happened to lack the
    newline. Nothing failed loudly; the endpoint just answered 401 forever.

    `None` means nothing was supplied, which is SKIPPED — never a pass. An
    empty mapping means the source was consulted and held nothing, which is
    a different, reportable state.
    """
    if secrets is None:
        return [Finding("security", "secret-hygiene", SKIPPED,
                        "no secret material supplied; pass --secrets-file")]
    if not secrets:
        return [Finding("security", "secret-hygiene", WARN,
                        "secret source held no entries — nothing was checked")]

    findings = []
    for name in sorted(secrets):
        raw = secrets[name]
        trailing = len(raw) - len(raw.rstrip(b" \t\r\n"))
        if trailing:
            tail = raw[-1:].hex()
            findings.append(Finding(
                "security", f"secret-hygiene:{name}", FAIL,
                f"{trailing} trailing whitespace byte(s), last=0x{tail}. "
                f"Re-provision with printf '%s', never echo, and TrimSpace on "
                f"read — an untrimmed value can make an endpoint unreachable "
                f"by every caller."))
        else:
            findings.append(Finding("security", f"secret-hygiene:{name}", PASS,
                                    "no trailing whitespace"))
    return findings


def check_psk_endpoint_callable(
    http_get: HttpGetAuth, base_url: str, path: str, *,
    header: str = "X-Internal-Auth", secret: str | None = None,
) -> Finding:
    """An internal endpoint must reject without the secret AND work with it.

    Those are independent properties, and checking only the first is what let
    the real defect ship: every existing check reads "401 without a secret =
    pass", which an endpoint nobody can ever call satisfies perfectly.

    So the signature being hunted here is 401 in BOTH directions — the mark of
    a secret that does not match what the service holds, whether through a
    trailing newline, drift, or a rotation nobody propagated.
    """
    url = base_url.rstrip("/") + path
    if not secret:
        return Finding("security", f"psk-callable:{path}", SKIPPED,
                       "no secret supplied; cannot test the positive direction")
    try:
        without, _, _ = http_get(url)
        with_secret, _, _ = http_get(url, headers={header: secret})
    except TypeError:
        return Finding("security", f"psk-callable:{path}", ERROR,
                       "http_get does not accept headers; cannot probe")
    except Exception as exc:  # noqa: BLE001
        return Finding("security", f"psk-callable:{path}", ERROR,
                       f"{url} unreachable: {exc}")

    if without < 400:
        return Finding("security", f"psk-callable:{path}", FAIL,
                       f"reachable WITHOUT the secret ({without})")
    if with_secret >= 400:
        return Finding(
            "security", f"psk-callable:{path}", FAIL,
            f"rejects with AND without the secret ({without} then "
            f"{with_secret}) — the endpoint is unreachable by every caller. "
            f"Check the provisioned value for trailing whitespace.")
    return Finding("security", f"psk-callable:{path}", PASS,
                   f"{without} without the secret, {with_secret} with it")


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


def _headers_to_dict(message) -> dict:
    """Lower-case header dict, preserving ALL Set-Cookie values as a list.

    A plain ``dict`` comprehension collapses duplicate headers, dropping all but
    the last Set-Cookie — and the session cookie is frequently not the last one.
    ``email.message.Message.get_all`` keeps every value.
    """
    headers = {k.lower(): v for k, v in message.items()}
    if message is not None:
        headers["set-cookie"] = message.get_all("set-cookie") or []
    return headers


def default_http_get(url: str, *, timeout: float = 10.0,
                     headers: dict | None = None) -> tuple[int, dict, str]:
    """GET a URL. `headers` is keyword-only with a default, so every existing
    caller and test fake keeps working while the PSK probe can authenticate."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "chief-wiggum-saas-gate", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - gate probes a user-supplied URL
            return resp.status, _headers_to_dict(resp.headers), resp.read(8192).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, _headers_to_dict(exc.headers) if exc.headers else {}, ""


# --- orchestration ----------------------------------------------------------


def _extract_set_cookies(headers: dict) -> list[str]:
    """Normalise Set-Cookie from a headers dict into a list of cookie strings.

    ``default_http_get`` stores a list (all values preserved); simpler/injected
    getters may store a single string. Other key casings are tolerated.
    """
    for k, v in headers.items():
        if k.lower() == "set-cookie":
            if isinstance(v, (list, tuple)):
                return [str(c) for c in v]
            return [str(v)]
    return []


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
    secrets: dict[str, bytes] | None = None,
    psk_paths: list[str] | None = None,
    psk_header: str = "X-Internal-Auth",
    psk_secret: str | None = None,
) -> SaasGateReport:
    report = SaasGateReport(base_url=base_url, stack=detect_stack(repo))
    if base_url:
        https = base_url.startswith("https://") or require_https
        try:
            _, headers, _ = http_get(base_url)
            set_cookie = _extract_set_cookies(headers)
            for f in check_security_headers(headers, https=https):
                report.findings.append(f)
            report.findings.append(check_csrf(set_cookie, auth_mode=auth_mode, https=https))
        except Exception as exc:  # noqa: BLE001
            # #289: --base-url was EXPLICITLY given — this is a target the
            # operator pointed the gate at, not an absent input. Collapsing
            # the whole header/CSRF battery into one SKIPPED finding hid that
            # N real checks (not one) went unmeasured, and SKIPPED can never
            # fail --gate — indistinguishable from "nothing was asked for".
            # Report each named check as ERROR instead: visible per-check,
            # and able to block.
            for name in HEADER_CSRF_CHECK_NAMES:
                report.add(
                    "security", name, ERROR,
                    f"could not fetch {base_url}: {exc}",
                )
        report.findings.append(check_health(http_get, base_url, health_path))
        report.findings.append(check_rate_limit(http_get, base_url, rate_limit_path, required=rate_limit_required))
        for path in (psk_paths or []):
            report.findings.append(check_psk_endpoint_callable(
                http_get, base_url, path, header=psk_header, secret=psk_secret))
        if not psk_paths:
            report.add("security", "psk-callable", SKIPPED,
                       "no --psk-path given; internal endpoints not probed")
    else:
        report.add("security", "headers", SKIPPED, "no --base-url; runtime checks skipped")
    report.findings.extend(check_secret_hygiene(secrets))
    report.findings.append(check_structured_logging(log_sample or []))
    report.add("isolation", "tenant-isolation", SKIPPED, "needs a live multi-user app; run via the /saas-gate skill")
    report.add("performance", "response-time", SKIPPED, "needs a representative deployment; run via the /saas-gate skill")
    report.add("data-integrity", "audit-trail/soft-delete", SKIPPED, "code-level; verify in review / a follow-up check")
    return report


def _scanner_version() -> str:
    """Hash-derived ``--scanner-version``: the source of this module plus its
    ``chief_wiggum`` dependencies. No hand-bumped constant to forget
    (INV-fh-005).
    @cw-trace guards CTR-fh-040 CTR-fh-041 CTR-fh-042 INV-fh-005"""
    here = Path(__file__).resolve()
    cw_dir = here.parent / "chief_wiggum"
    return scanner_version(here, cw_dir / "hashing.py")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SaaS NFR validation gate")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base-url")
    parser.add_argument("--auth-mode", choices=["cookie", "bearer"], default="cookie")
    parser.add_argument("--health-path", default="/health")
    parser.add_argument("--rate-limit-path", default="/login")
    parser.add_argument("--rate-limit-required", action="store_true")
    parser.add_argument(
        "--secrets-file",
        help="JSON object of provisioned secrets, name -> value, checked for "
             "trailing whitespace (chief-wiggum#370). Produce it with e.g. "
             "`gcloud secrets versions access latest --secret=NAME`; the "
             "values are never printed, only their trailing bytes.")
    parser.add_argument(
        "--psk-path", action="append", dest="psk_paths",
        help="Internal endpoint that must reject WITHOUT the secret and "
             "succeed WITH it. Repeatable.")
    parser.add_argument("--psk-header", default="X-Internal-Auth")
    parser.add_argument(
        "--psk-secret-env",
        help="Environment variable holding the PSK for --psk-path probes. "
             "Read at call time; never logged.")
    parser.add_argument("--require-https", action="store_true")
    parser.add_argument("--log-sample", help="File with sample log lines for structured-logging check")
    parser.add_argument("--gate", action="store_true", help="Exit 1 if any check failed")
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true")
    out.add_argument("--markdown", action="store_true")
    parser.add_argument(
        "--scanner-version",
        action="store_true",
        help="Print the hash-derived scanner version (source hash of this module + its "
        "chief_wiggum deps) and exit",
    )
    args = parser.parse_args(argv)

    if args.scanner_version:
        print(_scanner_version())
        return 0

    # #289: --repo was never validated — detect_stack() on a missing/wrong
    # path just returns [] (a silent no-op), indistinguishable from "this
    # repo genuinely has no go.mod/package.json/pyproject.toml". A named path
    # that isn't a real directory is a usage error, not honest absence.
    repo_path = Path(args.repo)
    if not repo_path.is_dir():
        print(f"Error: --repo {args.repo}: not a directory", file=sys.stderr)
        return 2

    log_sample = None
    if args.log_sample and Path(args.log_sample).exists():
        log_sample = Path(args.log_sample).read_text().splitlines()

    secrets = None
    if args.secrets_file:
        # A named file that cannot be read is a usage error, not honest
        # absence — degrading to None would report SKIPPED and read exactly
        # like "nobody asked for this check".
        try:
            raw = json.loads(Path(args.secrets_file).read_text())
        except (OSError, ValueError) as exc:
            print(f"Error: --secrets-file {args.secrets_file}: {exc}",
                  file=sys.stderr)
            return 2
        if not isinstance(raw, dict):
            print(f"Error: --secrets-file {args.secrets_file}: expected a JSON "
                  f"object of name -> value", file=sys.stderr)
            return 2
        secrets = {str(k): (v if isinstance(v, bytes) else str(v).encode())
                   for k, v in raw.items()}

    psk_secret = os.environ.get(args.psk_secret_env) if args.psk_secret_env else None
    if args.psk_secret_env and not psk_secret:
        print(f"Error: --psk-secret-env {args.psk_secret_env} is unset or empty",
              file=sys.stderr)
        return 2

    report = run_gate(
        args.repo, args.base_url, auth_mode=args.auth_mode, health_path=args.health_path,
        rate_limit_path=args.rate_limit_path, rate_limit_required=args.rate_limit_required,
        require_https=args.require_https, log_sample=log_sample,
        secrets=secrets, psk_paths=args.psk_paths,
        psk_header=args.psk_header, psk_secret=psk_secret,
    )

    if args.markdown:
        print(report.render_markdown())
    else:
        print(json.dumps(report.to_dict(), indent=2))
    return 1 if (args.gate and not report.ok) else 0


if __name__ == "__main__":
    sys.exit(main())
