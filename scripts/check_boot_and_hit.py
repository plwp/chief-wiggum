#!/usr/bin/env python3
"""The assembled service must actually start and serve its declared routes.

chief-wiggum#352. ``cmd/server`` had **no test files** -- the mux wiring was
never exercised by anything. A duplicate ``mux.Handle("/")`` (the server
package had already registered it) panicked at startup, so the container
crash-looped on Cloud Run and it was found on deploy. The demo page and
``/widget.js`` were likewise never wired into the server until the deploy
branch.

Package tests were green. `go build ./...` was green. Neither composes the
binary and asks it for a route.

What this gate is
-----------------
Given a **running instance** (``--base-url``), it probes every route the epic's
``contracts.json`` declares this service SERVES -- every operation that is not
``"external": true`` (that direction is chief-wiggum#353's) -- and reports
which ones the composition actually answers.

Reaching a base URL at all is itself the startup-panic check: a binary that
crash-loops has no URL to give. The route sweep is the wiring check.

Following ``saas_gate.py``'s precedent, booting the service is the operator's
job (or the skill's) and probing is this gate's. Without ``--base-url`` the
result is ``inapplicable`` -- loudly not a pass, because nothing was composed
and nothing was asked.

Two safety rules that shape the whole design
--------------------------------------------
**It never fires a mutating request by default.** Probing ``DELETE
/orders/:id`` against a real staging service destroys data. GET and HEAD are
probed; POST/PUT/PATCH/DELETE report ``not_probed`` unless the operator
explicitly passes ``--probe-mutating``, having decided the target is
disposable. A gate that damages the system it inspects is worse than no gate.

**It does not guess path parameters.** ``/orders/:id`` cannot be probed
literally, and a 404 from a substituted id is ambiguous -- it means "no such
record" just as readily as "route not registered", which is the exact question
being asked. Parameterized routes report ``unprobeable`` unless the operator
supplies ``--path-param id=123``. Reporting a route as broken because this gate
invented an id would be a false positive on correct code, and one of those
teaches the operator to ``--force`` past everything (``docs/gate-rollout.md``).

Per-route states
----------------
  served        2xx/3xx, or a status the operation itself declares in
                error_cases -- the composition answers here
  served_gated  401/403: the route IS registered, an auth layer answered
                first. Registration is what this gate asks about
  not_served    404/405 -- declared, not wired. THE finding this exists for
  error_status  5xx -- registered and broken
  not_probed    a mutating method, not probed by default
  unprobeable   a parameterized path with no supplied substitution
  unreachable   the request itself failed -- the service is not up

The entrypoint rule
-------------------
#352 asks that a service entrypoint not be allowed zero tests. It asks for the
CONJUNCTION, and the conjunction is the right rule: "entrypoint has no test AND
no boot-and-hit coverage". An entrypoint with no unit tests whose routes were
just successfully probed IS exercised -- demanding a test file as well would be
cargo cult. The finding fires only when nothing tested it and nothing booted it.

Gate status: REPORT-ONLY per ``docs/gate-rollout.md``. ``--gate`` exits 1 on
findings; no workflow passes it until a passing
``docs/quality/validation/check_boot_and_hit.json`` record exists.

CLI::

    python3 scripts/check_boot_and_hit.py <epic-dir> --source <repo-root>
        [--base-url http://localhost:8080] [--path-param id=123 ...]
        [--probe-mutating] [--format text|json] [--gate]

Exit codes: 0 ok / report-only / inapplicable, 1 findings under --gate,
2 usage error, 3 an input was present and unreadable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_ERROR = 3

SAFE_METHODS = {"GET", "HEAD"}

# `/orders/:id`, `/orders/{id}`, `/orders/<id>` — the three shapes CW's
# contracts have used in practice.
PARAM_RE = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\}|<([A-Za-z_][A-Za-z0-9_]*)>")

# Entrypoint shapes, by language. Deliberately a short, defensible list: a
# checker that claims to find "the entrypoint" of an arbitrary repo and
# silently finds none would report a clean zero for the wrong reason.
ENTRYPOINT_GLOBS = ("cmd/*/main.go", "cmd/*/*.go", "main.go", "main.py",
                    "manage.py", "src/main.ts", "src/main.js", "src/index.ts")
TEST_MARKERS = ("_test.go", "test_", "_test.py", ".test.ts", ".test.js",
                ".spec.ts", ".spec.js", "_test.ts")

SERVED = "served"
SERVED_GATED = "served_gated"
NOT_SERVED = "not_served"
ERROR_STATUS = "error_status"
NOT_PROBED = "not_probed"
UNPROBEABLE = "unprobeable"
UNREACHABLE = "unreachable"

BLOCKING_STATES = {NOT_SERVED, ERROR_STATUS, UNREACHABLE}


@dataclass
class RouteReport:
    method: str
    path: str
    probed_url: str | None
    status: int | None
    state: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EntrypointReport:
    path: str
    has_tests: bool
    covered_by_probe: bool
    state: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    routes: list[RouteReport] = field(default_factory=list)
    entrypoints: list[EntrypointReport] = field(default_factory=list)
    unparsed: list[dict] = field(default_factory=list)
    contracts_scanned: int = 0
    declared_operations: int = 0
    external_skipped: int = 0
    base_url: str | None = None

    @property
    def findings(self) -> list:
        return ([r for r in self.routes if r.state in BLOCKING_STATES]
                + [e for e in self.entrypoints if e.state == "untested_and_unbooted"])

    @property
    def applicability(self) -> str:
        if self.unparsed:
            return "error"
        # Nothing was composed and nothing was asked. The entrypoint half alone
        # is not this gate's question.
        if self.base_url is None or not self.routes:
            return "inapplicable"
        return "applicable"

    @property
    def outcome(self) -> str:
        if self.unparsed:
            return "error"
        if self.applicability == "inapplicable":
            return "inapplicable"
        return "findings" if self.findings else "pass"

    @property
    def measured(self) -> dict:
        by_state: dict[str, int] = {}
        for r in self.routes:
            by_state[r.state] = by_state.get(r.state, 0) + 1
        return {
            "contracts_scanned": self.contracts_scanned,
            "declared_operations": self.declared_operations,
            "external_skipped": self.external_skipped,
            "routes_considered": len(self.routes),
            "base_url_supplied": self.base_url is not None,
            "by_state": by_state,
            "entrypoints_found": len(self.entrypoints),
            "entrypoints_untested_and_unbooted": sum(
                1 for e in self.entrypoints if e.state == "untested_and_unbooted"),
            "files_unparsed": len(self.unparsed),
        }


# --- inventory ----------------------------------------------------------------

def collect_routes(targets: list[Path], report: Report) -> list[dict]:
    """Operations this service SERVES — every one not marked external."""
    routes: list[dict] = []
    for path in _contracts_files(targets):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            report.unparsed.append({"file": str(path), "reason": f"{type(exc).__name__}: {exc}"})
            continue
        report.contracts_scanned += 1
        entities = data.get("entities") if isinstance(data, dict) else None
        if not isinstance(entities, list):
            continue
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            operations = entity.get("operations")
            if not isinstance(operations, list):
                continue
            for operation in operations:
                if not isinstance(operation, dict):
                    continue
                report.declared_operations += 1
                if operation.get("external") is True:
                    # Calls OUT. chief-wiggum#353's question, not this one.
                    report.external_skipped += 1
                    continue
                method = str(operation.get("method") or "GET").upper()
                op_path = operation.get("path")
                if not isinstance(op_path, str) or not op_path:
                    continue
                routes.append({
                    "method": method,
                    "path": op_path,
                    "expected": _declared_statuses(operation),
                })
    return routes


def _contracts_files(targets: list[Path]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        if target.is_dir():
            files.extend(sorted(target.rglob("contracts.json")))
        elif target.name == "contracts.json":
            files.append(target)
    return files


def _declared_statuses(operation: dict) -> set[int]:
    """Statuses the operation itself says it returns. A 409 the contract
    declares is the route working, not the route missing."""
    out: set[int] = set()
    for case in operation.get("error_cases") or []:
        if isinstance(case, dict) and isinstance(case.get("status"), int):
            out.add(case["status"])
    return out


def substitute(path: str, params: dict[str, str]) -> tuple[str, list[str]]:
    """Fill path parameters; report the ones left unfilled."""
    missing: list[str] = []

    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(2) or m.group(3)
        if name in params:
            return params[name]
        missing.append(name)
        return m.group(0)

    return PARAM_RE.sub(repl, path), missing


# --- probing ------------------------------------------------------------------

def default_probe(url: str, method: str, *, timeout: float = 10.0) -> int:
    """Return the HTTP status, or raise. Mirrors saas_gate.default_http_get's
    injectable shape so tests never touch the network."""
    req = urllib.request.Request(url, method=method,
                                 headers={"User-Agent": "chief-wiggum-boot-and-hit"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-supplied URL
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def classify(status: int, expected: set[int]) -> tuple[str, str]:
    if status in expected:
        return SERVED, f"{status} — a status this operation declares"
    if 200 <= status < 400:
        return SERVED, str(status)
    if status in (401, 403):
        return (SERVED_GATED,
                f"{status} — the route is registered; an auth layer answered first")
    if status in (404, 405):
        return (NOT_SERVED,
                f"{status} — declared in contracts.json and not wired into the "
                f"assembled service")
    if status >= 500:
        return ERROR_STATUS, f"{status} — registered and erroring"
    return SERVED, f"{status}"


def probe_routes(routes: list[dict], base_url: str, params: dict[str, str],
                 probe_mutating: bool, probe=default_probe) -> list[RouteReport]:
    out: list[RouteReport] = []
    root = base_url.rstrip("/")
    for route in routes:
        method, path, expected = route["method"], route["path"], route["expected"]

        if method not in SAFE_METHODS and not probe_mutating:
            out.append(RouteReport(
                method, path, None, None, NOT_PROBED,
                "a mutating method is not probed by default — firing it at a real "
                "service has side effects. Pass --probe-mutating for a disposable target"))
            continue

        filled, missing = substitute(path, params)
        if missing:
            out.append(RouteReport(
                method, path, None, None, UNPROBEABLE,
                f"unfilled path parameter(s): {', '.join(missing)} — a 404 from an "
                f"invented value means 'no such record' as readily as 'not "
                f"registered'. Pass --path-param {missing[0]}=<value>"))
            continue

        url = root + (filled if filled.startswith("/") else "/" + filled)
        try:
            status = probe(url, method)
        except Exception as exc:  # noqa: BLE001 - any transport failure is the same verdict
            out.append(RouteReport(
                method, path, url, None, UNREACHABLE,
                f"the request failed ({type(exc).__name__}: {exc}) — the service is "
                f"not answering at all, which is never a pass"))
            continue

        state, detail = classify(status, expected)
        out.append(RouteReport(method, path, url, status, state, detail))
    return out


# --- entrypoints --------------------------------------------------------------

def find_entrypoints(source_root: Path) -> list[Path]:
    found: list[Path] = []
    if not source_root.is_dir():
        return found
    for pattern in ENTRYPOINT_GLOBS:
        for path in sorted(source_root.glob(pattern)):
            if path.is_file() and not _is_test_file(path) and path not in found:
                found.append(path)
    return found


def _is_test_file(path: Path) -> bool:
    name = path.name
    return any(name.endswith(m) or name.startswith(m) for m in TEST_MARKERS)


def _dir_has_tests(directory: Path) -> bool:
    return any(_is_test_file(p) for p in directory.glob("*") if p.is_file())


def evaluate_entrypoints(source_root: Path, probe_succeeded: bool) -> list[EntrypointReport]:
    reports: list[EntrypointReport] = []
    for entry in find_entrypoints(source_root):
        has_tests = _dir_has_tests(entry.parent)
        rel = str(entry.relative_to(source_root))
        if has_tests:
            state, detail = "tested", "the entrypoint's package has test files"
        elif probe_succeeded:
            # The conjunction #352 asked for. Routes were booted and answered,
            # so the composition IS exercised; demanding a unit test as well
            # would be cargo cult.
            state, detail = ("covered_by_probe",
                             "no test files, but the assembled service booted and "
                             "served its routes — the composition is exercised")
        else:
            state, detail = ("untested_and_unbooted",
                             "the entrypoint has no test files AND no boot-and-hit "
                             "coverage — nothing composes this binary and asks it "
                             "for anything")
        reports.append(EntrypointReport(rel, has_tests, probe_succeeded, state, detail))
    return reports


# --- orchestration ------------------------------------------------------------

def check(targets: list[Path], source_root: Path, base_url: str | None = None,
          params: dict[str, str] | None = None, probe_mutating: bool = False,
          probe=default_probe) -> Report:
    report = Report(base_url=base_url)
    routes = collect_routes(targets, report)

    if base_url is not None:
        report.routes = probe_routes(routes, base_url, params or {}, probe_mutating, probe)

    # "Booted and answered" means at least one route came back registered. A
    # sweep where every route was unreachable is not coverage.
    probe_succeeded = any(r.state in (SERVED, SERVED_GATED) for r in report.routes)
    report.entrypoints = evaluate_entrypoints(source_root, probe_succeeded)
    return report


def render_text(report: Report, gating: bool) -> str:
    m = report.measured
    lines = [
        f"Measured: {m['contracts_scanned']} contracts file(s), "
        f"{m['declared_operations']} operation(s) "
        f"({m['external_skipped']} external, skipped — that is #353's question), "
        f"{m['routes_considered']} route(s) probed, "
        f"{m['entrypoints_found']} entrypoint(s)"
    ]

    if report.unparsed:
        lines += ["", "ERROR: input(s) present that could not be read (chief-wiggum#289):"]
        lines += [f"  {u['file']}: {u['reason']}" for u in report.unparsed]

    if report.base_url is None:
        lines += ["", "INAPPLICABLE: no --base-url, so nothing was composed and nothing "
                      "was asked. A green build and green package tests do not exercise "
                      "the assembled binary — that is the whole point of this gate. "
                      "Start the service and pass its URL. This is not a pass."]
    elif not report.routes:
        lines += ["", "INAPPLICABLE: the epic declares no route this service serves; "
                      "nothing was checked (not a pass)"]

    for state, label in (
        (NOT_SERVED, "NOT SERVED — declared and not wired"),
        (ERROR_STATUS, "ERROR STATUS — registered and erroring"),
        (UNREACHABLE, "UNREACHABLE — the service did not answer"),
        (UNPROBEABLE, "UNPROBEABLE — parameterized path, no substitution supplied"),
        (NOT_PROBED, "NOT PROBED — mutating method (pass --probe-mutating)"),
        (SERVED_GATED, "SERVED (auth-gated)"),
        (SERVED, "SERVED"),
    ):
        group = [r for r in report.routes if r.state == state]
        if not group:
            continue
        lines += ["", f"## {label} ({len(group)})"]
        for r in group:
            lines.append(f"  {r.method} {r.path}: {r.detail}")

    if report.entrypoints:
        lines += ["", "## Entrypoints"]
        for e in report.entrypoints:
            lines.append(f"  {e.path} [{e.state}]: {e.detail}")

    if report.findings and not gating:
        lines += ["", "(report-only: exiting 0. Pass --gate to block — see "
                      "docs/gate-rollout.md)"]
    return "\n".join(lines)


def _parse_params(raw: list[str] | None) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in raw or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"--path-param needs name=value, got {item!r}")
        params[key] = value
    return params


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="The assembled service must start and serve its declared routes (chief-wiggum#352)"
    )
    parser.add_argument("targets", nargs="+", help="Epic directory or contracts.json file(s)")
    parser.add_argument("--source", required=True, help="Repo root, for entrypoint detection")
    parser.add_argument("--base-url", help="A RUNNING instance to probe")
    parser.add_argument("--path-param", action="append", dest="path_params",
                        help="name=value substitution for a parameterized route (repeatable)")
    parser.add_argument("--probe-mutating", action="store_true",
                        help="Also probe POST/PUT/PATCH/DELETE. Only for a disposable target.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--gate", action="store_true",
                        help="Block (exit 1) on findings. Report-only without it.")
    args = parser.parse_args(argv)

    targets = [Path(t) for t in args.targets]
    missing = [t for t in targets if not t.exists()]
    source = Path(args.source)
    if not source.exists():
        missing.append(source)
    if missing:
        for t in missing:
            print(f"ERROR: {t} does not exist", file=sys.stderr)
        return EXIT_USAGE

    try:
        params = _parse_params(args.path_params)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE

    report = check(targets, source, args.base_url, params, args.probe_mutating)

    if args.format == "json":
        print(json.dumps({
            "routes": [r.to_dict() for r in report.routes],
            "entrypoints": [e.to_dict() for e in report.entrypoints],
            "count": len(report.findings),
            "applicability": report.applicability,
            "outcome": report.outcome,
            "gating": args.gate,
            "measured": report.measured,
            "unparsed": report.unparsed,
        }, indent=2))
    else:
        print(render_text(report, args.gate))

    try:  # factory telemetry; no-op unless enabled, never breaks the gate
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from factory_log import emit_gate
        emit_gate("check_boot_and_hit", report.outcome, caught=len(report.findings))
    except Exception:
        pass

    if report.unparsed:
        return EXIT_ERROR
    if report.findings and args.gate:
        return EXIT_FINDINGS
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
