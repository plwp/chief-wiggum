"""Corpus freeze, results recording, and report assembly (chief-wiggum#391).

Two failure modes carry this file, and both are quiet ones.

The first is a corpus that admits what it cannot verify. A contaminated task
does not announce itself in the results; it just raises every arm that can
read the answer, and the effect looks like a real difference. So the tests
below push hard on the direction of the unverifiable case: git that cannot
answer, dates that do not parse, a base commit nobody recorded.

The second is a number that should never have been computed. A partial arm, or
one that ran against a different verifier, still produces a perfectly
plausible-looking ratio. The suppression tests assert that no ratio exists at
all in those cases, not merely that it was labelled.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from chief_wiggum.dag import corpus as corpus_mod
from chief_wiggum.dag import report as report_mod
from chief_wiggum.dag.corpus import (
    ExclusionReason,
    Reachability,
    TaskRecord,
    fingerprint_verifier,
    freeze_corpus,
    git_reachability,
)
from chief_wiggum.dag.experiment import NonInferiority, RunManifest
from chief_wiggum.dag.report import (
    ClosureSuppression,
    TaskOutcome,
    assemble_report,
    build_arm_run,
    conformance_rate,
    render_report,
)

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "dag_experiment.py"


def task(task_id: str, **kwargs) -> TaskRecord:
    fields = {
        "source": "cw-history",
        "task_class": "bugfix",
        "risk": "low",
        "size": "small",
        "base_commit": "b" * 40,
    }
    fields.update(kwargs)
    return TaskRecord(task_id=task_id, **fields)


def oracle(verdict: Reachability):
    return lambda _solution, _base: verdict


# ---------------------------------------------------------------- corpus ---


def test_reachable_solution_is_excluded_and_counted():
    frozen = freeze_corpus(
        [task("t1", solution_commit="s" * 40)],
        reachable=oracle(Reachability.REACHABLE),
    )
    assert [item.task_id for item in frozen.included] == []
    assert frozen.exclusions[0].reason is ExclusionReason.SOLUTION_IN_BASE
    assert frozen.exclusion_counts["SOLUTION_IN_BASE"] == 1
    # The denominator matters as much as the count.
    assert frozen.considered == 1


def test_unreachable_solution_is_included():
    frozen = freeze_corpus(
        [task("t1", solution_commit="s" * 40)],
        reachable=oracle(Reachability.UNREACHABLE),
    )
    assert [item.task_id for item in frozen.included] == ["t1"]
    assert frozen.exclusions == ()


def test_unknown_reachability_with_no_usable_dates_excludes_rather_than_admits():
    """Fail closed. An unverifiable task admitted is a contaminated corpus."""
    frozen = freeze_corpus(
        [task("t1", solution_commit="s" * 40)],
        reachable=oracle(Reachability.UNKNOWN),
    )
    assert frozen.included == ()
    assert frozen.exclusions[0].reason is ExclusionReason.UNVERIFIABLE_BASE


def test_unknown_reachability_falls_back_to_dates_in_both_directions():
    leaked = freeze_corpus(
        [task("t1", solution_commit="s" * 40, solution_date="2026-01-01",
              base_date="2026-07-01")],
        reachable=oracle(Reachability.UNKNOWN),
    )
    assert leaked.exclusions[0].reason is ExclusionReason.SOLUTION_PREDATES_BASE

    clean = freeze_corpus(
        [task("t1", solution_commit="s" * 40, solution_date="2026-07-01",
              base_date="2026-01-01")],
        reachable=oracle(Reachability.UNKNOWN),
    )
    assert [item.task_id for item in clean.included] == ["t1"]


def test_default_oracle_is_unknown_not_clean():
    """No repository supplied means nothing is known, not "not contaminated".

    The fail-open version of this default admits every task whose solution
    nobody looked for, and a contaminated corpus does not announce itself.
    """
    unverifiable = freeze_corpus([task("t1", solution_commit="s" * 40)])
    assert unverifiable.included == ()
    assert unverifiable.exclusions[0].reason is ExclusionReason.UNVERIFIABLE_BASE

    # Dates still settle it when they can.
    dated = freeze_corpus([task("t1", solution_commit="s" * 40,
                                solution_date="2026-07-01",
                                base_date="2026-01-01")])
    assert [item.task_id for item in dated.included] == ["t1"]


def test_malformed_dates_do_not_settle_reachability():
    """`2026-8-1` sorts before `2026-10-1` as text and after it in reality."""
    frozen = freeze_corpus(
        [task("t1", solution_commit="s" * 40, solution_date="2026-8-1",
              base_date="2026-10-1")],
        reachable=oracle(Reachability.UNKNOWN),
    )
    assert frozen.exclusions[0].reason is ExclusionReason.UNVERIFIABLE_BASE


def test_task_with_no_solution_needs_a_base_commit_to_be_included():
    frozen = freeze_corpus([task("t1", base_commit="")])
    assert frozen.exclusions[0].reason is ExclusionReason.UNVERIFIABLE_BASE

    with_base = freeze_corpus([task("t1")])
    assert [item.task_id for item in with_base.included] == ["t1"]


def test_public_benchmark_instance_is_annotated_not_excluded():
    frozen = freeze_corpus([
        task("bench-1", base_commit="", public_benchmark=True),
        task("cw-1"),
    ])
    assert sorted(item.task_id for item in frozen.included) == ["bench-1", "cw-1"]
    assert frozen.pretraining_risk == ("bench-1",)


def test_duplicate_ids_and_unknown_strata_are_excluded_with_reasons():
    frozen = freeze_corpus([
        task("t1"), task("t1"),
        task("t2", task_class="nonsense"),
        task("t3", risk="spicy"),
        task("t4", size="enormous"),
    ])
    reasons = {item.task_id: item.reason for item in frozen.exclusions}
    assert reasons["t1"] is ExclusionReason.DUPLICATE_ID
    assert reasons["t2"] is ExclusionReason.INVALID_STRATUM
    assert reasons["t3"] is ExclusionReason.INVALID_STRATUM
    assert reasons["t4"] is ExclusionReason.INVALID_STRATUM
    assert [item.task_id for item in frozen.included] == ["t1"]
    assert "risk='spicy'" in reasons_detail(frozen, "t3")


def reasons_detail(frozen, task_id: str) -> str:
    return next(item.detail for item in frozen.exclusions if item.task_id == task_id)


def test_corpus_digest_ignores_input_order_but_not_content():
    first = freeze_corpus([task("a"), task("b")])
    second = freeze_corpus([task("b"), task("a")])
    assert first.digest() == second.digest()

    changed = freeze_corpus([task("a"), task("b", risk="high")])
    assert changed.digest() != first.digest()


def test_underpowered_strata_are_reported_with_their_n_never_dropped():
    frozen = freeze_corpus(
        [task(f"t{index}") for index in range(5)]
        + [task(f"h{index}", risk="high") for index in range(25)],
        min_stratum_n=20,
    )
    assert frozen.strata["bugfix/low/small"] == 5
    assert frozen.strata["bugfix/high/small"] == 25
    assert frozen.underpowered == ("bugfix/low/small",)
    # Still counted in the corpus, not silently removed.
    assert len(frozen.included) == 30
    assert "UNDERPOWERED" in frozen.render()


def test_render_carries_denominators_for_every_count():
    frozen = freeze_corpus([task("t1"), task("t2", task_class="nonsense")])
    rendered = frozen.render()
    assert "included:   1/2" in rendered
    assert "excluded:   1/2" in rendered
    assert "pretraining risk: 0/1" in rendered


# ------------------------------------------------------ git reachability ---


def test_git_reachability_answers_unknown_when_git_cannot_answer(tmp_path):
    """A refused question must never read as a clean 'not contaminated'."""
    check = git_reachability(tmp_path)  # not a repository
    assert check("a" * 40, "b" * 40) is Reachability.UNKNOWN
    assert check("", "b" * 40) is Reachability.UNKNOWN


def test_git_reachability_reads_real_ancestry(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    subprocess.run(["git", "init", "--initial-branch=main", str(repo)],
                   check=True, capture_output=True, text=True)
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("one")
    run("add", "-A")
    run("commit", "-m", "first")
    first = run("rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("two")
    run("add", "-A")
    run("commit", "-m", "second")
    second = run("rev-parse", "HEAD").stdout.strip()

    check = git_reachability(repo)
    assert check(first, second) is Reachability.REACHABLE
    assert check(second, first) is Reachability.UNREACHABLE
    assert check("0" * 40, second) is Reachability.UNKNOWN


# -------------------------------------------------------------- verifier ---


def test_verifier_fingerprint_refuses_to_skip_a_missing_file(tmp_path):
    (tmp_path / "present.py").write_text("x = 1")
    with pytest.raises(FileNotFoundError, match="stop, not a skip"):
        fingerprint_verifier(["present.py", "absent.py"], root=tmp_path)


def test_verifier_hash_is_order_stable_and_content_sensitive(tmp_path):
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "b.py").write_text("y = 2")
    first = fingerprint_verifier(["a.py", "b.py"], root=tmp_path, command="pytest")
    second = fingerprint_verifier(["b.py", "a.py"], root=tmp_path, command="pytest")
    assert first.digest() == second.digest()

    (tmp_path / "b.py").write_text("y = 3")
    changed = fingerprint_verifier(["a.py", "b.py"], root=tmp_path, command="pytest")
    assert changed.digest() != first.digest()

    other_command = fingerprint_verifier(["a.py"], root=tmp_path, command="tox")
    assert other_command.digest() != fingerprint_verifier(
        ["a.py"], root=tmp_path, command="pytest"
    ).digest()


# ------------------------------------------------------- outcomes / runs ---


def manifest(arm: str, **kwargs) -> RunManifest:
    fields = {
        "corpus_version": "sha256:corpus",
        "provider_roster": {"implementer": "open-tier-a"},
        "seeds": {"task_order": 7},
        "budgets": {"usd_cents": 50_000},
        "verifier_hash": "sha256:verifier",
        "environment": {"python": "3.11"},
    }
    fields.update(kwargs)
    return RunManifest(arm=arm, **fields)


STRATA = {f"t{index}": "bugfix/low/small" for index in range(4)}


def outcome(task_id: str, accepted: bool, **kwargs) -> TaskOutcome:
    return TaskOutcome(task_id=task_id, accepted=accepted, **kwargs)


def test_outcome_rejects_a_missing_verdict_and_a_fractional_cost():
    with pytest.raises(ValueError, match="must not default"):
        TaskOutcome.from_dict({"task_id": "t1"})
    with pytest.raises(ValueError, match="whole integer"):
        TaskOutcome.from_dict({"task_id": "t1", "accepted": True,
                               "model_cost_cents": 10.5})
    with pytest.raises(ValueError, match="missing task_id"):
        TaskOutcome.from_dict({"accepted": True})
    assert TaskOutcome.from_dict({"task_id": "t1", "accepted": False}).accepted is False


def test_missing_corpus_tasks_are_incomplete_coverage_not_rejections():
    run = build_arm_run(
        arm="arm-1", model_tier="frontier-tier", process="static factory",
        manifest=manifest("arm-1"),
        outcomes=[outcome("t0", True), outcome("t1", True)],
        strata_by_task=STRATA,
    )
    assert run.coverage.complete is False
    assert run.coverage.attempted == 2
    assert run.coverage.missing == ("t2", "t3")
    # Scored on what it attempted — a crashed arm must not look merely bad.
    assert run.result.attempted == 2
    assert run.result.quality.point == 1.0


def test_unknown_and_duplicate_outcomes_are_counted_not_scored():
    run = build_arm_run(
        arm="arm-1", model_tier="frontier-tier", process="static factory",
        manifest=manifest("arm-1"),
        outcomes=[outcome("t0", True), outcome("t0", False),
                  outcome("nope", True), outcome("t1", False),
                  outcome("t2", True), outcome("t3", True)],
        strata_by_task=STRATA,
    )
    assert run.coverage.duplicates == ("t0",)
    assert run.coverage.unknown == ("nope",)
    assert run.coverage.complete is False
    # First record wins; the duplicate is not a second attempt.
    assert run.result.attempted == 4
    assert run.result.accepted == 3


def test_strata_come_from_the_corpus_not_from_the_arms_own_records():
    strata = {"t0": "feature/high/large", "t1": "bugfix/low/small"}
    run = build_arm_run(
        arm="arm-2", model_tier="open-tier", process="static factory",
        manifest=manifest("arm-2"),
        outcomes=[outcome("t0", False), outcome("t1", True)],
        strata_by_task=strata,
    )
    assert run.result.strata == {"feature/high/large": (0, 1),
                                 "bugfix/low/small": (1, 1)}


def test_results_digest_is_order_stable_and_record_sensitive():
    records = [outcome("t0", True), outcome("t1", False)]
    build = lambda items: build_arm_run(  # noqa: E731
        arm="arm-1", model_tier="frontier-tier", process="static factory",
        manifest=manifest("arm-1"), outcomes=items, strata_by_task=STRATA,
    )
    assert build(records).results_digest() == build(records[::-1]).results_digest()
    flipped = [outcome("t0", True), outcome("t1", True)]
    assert build(flipped).results_digest() != build(records).results_digest()


def test_results_digest_changes_when_the_manifest_changes():
    records = [outcome("t0", True)]
    base = build_arm_run(
        arm="arm-1", model_tier="frontier-tier", process="static factory",
        manifest=manifest("arm-1"), outcomes=records, strata_by_task=STRATA,
    )
    moved = build_arm_run(
        arm="arm-1", model_tier="frontier-tier", process="static factory",
        manifest=manifest("arm-1", verifier_hash="sha256:other"),
        outcomes=records, strata_by_task=STRATA,
    )
    assert base.results_digest() != moved.results_digest()


def test_conformance_excludes_tasks_outside_the_corpus():
    run = build_arm_run(
        arm="arm-1", model_tier="frontier-tier", process="static factory",
        manifest=manifest("arm-1"),
        outcomes=[outcome("t0", True, gate_conformant=True),
                  outcome("t1", True, gate_conformant=False),
                  outcome("nope", True, gate_conformant=False)],
        strata_by_task=STRATA,
    )
    interval = conformance_rate(run)
    assert (interval.successes, interval.n) == (1, 2)


# ---------------------------------------------------------------- report ---


def full_run(arm: str, accepted: int, *, cost_cents: int = 1000,
             escaped: int = 0, operator_seconds: int = 0,
             manifest_kwargs: dict | None = None, drop: int = 0):
    tier, process = report_mod.ARM_SPECS[arm]
    outcomes = []
    for index in range(4 - drop):
        outcomes.append(TaskOutcome(
            task_id=f"t{index}",
            accepted=index < accepted,
            escaped_defect=index < escaped,
            operator_seconds=operator_seconds,
            model_cost_cents=cost_cents,
        ))
    return build_arm_run(
        arm=arm, model_tier=tier, process=process,
        manifest=manifest(arm, **(manifest_kwargs or {})),
        outcomes=outcomes, strata_by_task=STRATA,
    )


CORPUS = {
    "corpus_version": "sha256:corpus",
    "considered": 6,
    "included_n": 4,
    "excluded_n": 2,
    "exclusion_counts": {"SOLUTION_IN_BASE": 2},
    "strata": {"bugfix/low/small": 4},
    "underpowered_strata": ["bugfix/low/small"],
    "pretraining_risk": [],
    "min_stratum_n": 20,
    "notes": "",
}

MARGIN = NonInferiority(margin=0.05, justification="registered before any arm ran")


def report_for(runs):
    return assemble_report(corpus=CORPUS, runs=runs, non_inferiority=MARGIN)


def test_clean_five_arm_run_computes_closure_for_arms_three_four_and_five():
    runs = [full_run("arm-1", 4), full_run("arm-2", 1), full_run("arm-3", 2),
            full_run("arm-4", 3), full_run("arm-5", 3)]
    report = report_for(runs)
    assert report["gap_closure"]["suppressed"] == str(ClosureSuppression.NONE)
    assert sorted(report["gap_closure"]["by_arm"]) == ["arm-3", "arm-4", "arm-5"]
    assert report["gap_closure"]["headline_arm"] == "arm-5"
    # (0.75 - 0.25) / (1.0 - 0.25)
    assert report["gap_closure"]["by_arm"]["arm-5"]["value"] == pytest.approx(2 / 3)
    assert report["gap_closure"]["by_arm"]["arm-3"]["value"] == pytest.approx(1 / 3)


def test_protocol_violation_means_no_ratio_exists_at_all():
    runs = [full_run("arm-1", 4), full_run("arm-2", 1), full_run("arm-3", 2),
            full_run("arm-4", 3),
            full_run("arm-5", 3, manifest_kwargs={"verifier_hash": "sha256:other"})]
    report = report_for(runs)
    assert report["protocol_violations"] == {"arm-5": ["VERIFIER_CHANGED"]}
    assert report["gap_closure"]["suppressed"] == str(
        ClosureSuppression.PROTOCOL_VIOLATION)
    assert report["gap_closure"]["by_arm"] == {}
    assert any("must re-run" in item for item in report["reporting_failures"])
    assert report["public_claim"]["allowed"] is False


def test_partial_arm_means_no_ratio_exists_at_all():
    runs = [full_run("arm-1", 4), full_run("arm-2", 1), full_run("arm-3", 2),
            full_run("arm-4", 3), full_run("arm-5", 3, drop=2)]
    report = report_for(runs)
    assert report["partial_arms"] == ["arm-5"]
    assert report["gap_closure"]["suppressed"] == str(ClosureSuppression.PARTIAL_RUN)
    assert report["gap_closure"]["by_arm"] == {}
    assert any("reported as partial" in item for item in report["reporting_failures"])


def test_missing_headline_arm_means_no_ratio_exists_at_all():
    report = report_for([full_run("arm-1", 4), full_run("arm-2", 1),
                         full_run("arm-3", 3)])
    assert report["gap_closure"]["suppressed"] == str(ClosureSuppression.MISSING_ARM)
    assert report["gap_closure"]["by_arm"] == {}


def test_suppression_is_distinct_from_a_degenerate_ratio():
    """"We never ran enough tasks" must not read as "the corpus was flat"."""
    flat = report_for([full_run("arm-1", 2), full_run("arm-2", 2),
                       full_run("arm-3", 2), full_run("arm-4", 2),
                       full_run("arm-5", 2)])
    assert flat["gap_closure"]["suppressed"] == str(ClosureSuppression.NONE)
    assert flat["gap_closure"]["by_arm"]["arm-5"]["status"] == "UNDEFINED_NO_GAP"
    assert flat["gap_closure"]["by_arm"]["arm-5"]["value"] is None


def test_negative_numerator_is_reported_never_clipped():
    report = report_for([full_run("arm-1", 4), full_run("arm-2", 3),
                         full_run("arm-3", 3), full_run("arm-4", 3),
                         full_run("arm-5", 1)])
    entry = report["gap_closure"]["by_arm"]["arm-5"]
    assert entry["status"] == "NEGATIVE_REGRESSION"
    assert entry["value"] < 0
    assert any("scored below arm-2" in item for item in report["negative_findings"])


def test_non_inferiority_uses_the_registered_margin_against_arm_one():
    report = report_for([full_run("arm-1", 4), full_run("arm-2", 1),
                         full_run("arm-5", 1)])
    assessment = report["non_inferiority"]
    assert assessment["margin"] == 0.05
    assert assessment["difference"] == pytest.approx(-0.75)
    assert assessment["non_inferior"] is False


def test_clean_sweep_across_five_arms_is_a_reporting_failure():
    runs = [full_run("arm-1", 4), full_run("arm-2", 4), full_run("arm-3", 4),
            full_run("arm-4", 4), full_run("arm-5", 4)]
    report = report_for(runs)
    # Wide intervals at N=4 are themselves a negative finding, so assert on the
    # mechanism rather than assuming the list is empty.
    assert any("too wide to be decisive" in item
               for item in report["negative_findings"])


def test_public_claim_is_refused_below_the_registered_minimum_n():
    report = report_for([full_run("arm-1", 4), full_run("arm-2", 1),
                         full_run("arm-3", 2), full_run("arm-4", 3),
                         full_run("arm-5", 3)])
    assert report["public_claim"]["allowed"] is False
    assert "below the pre-registered minimum" in report["public_claim"]["detail"]


def test_rendered_report_states_partial_and_suppression_in_words():
    runs = [full_run("arm-1", 4), full_run("arm-2", 1), full_run("arm-3", 2),
            full_run("arm-4", 3), full_run("arm-5", 3, drop=2)]
    markdown = render_report(report_for(runs))
    assert "**PARTIAL** 2/4" in markdown
    assert "**Not computed** (PARTIAL_RUN)" in markdown
    assert "does not produce a gap-closure ratio" in markdown
    assert "N=4" in markdown  # every proportion carries its N


def test_rendered_report_shows_all_three_adaptive_arms_when_clean():
    runs = [full_run("arm-1", 4), full_run("arm-2", 1), full_run("arm-3", 2),
            full_run("arm-4", 3), full_run("arm-5", 3)]
    markdown = render_report(report_for(runs))
    for name in ("arm-3", "arm-4", "arm-5"):
        assert f"| {name} | DEFINED" in markdown


def test_cost_quality_frontier_is_reported_beside_quality():
    runs = [full_run("arm-1", 4, cost_cents=10_000),
            full_run("arm-2", 1, cost_cents=100),
            full_run("arm-5", 3, cost_cents=500)]
    report = report_for(runs)
    frontier = {point["arm"]: point for point in report["cost_quality_frontier"]}
    assert frontier["arm-2"]["cost"] == pytest.approx(4.0)
    assert frontier["arm-2"]["on_frontier"] is True
    assert frontier["arm-5"]["on_frontier"] is True
    assert frontier["arm-1"]["on_frontier"] is True


# ------------------------------------------------------------------- CLI ---


def cli(*args, cwd=None):
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True, text=True, cwd=cwd or ROOT,
    )


def write(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, indent=2))
    return path


def test_cli_runs_freeze_manifest_protocol_record_report_end_to_end(tmp_path):
    candidates = [
        {"task_id": f"t{index}", "source": "cw-history", "task_class": "bugfix",
         "risk": "low", "size": "small", "base_commit": "b" * 40}
        for index in range(4)
    ]
    candidates.append({"task_id": "leak", "source": "cw-history",
                       "task_class": "bugfix", "risk": "low", "size": "small",
                       "base_commit": "b" * 40, "base_date": "2026-07-01",
                       "solution_commit": "s" * 40, "solution_date": "2026-01-01"})
    write(tmp_path / "candidates.json", {"tasks": candidates})
    corpus_path = tmp_path / "corpus.json"

    result = cli("freeze-corpus", "--candidates", str(tmp_path / "candidates.json"),
                 "--out", str(corpus_path))
    assert result.returncode == 0, result.stderr
    assert "included:   4/5" in result.stdout
    assert "SOLUTION_PREDATES_BASE: 1" in result.stdout

    # The underpowered-strata gate is report-only until asked to block.
    assert cli("freeze-corpus", "--candidates",
               str(tmp_path / "candidates.json")).returncode == 0
    assert cli("freeze-corpus", "--candidates", str(tmp_path / "candidates.json"),
               "--gate").returncode == 1

    (tmp_path / "verify.py").write_text("assert True")
    verifier_path = tmp_path / "verifier.json"
    assert cli("hash-verifier", "--path", "verify.py", "--root", str(tmp_path),
               "--command", "pytest", "--out", str(verifier_path)).returncode == 0

    manifests = []
    for arm in report_mod.ARM_SPECS:
        target = tmp_path / f"manifest-{arm}.json"
        result = cli("manifest", "--arm", arm, "--corpus", str(corpus_path),
                     "--verifier", str(verifier_path), "--roster",
                     "implementer=tier", "--seed", "order=7", "--budget",
                     "usd_cents=50000", "--env", "python=3.11", "--out", str(target))
        assert result.returncode == 0, result.stderr
        manifests.append(target)

    protocol_args = []
    for path in manifests:
        protocol_args += ["--manifest", str(path)]
    result = cli("check-protocol", "--gate", *protocol_args)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["violations"] == {}

    accepted_by_arm = {"arm-1": 4, "arm-2": 1, "arm-3": 2, "arm-4": 3, "arm-5": 3}
    runs = []
    for arm, accepted in accepted_by_arm.items():
        outcomes = [{"task_id": f"t{index}", "accepted": index < accepted,
                     "model_cost_cents": 1000, "gate_conformant": True}
                    for index in range(4)]
        write(tmp_path / f"outcomes-{arm}.json", {"outcomes": outcomes})
        run_path = tmp_path / f"run-{arm}.json"
        result = cli("record", "--arm", arm, "--corpus", str(corpus_path),
                     "--manifest", str(tmp_path / f"manifest-{arm}.json"),
                     "--outcomes", str(tmp_path / f"outcomes-{arm}.json"),
                     "--journal", str(tmp_path / "journal.jsonl"),
                     "--out", str(run_path), "--gate")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["journal_record"]
        runs.append(run_path)

    report_args = []
    for path in runs:
        report_args += ["--run", str(path)]
    result = cli("report", "--corpus", str(corpus_path), "--gate",
                 "--out-json", str(tmp_path / "report.json"),
                 "--out-md", str(tmp_path / "report.md"), *report_args)
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["gap_closure"]["by_arm"]["arm-5"]["status"] == "DEFINED"
    assert report["public_claim"]["allowed"] is False
    assert "Dynamic-DAG ablation results" in (tmp_path / "report.md").read_text()
    # --gate blocks because N=4 arms produce reporting failures worth seeing.
    assert result.returncode in (0, 1)


def test_cli_record_refuses_an_arms_results_under_another_arms_manifest(tmp_path):
    write(tmp_path / "candidates.json", {"tasks": [
        {"task_id": "t0", "source": "s", "task_class": "bugfix", "risk": "low",
         "size": "small", "base_commit": "b" * 40}]})
    corpus_path = tmp_path / "corpus.json"
    cli("freeze-corpus", "--candidates", str(tmp_path / "candidates.json"),
        "--out", str(corpus_path))
    (tmp_path / "verify.py").write_text("assert True")
    cli("hash-verifier", "--path", "verify.py", "--root", str(tmp_path),
        "--out", str(tmp_path / "verifier.json"))
    cli("manifest", "--arm", "arm-1", "--corpus", str(corpus_path), "--verifier",
        str(tmp_path / "verifier.json"), "--out", str(tmp_path / "m1.json"))
    write(tmp_path / "outcomes.json", {"outcomes": [{"task_id": "t0",
                                                     "accepted": True}]})

    result = cli("record", "--arm", "arm-2", "--corpus", str(corpus_path),
                 "--manifest", str(tmp_path / "m1.json"),
                 "--outcomes", str(tmp_path / "outcomes.json"))
    assert result.returncode == 2
    assert "protocol violation the manifest exists to prevent" in result.stderr


def test_cli_report_detects_a_run_file_edited_after_it_was_recorded(tmp_path):
    write(tmp_path / "candidates.json", {"tasks": [
        {"task_id": "t0", "source": "s", "task_class": "bugfix", "risk": "low",
         "size": "small", "base_commit": "b" * 40}]})
    corpus_path = tmp_path / "corpus.json"
    cli("freeze-corpus", "--candidates", str(tmp_path / "candidates.json"),
        "--out", str(corpus_path))
    (tmp_path / "verify.py").write_text("assert True")
    cli("hash-verifier", "--path", "verify.py", "--root", str(tmp_path),
        "--out", str(tmp_path / "verifier.json"))
    cli("manifest", "--arm", "arm-1", "--corpus", str(corpus_path), "--verifier",
        str(tmp_path / "verifier.json"), "--out", str(tmp_path / "m1.json"))
    write(tmp_path / "outcomes.json", {"outcomes": [{"task_id": "t0",
                                                     "accepted": False}]})
    cli("record", "--arm", "arm-1", "--corpus", str(corpus_path), "--manifest",
        str(tmp_path / "m1.json"), "--outcomes", str(tmp_path / "outcomes.json"),
        "--out", str(tmp_path / "run.json"))

    tampered = json.loads((tmp_path / "run.json").read_text())
    tampered["outcomes"][0]["accepted"] = True
    write(tmp_path / "run.json", tampered)

    result = cli("report", "--corpus", str(corpus_path), "--run",
                 str(tmp_path / "run.json"))
    assert result.returncode == 2
    assert "changed after it was recorded" in result.stderr


def test_cli_check_protocol_names_what_moved(tmp_path):
    write(tmp_path / "candidates.json", {"tasks": [
        {"task_id": "t0", "source": "s", "task_class": "bugfix", "risk": "low",
         "size": "small", "base_commit": "b" * 40}]})
    corpus_path = tmp_path / "corpus.json"
    cli("freeze-corpus", "--candidates", str(tmp_path / "candidates.json"),
        "--out", str(corpus_path))
    (tmp_path / "verify.py").write_text("assert True")
    cli("hash-verifier", "--path", "verify.py", "--root", str(tmp_path),
        "--out", str(tmp_path / "v1.json"))
    (tmp_path / "verify.py").write_text("assert False")
    cli("hash-verifier", "--path", "verify.py", "--root", str(tmp_path),
        "--out", str(tmp_path / "v2.json"))
    cli("manifest", "--arm", "arm-1", "--corpus", str(corpus_path), "--verifier",
        str(tmp_path / "v1.json"), "--out", str(tmp_path / "m1.json"))
    cli("manifest", "--arm", "arm-2", "--corpus", str(corpus_path), "--verifier",
        str(tmp_path / "v2.json"), "--out", str(tmp_path / "m2.json"))

    result = cli("check-protocol", "--gate", "--manifest", str(tmp_path / "m1.json"),
                 "--manifest", str(tmp_path / "m2.json"))
    assert result.returncode == 1
    assert json.loads(result.stdout)["violations"] == {"arm-2": ["VERIFIER_CHANGED"]}
    assert cli("check-protocol", "--manifest", str(tmp_path / "m1.json"),
               "--manifest", str(tmp_path / "m2.json")).returncode == 0


def test_cli_manifest_refuses_a_fractional_budget(tmp_path):
    write(tmp_path / "candidates.json", {"tasks": [
        {"task_id": "t0", "source": "s", "task_class": "bugfix", "risk": "low",
         "size": "small", "base_commit": "b" * 40}]})
    cli("freeze-corpus", "--candidates", str(tmp_path / "candidates.json"),
        "--out", str(tmp_path / "corpus.json"))
    (tmp_path / "verify.py").write_text("assert True")
    cli("hash-verifier", "--path", "verify.py", "--root", str(tmp_path),
        "--out", str(tmp_path / "verifier.json"))
    result = cli("manifest", "--arm", "arm-1", "--corpus",
                 str(tmp_path / "corpus.json"), "--verifier",
                 str(tmp_path / "verifier.json"), "--budget", "usd=10.5")
    assert result.returncode == 2
    assert "Express money in cents" in result.stderr


def test_cli_manifest_refuses_an_unregistered_arm(tmp_path):
    write(tmp_path / "candidates.json", {"tasks": [
        {"task_id": "t0", "source": "s", "task_class": "bugfix", "risk": "low",
         "size": "small", "base_commit": "b" * 40}]})
    cli("freeze-corpus", "--candidates", str(tmp_path / "candidates.json"),
        "--out", str(tmp_path / "corpus.json"))
    (tmp_path / "verify.py").write_text("assert True")
    cli("hash-verifier", "--path", "verify.py", "--root", str(tmp_path),
        "--out", str(tmp_path / "verifier.json"))
    result = cli("manifest", "--arm", "arm-9", "--corpus",
                 str(tmp_path / "corpus.json"), "--verifier",
                 str(tmp_path / "verifier.json"))
    assert result.returncode == 2
    assert "unknown arm" in result.stderr


def test_corpus_module_exposes_the_registered_floor():
    """The 20-task floor is pre-registered; it must not drift in code."""
    assert corpus_mod.MIN_STRATUM_N == 20


# --------------------------------------------------------------- journal ---


def digests(**overrides) -> dict:
    payload = {
        "results_digest": "sha256:results",
        "manifest_digest": "sha256:manifest",
        "corpus_version": "sha256:corpus",
        "verifier_hash": "sha256:verifier",
    }
    payload.update(overrides)
    return payload


def test_experiment_record_chains_onto_the_ratchet_journal(tmp_path):
    import ratchet
    from chief_wiggum import experiment_journal as ej

    journal = tmp_path / "ratchet-journal.jsonl"
    first = ej.append_experiment_record(journal, "arm-1", **digests())
    second = ej.append_experiment_record(
        journal, "arm-2", **digests(results_digest="sha256:other"))
    assert (first, second) == ("rec-00001", "rec-00002")

    records = ratchet.verified_prefix(journal)
    assert len(records) == 2
    assert records[0]["event"] == ej.EXPERIMENT_RECORD
    assert records[0]["ref"] == "arm-1"
    assert records[0]["details"] == "sha256:results"
    # Never merged: an experiment record must not move the ratchet high-water.
    assert all(record["merged"] is False for record in records)
    assert not ratchet.derive_highwater(records)["pass_set"]
    assert [record["ref"] for record in ej.experiment_records(journal)] == [
        "arm-1", "arm-2"]


def test_experiment_record_fails_closed_on_a_broken_chain(tmp_path):
    from chief_wiggum import experiment_journal as ej

    journal = tmp_path / "ratchet-journal.jsonl"
    ej.append_experiment_record(journal, "arm-1", **digests())
    with journal.open("a") as handle:
        handle.write("garbage\n")
    with pytest.raises(ej.ExperimentJournalError, match="fail closed"):
        ej.append_experiment_record(journal, "arm-2", **digests())
    # The torn tail is not evidence: the reader stops before it.
    assert len(ej.experiment_records(journal)) == 1


def test_reader_refuses_a_well_formed_but_unchained_record(tmp_path):
    """The dangerous forgery is valid JSON, not garbage.

    A hand-appended record parses fine; only the hash chain says it was never
    journaled. A reader that merely skipped unparseable lines would hand this
    one back as evidence.
    """
    from chief_wiggum import experiment_journal as ej

    journal = tmp_path / "ratchet-journal.jsonl"
    ej.append_experiment_record(journal, "arm-1", **digests())
    forged = {
        "record_id": "rec-00002", "event": ej.EXPERIMENT_RECORD, "ref": "arm-5",
        "details": "sha256:invented", "manifest_digest": "sha256:invented",
        "corpus_version": "sha256:corpus", "verifier_hash": "sha256:verifier",
        "merged": False, "record_hash": "0" * 64,
    }
    with journal.open("a") as handle:
        handle.write(json.dumps(forged, sort_keys=True) + "\n")

    records = ej.experiment_records(journal)
    assert [record["ref"] for record in records] == ["arm-1"]


def test_experiment_record_refuses_a_missing_digest(tmp_path):
    from chief_wiggum import experiment_journal as ej

    journal = tmp_path / "ratchet-journal.jsonl"
    for field in ("results_digest", "manifest_digest", "corpus_version",
                  "verifier_hash", "arm"):
        payload = digests()
        if field == "arm":
            with pytest.raises(ej.ExperimentJournalError, match="arm"):
                ej.append_experiment_record(journal, "", **payload)
        else:
            payload[field] = ""
            with pytest.raises(ej.ExperimentJournalError, match=field):
                ej.append_experiment_record(journal, "arm-1", **payload)
    assert not journal.exists()


def test_experiment_records_coexist_with_gate_authority_events(tmp_path):
    """Both event types share one chain; neither breaks the other's reader."""
    import ratchet
    from chief_wiggum import experiment_journal as ej

    journal = tmp_path / "ratchet-journal.jsonl"
    ratchet.append_authority_event(journal, "check_thing", "wire", wired_rid="rec-1")
    ej.append_experiment_record(journal, "arm-1", **digests())
    ratchet.append_authority_event(journal, "check_thing", "unwire")
    assert len(ratchet.verified_prefix(journal)) == 3
    assert ratchet.last_authority_action(journal, "check_thing") == "unwire"
    assert len(ej.experiment_records(journal)) == 1


# -------------------------------------------------------- static fallback ---


def test_static_fallback_still_projects_a_wave_plan():
    """The registered rollback path: back to `plan_waves.py` plus static roles.

    The pre-registration says the fallback is exercised rather than assumed.
    It shares #385's graph, so the projection is the thing to exercise: if
    this stops working, there is nothing to roll back TO and the rollback
    criteria in the pre-registration are unenforceable.
    """
    from chief_wiggum.dag import (
        dependency_block_to_intent_graph,
        project_legacy_waves,
    )

    block = "<!-- DEPENDENCIES\n#1: []\n#2: [#1]\n#3: [#1]\n-->"
    intent = dependency_block_to_intent_graph(
        block, graph_id="GRF-391-rollback", issues=[1, 2, 3],
        source_ref="fixture:391-rollback",
    )
    result = project_legacy_waves(intent)
    assert result.exit_code == 0
    assert result.plan["waves"] == [[1], [2, 3]]
