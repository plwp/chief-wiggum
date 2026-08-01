"""check_deferred_triggers.py (#171/#161 close-out) — the deferral index's
mechanical trigger checker: report-only always, exact checks may FIRE,
heuristic checks cap at CANDIDATE, unevaluable triggers surface as
UNEVALUATED (never silently skipped)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import check_deferred_triggers as cdt

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX = REPO_ROOT / "docs" / "deferred-rigor.json"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_deferred_triggers.py"), *args],
        capture_output=True, text=True)


def _write_log(tmp_path: Path, events: list[dict]) -> Path:
    log = tmp_path / "factory-log.jsonl"
    log.write_text("".join(json.dumps(e) + "\n" for e in events))
    return log


# --- shipped index integrity --------------------------------------------------

def test_shipped_index_is_valid_and_covers_both_source_issues():
    index = json.loads(INDEX.read_text())
    assert sorted(index["source_issues"]) == [161, 171]
    items = index["items"]
    assert len(items) == 11  # 10 rows from #171's table + the #161 CAS row
    assert all(it["settled_notes"] for it in items), "every deferral keeps its settled design"
    assert all(it["trigger"] for it in items)
    by_issue = {161: 0, 171: 0}
    for it in items:
        by_issue[it["source_issue"]] += 1
    assert by_issue[161] == 1 and by_issue[171] == 10


def test_report_only_exit_zero_even_when_triggers_fire():
    res = _run("--repo", str(REPO_ROOT))
    assert res.returncode == 0, res.stderr
    # CW's own state: 7 validation records > 5 — the exact check fires.
    assert "[      FIRED] gate-validation-designer" in res.stdout


def test_json_format_round_trips():
    res = _run("--format", "json")
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert {it["id"] for it in payload["items"]} == {
        it["id"] for it in json.loads(INDEX.read_text())["items"]}


# --- status semantics ---------------------------------------------------------

def test_heuristic_checks_cap_at_candidate_never_fired(tmp_path):
    """One human under several emails must not mechanically demand an issue."""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    for i, email in enumerate(["a@x.com", "b@y.com"]):
        (repo / f"f{i}.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", f"user.email={email}",
                        "-c", "user.name=t", "commit", "-qm", "c"], check=True)
    status, detail = cdt.check_human_contributors_gte(repo, 2)
    assert status == "CANDIDATE" and "2 distinct" in detail


def test_bug_keyword_is_candidate_never_fired(tmp_path):
    log = _write_log(tmp_path, [
        {"event": "bug", "summary": "duplicate delivery of webhook X"},
        {"event": "bug", "summary": "unrelated"}])
    status, detail = cdt.check_telemetry_bug_keyword(log, ["duplicate"])
    assert status == "CANDIDATE" and "human confirms" in detail


def test_exact_telemetry_check_fires_at_threshold(tmp_path):
    log = _write_log(tmp_path, [{"event": "query", "name": "writers"}] * 3)
    assert cdt.check_telemetry_whole_repo_queries_gte(log, 3)[0] == "FIRED"
    assert cdt.check_telemetry_whole_repo_queries_gte(log, 4)[0] == "QUIET"


def test_missing_inputs_surface_as_unevaluated(tmp_path):
    assert cdt.check_file_count_gt(None, 10)[0] == "UNEVALUATED"
    assert cdt.check_telemetry_bug_keyword(tmp_path / "absent.jsonl", ["x"])[0] == "UNEVALUATED"
    # An item with no checks at all is UNEVALUATED, not silently dropped.
    result = cdt.evaluate_item(
        {"id": "i", "title": "t", "trigger": "human judgment"}, None, tmp_path / "absent.jsonl")
    assert result["status"] == "UNEVALUATED"


def test_overall_status_prefers_fired_over_candidate(tmp_path):
    log = _write_log(tmp_path, [
        {"event": "query", "name": "map"},
        {"event": "bug", "summary": "retry storm in worker"}])
    item = {"id": "i", "title": "t", "trigger": "x",
            "checks": [{"kind": "telemetry_whole_repo_queries_gte", "value": 1},
                       {"kind": "telemetry_bug_keyword", "keywords": ["retry storm"]}]}
    assert cdt.evaluate_item(item, None, log)["status"] == "FIRED"
