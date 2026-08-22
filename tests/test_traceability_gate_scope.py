"""Ticket-scoped soundness blocking (chief-wiggum#379).

## The problem

`@cw-trace` direction errors (`guards` on a test, `verifies` on production code)
are real soundness violations, but `--gate soundness` was not part of the
per-ticket floor. Workers introduced them freely and they surfaced three merges
later, at wave-merge. Two separate `fix(trace): correct @cw-trace direction`
commits in one epic are the evidence.

## Why the obvious fix was wrong

Simply adding `--gate soundness --changed-since <base>` to the worker's floor
blocks the worker on findings it is FORBIDDEN to fix.

`--changed-since` scopes only the SOURCE scan. The epic docs are always read in
full, so `malformed_ids`, `unparsed_artifacts` and `orphan_business_rules` are
whole-epic properties that fire regardless of what the worker touched — and the
ratchet protects goalposts, so a worker may not edit `contracts.md` or
`invariants.md` to clear them. A gate that blocks on an unfixable defect is the
noisy gate `docs/gate-rollout.md` warns about: the operator learns to `--force`,
and every gate loses authority.

Measured on a real corpus before writing this: a worker whose diff was a single
unrelated helper function was blocked, exit 1, by a malformed ID in
`invariants.md` that predated its branch.

## The fix

Findings carry an `origin` (`source` / `epic` / `external`). `--gate-scope
changed` blocks only on `source`-origin findings, which under `--changed-since`
are exactly the annotations in the diff under review. Everything else still
prints, and still blocks in `/architect` and `/close-epic`, where the actor CAN
fix it. Same in-domain vs boundary split `docs/sidecar.md` already applies.

This is a NARROWING of an already-validated gate, never a way to make a finding
disappear.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import check_traceability as ct
import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "check_traceability.py"
CORPUS = ROOT / "tests" / "fixtures" / "gate_validation" / "traceability_clean"

SCHEMA = ct.load_schema()


def _git(repo: Path, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """The clean corpus as a git repo, with `main` as the worker's base."""
    r = tmp_path / "repo"
    shutil.copytree(CORPUS, r)
    _git(r, "init", "--initial-branch=main")  # pinned: Apple git defaults main, Linux master
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "baseline")
    return r


def _run(repo: Path, *extra: str):
    cmd = [sys.executable, str(SCRIPT), str(repo / "epic"), "--source", str(repo / "src"),
           "--format", "json", *extra]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _worker_branch(repo: Path):
    _git(repo, "checkout", "-b", "worker")


def _commit(repo: Path, msg="worker change"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


def _break_epic_docs(repo: Path):
    """A defect in the GOALPOSTS — the worker may not edit these."""
    inv = repo / "epic" / "invariants.md"
    inv.write_text(inv.read_text() + "\n- **BR-999**: malformed id, not KIND-SLUG-NNN\n")
    _commit(repo, "pre-existing epic defect (NOT the worker)")


class TestTruePositive:
    """The class #379 exists to catch must still block."""

    def test_a_direction_error_in_the_workers_diff_blocks_under_changed_scope(self, repo):
        _worker_branch(repo)
        (repo / "src" / "test_new.py").write_text(
            "# @cw-trace guards CTR-order-001\ndef test_x():\n    ...\n")
        _commit(repo)
        rc, out, _ = _run(repo, "--gate", "soundness", "--changed-since", "main",
                          "--gate-scope", "changed")
        assert rc == 1
        reasons = [f["reason"] for f in json.loads(out)["invalid_links"]]
        assert "guards cannot originate from test" in reasons

    def test_the_same_error_blocks_under_the_default_scope_too(self, repo):
        """Scoping must not be a way to escape a finding you do own."""
        _worker_branch(repo)
        (repo / "src" / "test_new.py").write_text(
            "# @cw-trace guards CTR-order-001\ndef test_x():\n    ...\n")
        _commit(repo)
        rc, _, _ = _run(repo, "--gate", "soundness", "--changed-since", "main")
        assert rc == 1

    def test_verifies_from_production_code_also_blocks(self, repo):
        """The other half of the direction class named in the ticket."""
        _worker_branch(repo)
        (repo / "src" / "handler.py").write_text(
            "# @cw-trace verifies CTR-order-001\ndef handle():\n    ...\n")
        _commit(repo)
        rc, out, _ = _run(repo, "--gate", "soundness", "--changed-since", "main",
                          "--gate-scope", "changed")
        assert rc == 1
        assert any("verifies cannot originate from code" in f["reason"]
                   for f in json.loads(out)["invalid_links"])

    def test_a_dangling_annotation_in_the_diff_blocks(self, repo):
        _worker_branch(repo)
        (repo / "src" / "handler.py").write_text(
            "# @cw-trace guards CTR-nope-999\ndef handle():\n    ...\n")
        _commit(repo)
        rc, out, _ = _run(repo, "--gate", "soundness", "--changed-since", "main",
                          "--gate-scope", "changed")
        assert rc == 1
        assert json.loads(out)["dangling"]


class TestNoFalsePositive:
    """The measured failure mode that made the naive fix wrong."""

    def test_a_clean_worker_is_not_blocked_by_a_pre_existing_epic_defect(self, repo):
        _break_epic_docs(repo)
        _worker_branch(repo)
        (repo / "src" / "unrelated.py").write_text("def helper():\n    return 1\n")
        _commit(repo)
        rc, _, _ = _run(repo, "--gate", "soundness", "--changed-since", "main",
                        "--gate-scope", "changed")
        assert rc == 0, "a worker was blocked by a goalpost defect it may not edit"

    def test_the_default_scope_still_blocks_on_that_same_defect(self, repo):
        """The finding is not forgiven — /architect and /close-epic still stop
        on it. Only the WORKER, who cannot fix it, is spared."""
        _break_epic_docs(repo)
        _worker_branch(repo)
        (repo / "src" / "unrelated.py").write_text("def helper():\n    return 1\n")
        _commit(repo)
        rc, _, _ = _run(repo, "--gate", "soundness", "--changed-since", "main")
        assert rc == 1

    def test_the_unblocked_finding_is_still_reported(self, repo):
        """Scoped OUT is not scoped AWAY. If exit 0 were the whole story this
        would be a fail-open; the finding must remain visible."""
        _break_epic_docs(repo)
        _worker_branch(repo)
        (repo / "src" / "unrelated.py").write_text("def helper():\n    return 1\n")
        _commit(repo)
        rc, out, _ = _run(repo, "--gate", "soundness", "--changed-since", "main",
                          "--gate-scope", "changed")
        report = json.loads(out)
        assert rc == 0
        assert report["malformed_ids"], "the epic-doc defect vanished from the report"
        assert report["soundness_ok"] is False, "soundness_ok must still tell the truth"

    def test_text_output_explains_why_it_did_not_block(self, repo):
        """`Soundness: FINDINGS` beside exit 0 reads exactly like a fail-open
        unless the output says which findings could block."""
        _break_epic_docs(repo)
        _worker_branch(repo)
        (repo / "src" / "unrelated.py").write_text("def helper():\n    return 1\n")
        _commit(repo)
        cmd = [sys.executable, str(SCRIPT), str(repo / "epic"), "--source", str(repo / "src"),
               "--gate", "soundness", "--changed-since", "main", "--gate-scope", "changed"]
        p = subprocess.run(cmd, capture_output=True, text=True)
        assert p.returncode == 0
        assert "Gate scope: **changed**" in p.stdout
        assert "originate in the scanned diff and can block" in p.stdout


class TestUsageGuards:
    """The flag must not become a way to quietly weaken an authoritative run."""

    def test_changed_scope_without_changed_since_is_a_usage_error(self, repo):
        """Without --changed-since the source scan is the WHOLE repo, so
        'originates in source' stops meaning 'in this diff' and the flag would
        drop epic findings from an authoritative gate."""
        rc, _, err = _run(repo, "--gate", "soundness", "--gate-scope", "changed")
        assert rc == 2
        assert "requires --changed-since" in err

    def test_changed_scope_with_coverage_gate_is_a_usage_error(self, repo):
        """Coverage over a partial scan is meaningless — every untouched
        contract looks uncovered. Refuse rather than imply a scoped coverage
        gate exists."""
        rc, _, err = _run(repo, "--gate", "coverage", "--changed-since", "main",
                          "--gate-scope", "changed")
        assert rc == 2
        assert "soundness only" in err

    def test_the_default_scope_is_all(self, repo):
        """Every existing caller keeps its behaviour without naming the flag."""
        _break_epic_docs(repo)
        rc, _, _ = _run(repo, "--gate", "soundness")
        assert rc == 1


class TestOriginTagging:
    def test_epic_annotations_are_tagged_epic(self):
        report = ct.build_report(
            {"CTR-x-001": "CTR"},
            [ct.Annotation("guards", "CTR-x-001", "contracts.md", 1, "test")],
            SCHEMA,
        )
        assert report.invalid_links[0]["origin"] == ct.ORIGIN_EPIC

    def test_source_annotations_are_tagged_source(self):
        ann = ct.Annotation("guards", "CTR-x-001", "a.py", 1, "test")
        report = ct.build_report(
            {"CTR-x-001": "CTR"}, [ann], SCHEMA, source_annotations={id(ann)},
        )
        assert report.invalid_links[0]["origin"] == ct.ORIGIN_SOURCE

    def test_external_annotations_are_tagged_external_not_source(self):
        """External links are re-anchored against the WHOLE tree and are never
        filtered by --changed-since, so they are not diff-local. Tagging them
        `source` would block a worker for a link it did not touch."""
        ann = ct.Annotation("guards", "CTR-x-001", "hook.lua", 1, "test")
        report = ct.build_report(
            {"CTR-x-001": "CTR"}, [ann], SCHEMA, external_annotations={id(ann)},
        )
        assert report.invalid_links[0]["origin"] == ct.ORIGIN_EXTERNAL

    def test_an_external_finding_does_not_block_under_changed_scope(self):
        ann = ct.Annotation("guards", "CTR-x-001", "hook.lua", 1, "test")
        report = ct.build_report(
            {"CTR-x-001": "CTR"}, [ann], SCHEMA, external_annotations={id(ann)},
        )
        assert report.soundness_ok is False
        assert report.soundness_ok_for_scope(ct.GATE_SCOPE_CHANGED) is True

    def test_scope_all_is_identical_to_soundness_ok(self):
        """The narrowing must be a strict subset — `all` may not drift."""
        ann = ct.Annotation("guards", "CTR-x-001", "a.py", 1, "test")
        report = ct.build_report(
            {"CTR-x-001": "CTR", "BR-x-001": "BR"}, [ann], SCHEMA,
            source_annotations={id(ann)},
        )
        assert report.soundness_ok_for_scope(ct.GATE_SCOPE_ALL) == report.soundness_ok
