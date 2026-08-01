"""Tests for scripts/plan_from_debt.py (#216) — clustering precedence, the
REQUIRED budget, boundary referrals, grandfather handling, pathset derivation
(ratchet-compatible shape), dependency ordering, and the /close-epic verify
acceptance check."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import plan_from_debt
import pytest
import ratchet

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _item(iid: str, engine: str = "markers", severity: str = "low",
          locations: list[str] | None = None, partners: list[str] | None = None,
          **extra) -> dict:
    return {
        "id": iid, "engine": engine, "kind": "TODO", "severity": severity,
        "symbol": iid, "detail": f"detail for {iid}",
        "locations": locations if locations is not None else ["pkg/a.py:3"],
        "blast_radius": {
            "coupling_partners": [{"file": f, "count": 2} for f in (partners or [])],
            "hotspot_decile": None,
        },
        **extra,
    }


def _debt(items: list[dict], tmp_path: Path, name: str = "debt.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps({"schema": "debt/1", "target_sha": "abc123",
                             "items": items}, indent=2))
    return p


@pytest.fixture()
def target(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEF_WIGGUM_USER_DIR", str(tmp_path / "cw-home"))
    repo = tmp_path / "target"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.name", "A"],
                 ["config", "user.email", "a@e.co"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "seed.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed", "--no-verify"],
                   check=True, capture_output=True)
    return repo


def _plan(items, target, tmp_path, **budget):
    import artifacts
    debt_file = _debt(items, tmp_path)
    resolver = artifacts.Resolver.resolve(str(target))
    return plan_from_debt.build_plan(
        plan_from_debt.load_debt(debt_file), str(debt_file), resolver,
        budget_count=budget.get("count"),
        severity_floor=budget.get("floor"),
        cluster_cap=budget.get("cap"),
    )


# --- clustering precedence ----------------------------------------------------


def test_clone_class_items_are_singleton_tickets(target, tmp_path):
    items = [
        _item("DEBT-clone00001", engine="clones", severity="high",
              locations=["a/x.py:1", "b/y.py:9"], symbol="cafecafe"),
        _item("DEBT-marker0001", locations=["a/x.py:5"]),
    ]
    plan = _plan(items, target, tmp_path, count=10)
    strategies = {t["cluster"]["strategy"] for t in plan["tickets"]}
    assert strategies == {"clone-class", "module"}
    clone = next(t for t in plan["tickets"] if t["cluster"]["strategy"] == "clone-class")
    assert clone["debt_ids"] == ["DEBT-clone00001"]
    assert clone["kind"] == "refactor"


def test_module_clustering_groups_same_directory(target, tmp_path):
    items = [
        _item("DEBT-aaaa000001", locations=["pkg/a.py:1"]),
        _item("DEBT-bbbb000001", locations=["pkg/b.py:2"]),
        _item("DEBT-cccc000001", locations=["other/c.py:3"]),
    ]
    plan = _plan(items, target, tmp_path, count=10)
    by_key = {t["cluster"]["key"]: t for t in plan["tickets"]}
    assert set(by_key) == {"pkg", "other"}
    assert sorted(by_key["pkg"]["debt_ids"]) == ["DEBT-aaaa000001", "DEBT-bbbb000001"]


def test_coupling_partnership_merges_module_clusters(target, tmp_path):
    items = [
        _item("DEBT-aaaa000001", locations=["pkg/a.py:1"], partners=["svc/handler.py"]),
        _item("DEBT-bbbb000001", locations=["svc/handler.py:2"]),
        _item("DEBT-cccc000001", locations=["lonely/c.py:3"]),
    ]
    plan = _plan(items, target, tmp_path, count=10)
    coupled = [t for t in plan["tickets"] if t["cluster"]["strategy"] == "coupling"]
    assert len(coupled) == 1
    assert sorted(coupled[0]["debt_ids"]) == ["DEBT-aaaa000001", "DEBT-bbbb000001"]
    assert "pkg" in coupled[0]["cluster"]["key"] and "svc" in coupled[0]["cluster"]["key"]
    # the uncoupled module stays its own ticket
    assert any(t["cluster"] == {"strategy": "module", "key": "lonely"}
               for t in plan["tickets"])


def test_precedence_documented_in_plan(target, tmp_path):
    plan = _plan([_item("DEBT-aaaa000001")], target, tmp_path, count=1)
    text = " ".join(plan["clustering"]["precedence"])
    assert "clone-class" in text and "module" in text and "coupling" in text


# --- budget -------------------------------------------------------------------


def test_budget_count_cuts_lowest_ranked_clusters_and_records_excluded(target, tmp_path):
    items = [
        _item("DEBT-hi00000001", severity="high", locations=["hot/a.py:1"]),
        _item("DEBT-lo00000001", severity="low", locations=["cold/b.py:1"]),
    ]
    plan = _plan(items, target, tmp_path, count=1)
    assert len(plan["tickets"]) == 1
    assert plan["tickets"][0]["debt_ids"] == ["DEBT-hi00000001"]
    assert plan["excluded"] == [{"id": "DEBT-lo00000001", "engine": "markers",
                                 "severity": "low", "reason": "over_budget"}]


def test_severity_floor_excludes_items_below_it(target, tmp_path):
    items = [
        _item("DEBT-hi00000001", severity="medium", locations=["p/a.py:1"]),
        _item("DEBT-lo00000001", severity="low", locations=["p/a.py:9"]),
    ]
    plan = _plan(items, target, tmp_path, floor="medium")
    assert plan["tickets"][0]["debt_ids"] == ["DEBT-hi00000001"]
    assert [e["reason"] for e in plan["excluded"]] == ["below_severity_floor"]


def test_cluster_cap_trims_overflow_keeping_most_severe(target, tmp_path):
    items = [
        _item("DEBT-hi00000001", severity="high", locations=["p/a.py:1"]),
        _item("DEBT-md00000001", severity="medium", locations=["p/b.py:1"]),
        _item("DEBT-lo00000001", severity="low", locations=["p/c.py:1"]),
    ]
    plan = _plan(items, target, tmp_path, cap=2)
    assert sorted(plan["tickets"][0]["debt_ids"]) == ["DEBT-hi00000001", "DEBT-md00000001"]
    assert plan["excluded"] == [{"id": "DEBT-lo00000001", "engine": "markers",
                                 "severity": "low", "reason": "over_cluster_cap"}]


def test_cli_refuses_without_any_budget(target, tmp_path):
    _debt([_item("DEBT-aaaa000001")], tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "plan_from_debt.py"), "plan",
         "--repo", str(target), "--debt", str(tmp_path / "debt.json")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "unbudgeted remediation epic is unbounded scope" in proc.stderr


# --- boundary findings --------------------------------------------------------


def test_out_of_scope_items_become_boundary_referrals_never_tickets(target, tmp_path):
    (target / "docs").mkdir(exist_ok=True)
    (target / "docs" / "scope.json").write_text(json.dumps({"include": ["ours/*"]}))
    items = [
        _item("DEBT-ours000001", locations=["ours/a.py:1"]),
        _item("DEBT-theirs0001", severity="high", locations=["theirs/b.py:1"]),
    ]
    plan = _plan(items, target, tmp_path, count=10)
    all_ticketed = [i for t in plan["tickets"] for i in t["debt_ids"]]
    assert all_ticketed == ["DEBT-ours000001"]
    assert [r["id"] for r in plan["boundary_referrals"]] == ["DEBT-theirs0001"]
    body = plan["boundary_referrals"][0]["issue_body"]
    assert "owning team" in body
    assert "DEBT-theirs0001" in body
    assert "theirs/b.py:1" in body
    assert "never ticketed" in body and "never auto-fixed" in body
    # boundary items are never in the excluded list either — they're referrals
    assert all(e["id"] != "DEBT-theirs0001" for e in plan["excluded"])


def test_marked_boundary_item_is_referred_even_in_scope(target, tmp_path):
    items = [_item("DEBT-mark000001", boundary=True)]
    plan = _plan(items, target, tmp_path, count=10)
    assert plan["tickets"] == []
    assert [r["id"] for r in plan["boundary_referrals"]] == ["DEBT-mark000001"]


# --- grandfather handling -----------------------------------------------------


def test_grandfathered_items_are_valid_input_and_marked(target, tmp_path):
    items = [
        _item("DEBT-gf00000001", grandfathered=True, grandfather_expiry="2999-01-01"),
        _item("DEBT-new0000001", locations=["pkg/n.py:1"]),
    ]
    plan = _plan(items, target, tmp_path, count=10)
    ticketed = {i for t in plan["tickets"] for i in t["debt_ids"]}
    assert "DEBT-gf00000001" in ticketed
    gf = [g for t in plan["tickets"] for g in t["grandfathered_ids"]]
    assert gf == ["DEBT-gf00000001"]
    assert plan["counts"]["grandfathered_ticketed"] == 1


# --- pathset derivation -------------------------------------------------------


def test_ticket_pathset_is_location_files_and_ratchet_loads_it(target, tmp_path):
    items = [
        _item("DEBT-aaaa000001", locations=["pkg/a.py:3", "pkg/b.py:7"]),
    ]
    plan = _plan(items, target, tmp_path, count=1)
    ticket = plan["tickets"][0]
    assert ticket["pathset"]["paths"] == ["pkg/a.py", "pkg/b.py"]
    assert ticket["collateral"] == []

    spec = plan_from_debt.derive_pathset(ticket, ["tests/test_a.py"])
    assert spec["paths"] == ["pkg/a.py", "pkg/b.py", "tests/test_a.py"]
    assert spec["source"].startswith("plan_from_debt RT-001")

    # the emitted shape must load through ratchet's pathset consumer
    ps = tmp_path / "pathset.json"
    ps.write_text(json.dumps(spec))
    loaded = ratchet.load_pathset(ps)
    assert ratchet.pathset_outside(loaded, ["pkg/a.py", "elsewhere/x.py"]) == ["elsewhere/x.py"]


def test_pathset_subcommand_emits_ratchet_shape(target, tmp_path):
    items = [_item("DEBT-aaaa000001", locations=["pkg/a.py:3"])]
    debt_file = _debt(items, tmp_path)
    out_dir = tmp_path / "plan-out"
    subprocess.run(
        [sys.executable, str(SCRIPTS / "plan_from_debt.py"), "plan",
         "--repo", str(target), "--debt", str(debt_file),
         "--budget-count", "1", "--out", str(out_dir)],
        capture_output=True, text=True, check=True,
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "plan_from_debt.py"), "pathset",
         "--plan", str(out_dir / "remediation-plan.json"), "--id", "RT-001",
         "--collateral", "tests/test_pkg.py"],
        capture_output=True, text=True, check=True,
    )
    spec = json.loads(proc.stdout)
    assert spec == {"paths": ["pkg/a.py", "tests/test_pkg.py"],
                    "source": spec["source"]}
    assert ratchet.pathset_outside(spec, ["pkg/a.py"]) == []


# --- dependency ordering ------------------------------------------------------


def test_tickets_ordered_by_severity_and_overlap_serialized(target, tmp_path):
    # A clone-class ticket never merges (precedence (a) is final), so sharing
    # a file with a module ticket produces a DEPENDENCY, not a merge.
    items = [
        _item("DEBT-lo00000001", severity="low", locations=["pkg/a.py:1"]),
        _item("DEBT-hi00000001", engine="clones", severity="high",
              locations=["pkg/a.py:10", "other/z.py:1"], symbol="cafecafe"),
    ]
    plan = _plan(items, target, tmp_path, count=10)
    assert plan["tickets"][0]["debt_ids"] == ["DEBT-hi00000001"]  # severity first
    # second ticket's pathset overlaps the first's -> serialized after it
    assert plan["tickets"][1]["depends_on"] == ["RT-001"]


def test_disjoint_tickets_have_no_dependencies(target, tmp_path):
    items = [
        _item("DEBT-aaaa000001", locations=["pkg/a.py:1"]),
        _item("DEBT-bbbb000001", locations=["other/b.py:1"]),
    ]
    plan = _plan(items, target, tmp_path, count=10)
    assert all(t["depends_on"] == [] for t in plan["tickets"])


def test_plan_is_deterministic(target, tmp_path):
    items = [
        _item("DEBT-aaaa000001", locations=["pkg/a.py:1"]),
        _item("DEBT-bbbb000001", severity="high", locations=["other/b.py:1"]),
        _item("DEBT-cccc000001", engine="clones", severity="high",
              locations=["x/a.py:1", "y/b.py:2"], symbol="beefbeef"),
    ]
    p1 = _plan(items, target, tmp_path, count=10)
    p2 = _plan(list(reversed(items)), target, tmp_path, count=10)
    strip = ("generated_at", "debt_sha256")
    a = {k: v for k, v in p1.items() if k not in strip}
    b = {k: v for k, v in p2.items() if k not in strip}
    assert a == b


# --- plan CLI artifacts -------------------------------------------------------


def test_plan_cli_writes_json_and_markdown(target, tmp_path):
    items = [_item("DEBT-aaaa000001", locations=["pkg/a.py:1"]),
             _item("DEBT-bbbb000001", severity="low", locations=["cold/b.py:1"])]
    debt_file = _debt(items, tmp_path)
    out_dir = tmp_path / "plan-out"
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "plan_from_debt.py"), "plan",
         "--repo", str(target), "--debt", str(debt_file),
         "--budget-count", "1", "--out", str(out_dir)],
        capture_output=True, text=True, check=True,
    )
    plan = json.loads((out_dir / "remediation-plan.json").read_text())
    assert plan["schema"] == "remediation-plan/1"
    assert plan["target_sha"], "plan is stamped with the target HEAD"
    assert plan["budget"] == {"count": 1, "severity_floor": None, "cluster_cap": None}
    md = (out_dir / "remediation-plan.md").read_text()
    assert "RT-001" in md and "Excluded" in md
    assert "normal end state" in proc.stdout


# --- verify (the /close-epic acceptance test) ---------------------------------


def _mini_plan(ticketed: list[str], grandfathered: list[str] | None = None) -> dict:
    return {"schema": "remediation-plan/1",
            "tickets": [{"id": "RT-001", "debt_ids": ticketed,
                         "grandfathered_ids": grandfathered or []}]}


def test_verify_clean_when_ticketed_ids_gone():
    plan = _mini_plan(["DEBT-aaaa000001"])
    fresh = {"items": [{"id": "DEBT-other00001", "severity": "low"}]}
    result = plan_from_debt.verify_plan(plan, fresh)
    assert result["ok"] and result["resolved"] == ["DEBT-aaaa000001"]


def test_verify_fails_listing_surviving_ticketed_ids():
    plan = _mini_plan(["DEBT-aaaa000001", "DEBT-bbbb000001"])
    fresh = {"items": [{"id": "DEBT-aaaa000001", "severity": "high", "detail": "still here"}]}
    result = plan_from_debt.verify_plan(plan, fresh)
    assert not result["ok"]
    assert [u["id"] for u in result["unresolved"]] == ["DEBT-aaaa000001"]
    assert result["resolved"] == ["DEBT-bbbb000001"]


def test_verify_only_checks_ticketed_ids_leftovers_are_normal():
    plan = _mini_plan(["DEBT-aaaa000001"])
    fresh = {"items": [{"id": "DEBT-leftover01", "severity": "low"}]}
    assert plan_from_debt.verify_plan(plan, fresh)["ok"]


def test_verify_post_plan_grandfather_is_explicit_waiver():
    plan = _mini_plan(["DEBT-aaaa000001"])
    fresh = {"items": [{"id": "DEBT-aaaa000001", "grandfathered": True,
                        "grandfather_expiry": "2999-01-01"}]}
    result = plan_from_debt.verify_plan(plan, fresh)
    assert result["ok"]
    assert [w["id"] for w in result["waived"]] == ["DEBT-aaaa000001"]


def test_verify_pre_plan_grandfather_is_not_a_waiver():
    # ticketed WHILE grandfathered: remediating it was the point — survival is
    # unresolved, not waived.
    plan = _mini_plan(["DEBT-aaaa000001"], grandfathered=["DEBT-aaaa000001"])
    fresh = {"items": [{"id": "DEBT-aaaa000001", "grandfathered": True,
                        "grandfather_expiry": "2999-01-01"}]}
    result = plan_from_debt.verify_plan(plan, fresh)
    assert not result["ok"]


def test_verify_expired_grandfather_does_not_waive():
    plan = _mini_plan(["DEBT-aaaa000001"])
    fresh = {"items": [{"id": "DEBT-aaaa000001", "grandfathered": True,
                        "grandfather_expiry": "2000-01-01"}]}
    assert not plan_from_debt.verify_plan(plan, fresh)["ok"]


def test_verify_cli_exit_codes(tmp_path):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(_mini_plan(["DEBT-aaaa000001"])))
    fresh_dirty = tmp_path / "fresh-dirty.json"
    fresh_dirty.write_text(json.dumps(
        {"schema": "debt/1", "items": [{"id": "DEBT-aaaa000001", "severity": "low"}]}))
    fresh_clean = tmp_path / "fresh-clean.json"
    fresh_clean.write_text(json.dumps({"schema": "debt/1", "items": []}))

    dirty = subprocess.run(
        [sys.executable, str(SCRIPTS / "plan_from_debt.py"), "verify",
         "--plan", str(plan_file), "--debt", str(fresh_dirty)],
        capture_output=True, text=True,
    )
    assert dirty.returncode == 1
    assert "UNRESOLVED DEBT-aaaa000001" in dirty.stdout
    clean = subprocess.run(
        [sys.executable, str(SCRIPTS / "plan_from_debt.py"), "verify",
         "--plan", str(plan_file), "--debt", str(fresh_clean)],
        capture_output=True, text=True,
    )
    assert clean.returncode == 0
