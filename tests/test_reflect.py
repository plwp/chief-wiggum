"""Tests for scripts/reflect.py (post-hoc factory-effectiveness miner)."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import reflect  # noqa: E402

# --- pure parsers -----------------------------------------------------------

def test_parse_git_log_and_classify():
    log = "abc\x1ffeat(x): add\ndef\x1ffix(y): repair\nghi\x1fRevert \"feat(x): add\""
    commits = reflect.parse_git_log(log)
    assert [c["hash"] for c in commits] == ["abc", "def", "ghi"]
    kinds = reflect.classify_commits(commits)
    # conventional-commit histogram; GitHub-style `Revert "..."` has no `:` -> "other"
    assert kinds["feat"] == 1 and kinds["fix"] == 1 and kinds["other"] == 1
    # ...but it IS caught as a revert for slippage
    assert len(reflect.detect_reverts(commits)) == 1


def test_detect_reverts_and_slippage():
    commits = [
        {"hash": "1", "subject": "feat(a): new"},
        {"hash": "2", "subject": "fix(a): broke prod"},
        {"hash": "3", "subject": "Revert \"feat(a): new\""},
        {"hash": "4", "subject": "hotfix: urgent"},
        {"hash": "5", "subject": "docs: readme"},
    ]
    assert len(reflect.detect_reverts(commits)) == 1
    slips = reflect.slippage_commits(commits)
    assert {c["hash"] for c in slips} == {"2", "3", "4"}


def test_gate_mentions_and_force_bypasses():
    texts = ["ran check_traceability and ratchet", "merged with --force past the ratchet",
             "fixed @cw-writes single-writer violation"]
    mentions = reflect.gate_mentions(texts)
    assert mentions["traceability"] >= 1
    assert mentions["ratchet"] >= 2
    assert mentions["single_writer"] >= 1
    assert reflect.force_bypasses(texts) == 1


def test_ratchet_health():
    records = [
        {"gate_result": "pass", "merged": True, "amended": {"CTR-a-001": ["h"]}},
        {"gate_result": "forced", "merged": True, "retired": ["INV-b-002"]},
        {"gate_result": "fail", "merged": False},
    ]
    h = reflect.ratchet_health(records)
    assert h["records"] == 3 and h["merged"] == 2
    assert h["forced_merges"] == 1 and h["gate_failed"] == 1
    assert h["amended_contracts"] == 1 and h["retired_contracts"] == 1


def test_pattern_coverage_flags_unfolded_invariants():
    adopted = {"patterns": {"p": {"invariants": ["INV-FOWR-001", "INV-FOWR-004"],
                                   "unresolved": ["resource"]}}}
    invariants = "## Epic\n- **INV-FOWR-001** — trigger only\n"  # 004 missing
    cov = reflect.pattern_coverage(adopted, invariants)[0]
    assert cov["promised"] == 2 and cov["folded"] == 1
    assert cov["missing"] == ["INV-FOWR-004"]
    assert cov["unresolved_params"] == ["resource"]


# --- end-to-end over a synthetic repo ---------------------------------------

def _init_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)


def _commit(tmp_path, subject):
    (tmp_path / "f.txt").write_text(subject)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", subject], check=True)


def test_collect_end_to_end(tmp_path):
    _init_repo(tmp_path)
    _commit(tmp_path, "feat(x): thing")
    _commit(tmp_path, "fix(x): slipped bug")
    # an unresolved marker in docs
    (tmp_path / "docs" / "epics" / "e").mkdir(parents=True)
    (tmp_path / "docs/epics/e/invariants.md").write_text(
        "## Epic\n- **INV-FOWR-001** — trigger only\nTBD: confirm the schema name\n")
    (tmp_path / "docs/patterns").mkdir(parents=True)
    (tmp_path / "docs/patterns/adopted.json").write_text(json.dumps(
        {"patterns": {"fetch-on-webhook-reconcile":
                      {"invariants": ["INV-FOWR-001", "INV-FOWR-004"], "unresolved": []}}}))

    report = reflect.collect(tmp_path)
    assert report["slippage_commits"] == 1
    assert report["assumptions"]["total"] >= 1
    dims = {f["dimension"] for f in report["findings"]}
    assert "slippage" in dims and "assumptions" in dims and "patterns" in dims
    # INV-FOWR-004 promised but not folded -> a patterns finding
    assert any("never folded" in f["message"] for f in report["findings"])


def test_collect_consumes_factory_telemetry(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    _commit(tmp_path, "feat: init")
    log = tmp_path / "factory-log.jsonl"
    repo_key = tmp_path.name  # reflect filters telemetry by repo.name
    log.write_text("\n".join(json.dumps(r) for r in [
        {"event": "gate", "name": "noisy", "result": "pass", "caught": 0, "duration_ms": 5, "repo": repo_key},
        {"event": "gate", "name": "noisy", "result": "pass", "caught": 0, "duration_ms": 5, "repo": repo_key},
        {"event": "gate", "name": "noisy", "result": "pass", "caught": 0, "duration_ms": 5, "repo": repo_key},
    ]) + "\n")
    monkeypatch.setenv("CW_FACTORY_LOG", str(log))
    report = reflect.collect(tmp_path)
    assert report["telemetry"]["gates"]["noisy"]["value"] == "noise-candidate"
    assert any("candidate noise" in f["message"] for f in report["findings"])


def test_cli_json(tmp_path):
    _init_repo(tmp_path)
    _commit(tmp_path, "feat: init")
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "reflect.py"), str(tmp_path), "--format", "json"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["commits_scanned"] == 1


def test_cli_rejects_non_repo(tmp_path):
    proc = subprocess.run([sys.executable, str(SCRIPTS / "reflect.py"), str(tmp_path)],
                          capture_output=True, text=True)
    assert proc.returncode == 2
