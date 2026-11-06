"""The performance gate (chief-wiggum#524): static rules, budgets, the cold
walk, the production p95 line, and the planted-defect self-test."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_perf  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "perf_gate"
SCRIPT = ROOT / "scripts" / "check_perf.py"


def _copy(name: str, tmp_path: Path) -> Path:
    dst = tmp_path / name
    shutil.copytree(FIXTURES / name, dst)
    return dst


def _plant_build_output(root: Path) -> None:
    """node_modules and .next are gitignored here, so plant them at test time.
    Each holds a GET handler that writes: if the scanner walks them, it fires."""
    body = (
        "package pkg\n\nimport \"net/http\"\n\n"
        "func handleVendored(w http.ResponseWriter, r *http.Request) {\n"
        "\tclient.RunTransaction(r.Context(), nil)\n}\n"
    )
    for rel in ("node_modules/pkg/vendored.go", ".next/server/chunk.go", "dist/x.go"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def _rules(report) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in report.findings:
        out.setdefault(f["rule"], []).append(f)
    return out


# --- static rules ---------------------------------------------------------------


def test_inline_sweep_on_get_is_a_write_on_read(tmp_path):
    root = _copy("inline_sweep", tmp_path)
    report = check_perf.scan_static(root)
    assert report.outcome == "findings"
    writes = _rules(report)["write-on-read"]
    assert len(writes) == 1
    w = writes[0]
    assert w["file"] == "server/store_firestore.go"
    assert w["line"] == 13  # the RunTransaction call
    assert w["handler"] == "handleStaff"
    assert "applySweep" in w["chain"]
    assert "AppendAction" in w["chain"]


def test_write_finding_names_file_and_line_in_text(tmp_path):
    root = _copy("inline_sweep", tmp_path)
    text = check_perf.render_static_text(check_perf.scan_static(root))
    assert "server/store_firestore.go:13: write-on-read" in text


def test_post_branch_and_post_only_route_are_not_read_paths(tmp_path):
    root = _copy("inline_sweep", tmp_path)
    report = check_perf.scan_static(root)
    handlers = {f["handler"] for f in report.findings if f["rule"] == "write-on-read"}
    assert "handleClient" not in handlers
    assert "handleImport" not in handlers


def test_build_output_is_never_walked(tmp_path):
    root = _copy("inline_sweep", tmp_path)
    _plant_build_output(root)
    report = check_perf.scan_static(root)
    files = {f["file"] for f in report.findings}
    assert not any(f.startswith(("node_modules", ".next", "dist")) for f in files)
    assert report.files_scanned == 3


def test_type_declarations_are_not_writes(tmp_path):
    root = _copy("inline_sweep", tmp_path)
    report = check_perf.scan_static(root)
    # Interface method AppendAction and struct fields Set/Update sit in
    # handlers.go; only call sites may be findings, and none there writes.
    for f in report.findings:
        if f["rule"] == "write-on-read":
            assert f["file"] != "server/handlers.go"
    assert not any(f["file"].endswith(".ts") for f in report.findings)


def test_tenant_only_filter_is_an_unbounded_list(tmp_path):
    root = _copy("inline_sweep", tmp_path)
    unbounded = _rules(check_perf.scan_static(root))["unbounded-list"]
    assert [(f["file"], f["line"]) for f in unbounded] == [("server/store_firestore.go", 20)]


def test_store_read_inside_a_loop_is_flagged(tmp_path):
    root = _copy("inline_sweep", tmp_path)
    loops = _rules(check_perf.scan_static(root))["read-in-loop"]
    assert ("server/handlers.go", 46) in {(f["file"], f["line"]) for f in loops}


def test_background_sweep_windowed_and_batched_reads_pass(tmp_path):
    root = _copy("background_sweep", tmp_path)
    report = check_perf.scan_static(root)
    assert report.findings == []
    assert report.outcome == "pass"
    assert report.handlers >= 1


def test_ok_marker_suppresses_a_reviewed_site(tmp_path):
    root = _copy("inline_sweep", tmp_path)
    p = root / "server" / "handlers.go"
    p.write_text(p.read_text().replace(
        "\ts.applySweep(ctx, site, recs)\n",
        "\ts.applySweep(ctx, site, recs) // cw-perf: ok reviewed, drained once\n"))
    report = check_perf.scan_static(root)
    assert "write-on-read" not in _rules(report)
    assert report.suppressed >= 1


def test_no_go_handlers_is_inapplicable(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    report = check_perf.scan_static(tmp_path)
    assert report.outcome == "inapplicable"


def test_cli_report_only_exits_zero_and_gate_exits_one(tmp_path):
    root = _copy("inline_sweep", tmp_path)
    ro = subprocess.run([sys.executable, str(SCRIPT), "static", str(root)],
                        capture_output=True, text=True)
    assert ro.returncode == 0, ro.stderr
    gated = subprocess.run([sys.executable, str(SCRIPT), "static", str(root), "--gate"],
                           capture_output=True, text=True)
    assert gated.returncode == 1
    clean = subprocess.run([sys.executable, str(SCRIPT), "static",
                            str(_copy("background_sweep", tmp_path)), "--gate"],
                           capture_output=True, text=True)
    assert clean.returncode == 0, clean.stdout


def test_self_test_plants_and_catches_the_defect():
    proc = subprocess.run([sys.executable, str(SCRIPT), "self-test"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "fired" in proc.stdout


# --- budgets --------------------------------------------------------------------


def _models(tmp_path: Path) -> Path:
    d = tmp_path / "models"
    d.mkdir()
    (d / "contracts.json").write_text(json.dumps({"entities": [{
        "name": "Booking",
        "operations": [
            {"name": "list", "method": "GET", "path": "/api/staff/{site}/bookings",
             "latency_budget_ms": 1500},
            {"name": "get", "method": "GET", "path": "/api/health"},
            {"name": "save", "method": "POST", "path": "/api/bookings"},
            {"name": "kc", "method": "GET", "path": "/kc", "external": True},
        ]}]}))
    (d / "ui-spec.json").write_text(json.dumps({
        "pages": {
            "/staff/bookings": {"layout": "sidebar-main", "auth": "required"},
            "/book": {"layout": "centered", "auth": "public", "latency_budget_ms": 3000},
            "/landing": {"layout": "centered", "auth": "public"},
        },
        "navigation": {}}))
    return d


def test_budgets_read_declared_fields_and_default_staff_screens(tmp_path):
    targets = check_perf.load_budgets(_models(tmp_path))
    by = {(t["kind"], t["path"]): t for t in targets}
    assert by[("operation", "/api/staff/{site}/bookings")]["budget_ms"] == 1500
    assert by[("operation", "/api/staff/{site}/bookings")]["budget_source"] == "declared"
    assert by[("operation", "/api/health")]["budget_ms"] == 2000
    assert by[("operation", "/api/health")]["budget_source"] == "default"
    assert ("operation", "/kc") not in by  # external: someone else's latency
    assert by[("screen", "/staff/bookings")]["budget_ms"] == 2000
    assert by[("screen", "/book")]["budget_ms"] == 3000
    # A public page with no declared budget has none: the default is for staff screens.
    assert ("screen", "/landing") not in by


def test_schemas_accept_the_budget_field():
    contracts = json.loads((ROOT / "templates/formal-models/contracts-schema.json").read_text())
    ui = json.loads((ROOT / "templates/formal-models/ui-spec-schema.json").read_text())
    op = contracts["$defs"]["operation"]["properties"]["latency_budget_ms"]
    page = ui["$defs"]["page"]["properties"]["latency_budget_ms"]
    assert op["type"] == "integer" and page["type"] == "integer"


# --- the walk -------------------------------------------------------------------


class FakeClock:
    """Each fetch advances the clock by the scripted duration for that URL."""

    def __init__(self, script: dict[str, list[float]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.now = 0.0
        self.calls: list[tuple[str, str]] = []

    def time(self) -> float:
        return self.now

    def fetch(self, url: str, method: str, headers: dict) -> int:
        self.calls.append((method, url))
        self.now += self.script[url].pop(0)
        return 200


def test_walk_times_cold_and_warm_and_fails_over_budget(tmp_path):
    base = "http://localhost:8080"
    clock = FakeClock({
        f"{base}/api/staff/hg/bookings": [20.0, 0.4],  # cold load blows the budget
        f"{base}/api/health": [0.01, 0.01],
    })
    report = check_perf.walk_operations(
        check_perf.load_budgets(_models(tmp_path)), base,
        params={"site": "hg"}, fetch=clock.fetch, clock=clock.time)
    by = {e["path"]: e for e in report["operations"]}
    slow = by["/api/staff/{site}/bookings"]
    assert slow["cold_ms"] == 20000 and slow["warm_ms"] == 400
    assert slow["verdict"] == "over_budget"
    assert by["/api/health"]["verdict"] == "within_budget"
    # Never fires a mutating request without --probe-mutating.
    assert by["/api/bookings"]["verdict"] == "not_probed"
    assert all(m == "GET" for m, _ in clock.calls)
    assert report["outcome"] == "findings"


def test_walk_parameterised_route_without_value_is_unprobeable(tmp_path):
    clock = FakeClock({"http://x/api/health": [0.01, 0.01]})
    report = check_perf.walk_operations(
        check_perf.load_budgets(_models(tmp_path)), "http://x",
        params={}, fetch=clock.fetch, clock=clock.time)
    by = {e["path"]: e for e in report["operations"]}
    assert by["/api/staff/{site}/bookings"]["verdict"] == "unprobeable"


def _har(page_url: str, entries: list[tuple[float, float, str]]) -> dict:
    """entries: (start offset ms, duration ms, url)."""
    def iso(ms: float) -> str:
        whole, frac = divmod(int(ms), 1000)
        return f"2026-11-06T10:00:{whole:02d}.{frac:03d}Z"
    return {"log": {
        "pages": [{"id": "p1", "title": page_url, "startedDateTime": iso(0),
                   "pageTimings": {"onLoad": 300}}],
        "entries": [{"pageref": "p1", "startedDateTime": iso(start), "time": dur,
                     "request": {"method": "GET", "url": url},
                     "response": {"status": 200},
                     "_resourceType": "fetch" if "/api/" in url else "document"}
                    for start, dur, url in entries]}}


def test_screen_walk_reads_har_waterfall_cold_and_warm(tmp_path):
    cold = tmp_path / "cold.har"
    warm = tmp_path / "warm.har"
    page = "http://localhost:3000/staff/bookings"
    cold.write_text(json.dumps(_har(page, [
        (0, 120, page), (150, 100, "http://localhost:8080/api/staff/me"),
        (260, 20000, "http://localhost:8080/api/staff/hg/bookings")])))
    warm.write_text(json.dumps(_har(page, [
        (0, 80, page), (100, 400, "http://localhost:8080/api/staff/hg/bookings")])))
    report = check_perf.walk_screens(
        check_perf.load_budgets(_models(tmp_path)), cold_har=cold, warm_har=warm)
    s = {e["path"]: e for e in report["screens"]}["/staff/bookings"]
    assert s["cold_ms"] == 20260 and s["warm_ms"] == 500
    assert s["verdict"] == "over_budget"
    assert s["waterfall"]["cold"][-1]["url"].endswith("/api/staff/hg/bookings")
    # Declared screens the walk never loaded are reported, not passed.
    assert {e["path"]: e for e in report["screens"]}["/book"]["verdict"] == "not_walked"


# --- production telemetry -------------------------------------------------------


def _log(path: str, latency_s: float, method: str = "GET", status: int = 200) -> dict:
    return {"httpRequest": {"requestMethod": method,
                            "requestUrl": f"https://api.example.invalid{path}?from=2026-11-06",
                            "status": status, "latency": f"{latency_s}s"}}


def test_telemetry_line_gives_p50_p95_per_endpoint_and_flags_budget(tmp_path):
    entries = [_log("/api/staff/site-a/bookings", s) for s in
               [0.4, 0.5, 0.6, 0.7, 20.0, 20.0, 0.5, 0.6, 0.5, 0.4]]
    entries += [_log("/api/staff/site-b/bookings", 0.3)]
    entries += [_log("/api/staff/site-a/client/abc123", 0.2)]
    entries += [_log("/healthz", 0.001)]
    budgets = check_perf.load_budgets(_models(tmp_path))
    stats = check_perf.endpoint_stats(entries, budgets)
    by = {(s["method"], s["route"]): s for s in stats}
    b = by[("GET", "/api/staff/{site}/bookings")]
    assert b["count"] == 11
    assert b["p95_ms"] == 20000
    assert b["budget_ms"] == 1500 and b["over_budget"] is True
    line = check_perf.briefing_line(stats, window="24h")
    assert line.startswith("Perf (24h):")
    assert "GET /api/staff/{site}/bookings" in line and "OVER" in line


def test_telemetry_parses_cloud_run_latency_formats():
    assert check_perf._latency_ms("0.250s") == 250
    assert check_perf._latency_ms("20.000123456s") == 20000
    assert check_perf._latency_ms({"seconds": 1, "nanos": 500000000}) == 1500
    assert check_perf._latency_ms(None) is None


# --- /saas-gate integration -----------------------------------------------------


def test_saas_gate_fails_on_the_planted_defect_and_passes_the_fix(tmp_path):
    import saas_gate as sg

    bad = sg.run_gate(_copy("inline_sweep", tmp_path), None)
    perf = {f.name: f for f in bad.findings if f.category == "performance"}
    assert perf["write-on-read"].status == sg.FAIL
    assert "server/store_firestore.go:13" in perf["write-on-read"].detail
    assert perf["budget-walk"].status == sg.SKIPPED
    assert bad.ok is False

    good = sg.run_gate(_copy("background_sweep", tmp_path), None)
    perf = {f.name: f for f in good.findings if f.category == "performance"}
    assert {perf[r].status for r in ("write-on-read", "unbounded-list", "read-in-loop")} == {sg.PASS}
    assert good.ok is True


def test_saas_gate_walk_fails_an_operation_over_budget(tmp_path):
    import saas_gate as sg

    clock = FakeClock({"http://x/api/staff/hg/bookings": [20.0, 0.3],
                       "http://x/api/health": [0.01, 0.01]})
    out = check_perf.saas_findings(
        _copy("background_sweep", tmp_path), models=[_models(tmp_path)], base_url="http://x",
        params={"site": "hg"}, fetch=clock.fetch, clock=clock.time)
    by = {n: (s, d) for n, s, d in out}
    assert by["budget-walk"][0] == sg.FAIL
    assert "GET /api/staff/{site}/bookings over_budget (cold 20000 ms" in by["budget-walk"][1]
    assert by["screen-walk"][0] == sg.SKIPPED


def test_telemetry_route_templates_group_tenant_slugs():
    entries = [_log("/api/staff/site-a/sheets", 0.4), _log("/api/staff/site-b/sheets", 0.6)]
    stats = check_perf.endpoint_stats(entries, [], routes=["/api/staff/{site}/sheets"])
    assert [(s["route"], s["count"], s["budget_ms"]) for s in stats] == [("/api/staff/{site}/sheets", 2, 2000)]


def test_validation_record_self_test_trials_match_a_live_run():
    record = json.loads((ROOT / "docs/quality/validation/perf-gate.json").read_text())
    assert record["gate"] == "perf-gate" and record["status"] == "report-only"
    recorded = {t["seed_id"]: t for t in record["seeded_defect_trials"]}
    for live in check_perf.self_test():
        assert recorded[live["seed_id"]]["result"] == live["result"]
        assert recorded[live["seed_id"]]["passed"] is live["passed"] is True
    assert all(t["passed"] for t in record["seeded_defect_trials"])


def test_route_table_row_naming_post_is_not_a_read_path(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "routes.go").write_text('''package main

import (
	"context"
	"net/http"
)

type route struct {
	method  string
	handler func(*server, http.ResponseWriter, *http.Request)
}

var routes = map[string]route{
	"submit": {method: http.MethodPost, handler: (*server).handleSubmit},
	"list":   {method: http.MethodGet, handler: (*server).handleList},
}

type server struct{ client *fsClient }
type fsClient struct{}

func (s *server) handleSubmit(w http.ResponseWriter, r *http.Request) {
	s.client.RunTransaction(r.Context(), nil)
}

func (s *server) handleList(w http.ResponseWriter, r *http.Request) {
	s.save(r.Context())
}

func (s *server) save(ctx context.Context) {
	s.client.RunTransaction(ctx, nil)
}
''')
    report = check_perf.scan_static(tmp_path)
    writes = [f for f in report.findings if f["rule"] == "write-on-read"]
    assert [(f["handler"], f["line"]) for f in writes] == [("handleList", 30)]
