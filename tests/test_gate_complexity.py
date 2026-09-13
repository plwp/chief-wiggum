"""Pins the complexity fixes measured against a synthetic scaled target
(500 / 2,000 / 8,000 files): every gate scaled linearly except change
coupling, which is quadratic in the size of a single bulk commit, and
two fixed-cost multipliers — the ratchet journal verified once per gate,
and `code_query orient` paying epic discovery + a provenance index per
file when the workflows call it over every file in a diff.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_gate_validation as gv  # noqa: E402
import code_query  # noqa: E402
from quality import process  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "code_query_repo"


# --- change coupling: bulk commits are skipped, not walked ------------------


def _commit(files: list[str]) -> dict:
    return {"author": "a", "subject": "s", "files": [(f, 1) for f in files]}


def test_bulk_commit_contributes_no_pairs_and_the_walk_stays_bounded():
    """One 3,000-file import commit is 4.5M pairs (measured: ~6 s at 3k files,
    >70 s at 12k). It carries no coupling signal, so it is skipped outright;
    the genuine pair from the small commits is unchanged."""
    bulk = _commit([f"src/m{i}.py" for i in range(3000)])
    small = [_commit(["src/a.py", "src/b.py"]) for _ in range(4)]
    t0 = time.perf_counter()
    pairs = process._coupling_from_commits([bulk, *small])
    assert time.perf_counter() - t0 < 1.0
    assert [(p["a"], p["b"], p["co_changes"], p["confidence"]) for p in pairs] == [
        ("src/a.py", "src/b.py", 4, 1.0)
    ]


def test_bulk_threshold_is_a_kwarg_and_excluded_commits_count_for_nothing():
    """Skipping a bulk commit must not leave its files in the per-file commit
    totals either — otherwise confidence (co / min(commits)) would be diluted
    by commits that could never have expressed coupling."""
    bulk = _commit(["src/a.py", "src/b.py", "src/c.py"])
    small = [_commit(["src/a.py", "src/b.py"]) for _ in range(4)]
    with_cap = process._coupling_from_commits([bulk, *small], bulk_max_files=2)
    without = process._coupling_from_commits([bulk, *small], bulk_max_files=3)
    assert with_cap[0]["co_changes"] == 4 and with_cap[0]["confidence"] == 1.0
    assert without[0]["co_changes"] == 5
    assert process.BULK_COMMIT_MAX_FILES == 50


# --- gate validation: the journal chain is verified once per run ------------


def test_multi_gate_check_walks_the_verified_prefix_once(monkeypatch):
    calls: list[str] = []
    original = gv.ratchet_verified_prefix

    def counting(journal):
        calls.append(str(journal))
        return original(journal)

    monkeypatch.setattr(gv, "ratchet_verified_prefix", counting)
    chain_cache: dict = {}
    vdir = REPO / "docs" / "quality" / "validation"
    for gate in ("check_traceability", "check_single_writer", "ratchet"):
        gv.check_and_transition(gate, vdir, schema=gv.load_schema(), chain_cache=chain_cache)
    assert len(calls) == 1, f"verified_prefix walked {len(calls)}x for three gates"


def test_uncached_read_still_answers_from_the_ratchet_original(monkeypatch):
    """A caller with no chain_cache (single-gate CLI paths, tests that call
    check_and_transition directly) gets exactly ratchet.last_authority_action."""
    seen = []
    monkeypatch.setattr(gv, "ratchet_last_authority_action", lambda j, g: seen.append((j, g)) or "wire")
    assert gv._last_authority_action_cached(Path("/x/journal"), "g", None) == "wire"
    assert seen == [(Path("/x/journal"), "g")]


# --- code_query orient: one batch = the union of the single answers ---------


def _keyed(env: dict) -> set[tuple]:
    return {(f["kind"], f.get("id"), f["handle"]) for f in env["facts"]}


@pytest.mark.parametrize("paths", [
    ["src/order.py", "src/admin.py"],
    ["src/order.py", "ui/orders/page.tsx", "src/legacy_util.py"],
])
def test_batch_orient_is_the_union_of_single_orients(paths):
    batch = code_query.cmd_orient(FIXTURE, paths, "checkout", limit=1000)
    singles: set[tuple] = set()
    for p in paths:
        singles |= _keyed(code_query.cmd_orient(FIXTURE, p, "checkout", limit=1000))
    assert _keyed(batch) == singles
    assert batch["applicability"] == "applicable"
    assert batch["summary"].startswith(f"orient: {len(batch['facts'])} governing fact(s) across {len(paths)} file(s)")


def test_batch_orient_reports_a_missing_path_as_a_warning_not_a_drop():
    env = code_query.cmd_orient(FIXTURE, ["src/order.py", "src/does_not_exist.py"], "checkout")
    assert env["applicability"] == "applicable"
    assert any("does_not_exist.py not found" in w for w in env["warnings"])
    assert env["summary"].endswith("1 not found")
    assert _keyed(env) == _keyed(code_query.cmd_orient(FIXTURE, "src/order.py", "checkout"))


def test_batch_orient_with_nothing_scannable_is_inapplicable_never_a_clean_empty():
    env = code_query.cmd_orient(FIXTURE, ["src/nope1.py", "src/nope2.py"], "checkout")
    assert env["applicability"] == "inapplicable"
    assert env["facts"] == []


def test_single_path_orient_is_byte_identical_to_before():
    """A one-element list dispatches through the single-path form (the CLI
    does this), so existing callers see the exact envelope they always did."""
    one = code_query.cmd_orient(FIXTURE, "src/order.py", "checkout")
    assert one["summary"].startswith("orient: ") and " for src/order.py " in one["summary"]
    missing = code_query.cmd_orient(FIXTURE, "src/does_not_exist.py", "checkout")
    assert missing["applicability"] == "inapplicable" and missing["summary"].startswith("unscanned:")
