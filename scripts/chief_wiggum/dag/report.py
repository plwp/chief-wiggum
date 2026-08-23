"""Raw results, coverage, and report assembly for the ablation (chief-wiggum#391).

`experiment.py` holds the pre-registered analysis primitives and is
deliberately left alone: the formula, the margin and the degenerate cases were
registered before any arm ran, and a pre-registered artifact that keeps
growing branches is not pre-registered any more.

This module decides whether there is a number to analyse at all. It folds raw
per-task outcomes into arm results, establishes what each arm actually
attempted against the frozen corpus, and enforces the registered stopping
rules AROUND the formula rather than inside it:

- an arm that ran under different conditions stops and re-runs, so no ratio is
  computed from the set it is in;
- a partial experiment is reported as partial, with its N, and produces no
  ratio;
- gap closure is reported for arms 3, 4 and 5 against the same denominator, so
  the flattering one cannot be quietly promoted to the headline.

Suppression is settled before the ratio is computed, not disclaimed after.
Computing a number and then explaining why it should not be quoted is how a
suppressed ratio ends up quoted anyway.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .canonical import canonical_json_bytes
from .experiment import (
    ArmResult,
    GapClosure,
    Interval,
    NonInferiority,
    RunManifest,
    cost_quality_frontier,
    gap_closure,
    negative_findings,
    protocol_violations,
    readme_claim_allowed,
    reporting_failures,
    wilson_interval,
)

# The arms the pre-registered formula names. Arm 5 is the adaptive arm: the
# registered non-inferiority claim is "arm 5 is not worse than arm 1", which
# is what makes it the headline rather than a choice made after seeing
# results. Arms 3 and 4 are reported beside it against the same denominator.
FRONTIER_ARM = "arm-1"
STATIC_OPEN_ARM = "arm-2"
ADAPTIVE_ARMS = ("arm-3", "arm-4", "arm-5")
HEADLINE_ADAPTIVE_ARM = "arm-5"

# The registered arm table, copied from the pre-registration so an arm cannot
# be relabelled at run time. Tiers, never model names: the roster changes and
# the experiment has to survive it.
ARM_SPECS: dict[str, tuple[str, str]] = {
    FRONTIER_ARM: ("frontier-tier", "static factory"),
    STATIC_OPEN_ARM: ("open-tier", "static factory"),
    "arm-3": ("open-tier", "dynamic DAG"),
    "arm-4": ("open-tier", "dynamic DAG + selective candidates"),
    "arm-5": ("adaptive hybrid routing", "dynamic DAG"),
}


class ClosureSuppression(StrEnum):
    """Why a gap-closure ratio was not computed at all.

    Distinct from `GapClosureStatus`, which describes a ratio that WAS
    computed and came back degenerate. Collapsing the two would let "we never
    ran enough tasks" read like "the corpus was not discriminative", and only
    one of those is a finding about the corpus.
    """

    NONE = "NONE"
    PROTOCOL_VIOLATION = "PROTOCOL_VIOLATION"
    PARTIAL_RUN = "PARTIAL_RUN"
    MISSING_ARM = "MISSING_ARM"


@dataclass(frozen=True)
class TaskOutcome:
    """One task's result under one arm.

    Money is in whole cents and time in whole seconds because these records
    are hashed, and INV-dag-004's canonical encoding rejects floats so a hash
    stays replay stable.
    """

    task_id: str
    accepted: bool
    gate_conformant: bool = True
    escaped_defect: bool = False
    operator_seconds: int = 0
    model_cost_cents: int = 0
    wall_clock_seconds: int = 0
    retries: int = 0
    graph_mutations: int = 0
    escalations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "accepted": self.accepted,
            "gate_conformant": self.gate_conformant,
            "escaped_defect": self.escaped_defect,
            "operator_seconds": self.operator_seconds,
            "model_cost_cents": self.model_cost_cents,
            "wall_clock_seconds": self.wall_clock_seconds,
            "retries": self.retries,
            "graph_mutations": self.graph_mutations,
            "escalations": self.escalations,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TaskOutcome:
        task_id = str(raw.get("task_id", "")).strip()
        if not task_id:
            raise ValueError("task outcome is missing task_id")
        if "accepted" not in raw:
            raise ValueError(
                f"task outcome {task_id} has no 'accepted' field; an absent"
                " verdict must not default to accepted or to rejected"
            )

        def whole(name: str) -> int:
            value = raw.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"task outcome {task_id} field {name}={value!r} must be a"
                    " whole integer (cents, seconds); these records are hashed"
                    " and the canonical encoding rejects floats"
                )
            return value

        return cls(
            task_id=task_id,
            accepted=bool(raw["accepted"]),
            gate_conformant=bool(raw.get("gate_conformant", True)),
            escaped_defect=bool(raw.get("escaped_defect", False)),
            operator_seconds=whole("operator_seconds"),
            model_cost_cents=whole("model_cost_cents"),
            wall_clock_seconds=whole("wall_clock_seconds"),
            retries=whole("retries"),
            graph_mutations=whole("graph_mutations"),
            escalations=whole("escalations"),
        )


@dataclass(frozen=True)
class Coverage:
    """What an arm actually attempted against the frozen corpus.

    `attempted` is never inferred from how many records were handed in. An
    outcome for a task outside the corpus is `unknown`, not attempted, and a
    corpus task with no outcome is `missing`, not a rejection - scoring a
    missing task as a failure would let an arm that died halfway look merely
    bad instead of incomplete.
    """

    corpus_n: int
    attempted: int
    missing: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return (self.attempted == self.corpus_n and not self.missing
                and not self.unknown and not self.duplicates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_n": self.corpus_n,
            "attempted": self.attempted,
            "complete": self.complete,
            "missing": list(self.missing),
            "unknown": list(self.unknown),
            "duplicates": list(self.duplicates),
        }


@dataclass(frozen=True)
class ArmRun:
    """One arm's manifest, raw outcomes, coverage, and derived result."""

    result: ArmResult
    manifest: RunManifest
    coverage: Coverage
    outcomes: tuple[TaskOutcome, ...]

    def results_digest(self) -> str:
        """Hash of the raw per-task records, sorted by task id.

        The sort is explicit because a task outcome carries no DAG identity
        key, so the canonical encoder will not sort the list for us and the
        same results in a different order would hash differently.
        """
        payload = {
            "arm": self.result.arm,
            "manifest_digest": self.manifest.digest(),
            "outcomes": [outcome.to_dict() for outcome in
                         sorted(self.outcomes, key=lambda item: item.task_id)],
        }
        return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "results_digest": self.results_digest(),
            "manifest": self.manifest.to_dict(),
            "manifest_digest": self.manifest.digest(),
            "coverage": self.coverage.to_dict(),
            "result": self.result.to_dict(),
            "outcomes": [outcome.to_dict() for outcome in
                         sorted(self.outcomes, key=lambda item: item.task_id)],
        }


def build_arm_run(
    *,
    arm: str,
    model_tier: str,
    process: str,
    manifest: RunManifest,
    outcomes: Sequence[TaskOutcome],
    strata_by_task: Mapping[str, str],
) -> ArmRun:
    """Fold raw per-task outcomes into one arm's result plus its coverage.

    `strata_by_task` comes from the frozen corpus, never from the outcome
    records. An arm that reported its own strata could relabel a task it lost
    into a stratum it was winning.
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    unknown: list[str] = []
    scored: list[TaskOutcome] = []
    for outcome in outcomes:
        if outcome.task_id in seen:
            duplicates.append(outcome.task_id)
            continue
        seen.add(outcome.task_id)
        if outcome.task_id not in strata_by_task:
            unknown.append(outcome.task_id)
            continue
        scored.append(outcome)

    coverage = Coverage(
        corpus_n=len(strata_by_task),
        attempted=len(scored),
        missing=tuple(sorted(set(strata_by_task) - seen)),
        unknown=tuple(sorted(unknown)),
        duplicates=tuple(sorted(duplicates)),
    )

    strata: dict[str, tuple[int, int]] = {}
    for outcome in scored:
        stratum = strata_by_task[outcome.task_id]
        accepted, attempted = strata.get(stratum, (0, 0))
        strata[stratum] = (accepted + int(outcome.accepted), attempted + 1)

    result = ArmResult(
        arm=arm,
        model_tier=model_tier,
        process=process,
        accepted=sum(1 for outcome in scored if outcome.accepted),
        attempted=len(scored),
        escaped_defects=sum(1 for outcome in scored if outcome.escaped_defect),
        operator_minutes=sum(outcome.operator_seconds for outcome in scored) / 60.0,
        model_cost=sum(outcome.model_cost_cents for outcome in scored) / 100.0,
        wall_clock_seconds=float(
            sum(outcome.wall_clock_seconds for outcome in scored)
        ),
        retries=sum(outcome.retries for outcome in scored),
        graph_mutations=sum(outcome.graph_mutations for outcome in scored),
        escalations=sum(outcome.escalations for outcome in scored),
        strata=strata,
    )
    return ArmRun(result=result, manifest=manifest, coverage=coverage,
                  outcomes=tuple(outcomes))


def conformance_rate(run: ArmRun) -> Interval:
    """Contract, test, and gate conformance across the arm's SCORED tasks.

    Scored, not submitted: an outcome for a task outside the corpus does not
    count towards conformance any more than it counts towards quality.
    """
    unknown = set(run.coverage.unknown)
    seen: set[str] = set()
    scored = []
    for outcome in run.outcomes:
        if outcome.task_id in unknown or outcome.task_id in seen:
            continue
        seen.add(outcome.task_id)
        scored.append(outcome)
    return wilson_interval(
        sum(1 for outcome in scored if outcome.gate_conformant), len(scored)
    )


def _suppression(
    by_arm: Mapping[str, ArmRun],
    violations: Mapping[str, Sequence[str]],
    partial: Sequence[str],
) -> tuple[ClosureSuppression, str]:
    """Decide whether the registered stopping rules permit a ratio at all."""
    required = [FRONTIER_ARM, STATIC_OPEN_ARM, HEADLINE_ADAPTIVE_ARM]
    absent = [name for name in required if name not in by_arm]
    if violations:
        return ClosureSuppression.PROTOCOL_VIOLATION, (
            "arms ran under different conditions: "
            + "; ".join(f"{arm}: {', '.join(items)}"
                        for arm, items in sorted(violations.items()))
            + ". The pre-registration stops the arm and re-runs it from a clean"
              " manifest; no ratio is computed."
        )
    if partial:
        return ClosureSuppression.PARTIAL_RUN, (
            "incomplete arms: " + ", ".join(sorted(partial))
            + ". A partial result is reported as partial, with its N, and does"
              " not produce a gap-closure ratio."
        )
    if absent:
        return ClosureSuppression.MISSING_ARM, (
            "the formula needs " + ", ".join(required)
            + "; missing " + ", ".join(absent)
        )
    return ClosureSuppression.NONE, ""


def assemble_report(
    *,
    corpus: Mapping[str, Any],
    runs: Sequence[ArmRun],
    non_inferiority: NonInferiority,
    min_n: int = 100,
) -> dict[str, Any]:
    """Assemble the full result: coverage, protocol, closure, frontier, findings."""
    by_arm = {run.result.arm: run for run in runs}
    arms = [run.result for run in runs]

    violations: dict[str, list[str]] = {}
    baseline = by_arm.get(FRONTIER_ARM)
    if baseline is not None:
        for run in runs:
            if run.result.arm == FRONTIER_ARM:
                continue
            found = protocol_violations(baseline.manifest, run.manifest)
            if found:
                violations[run.result.arm] = [str(item) for item in found]

    partial = sorted(run.result.arm for run in runs if not run.coverage.complete)
    suppression, detail = _suppression(by_arm, violations, partial)

    closures: dict[str, Any] = {}
    headline: GapClosure | None = None
    if suppression is ClosureSuppression.NONE:
        frontier_quality = by_arm[FRONTIER_ARM].result.quality.point
        static_open_quality = by_arm[STATIC_OPEN_ARM].result.quality.point
        for name in ADAPTIVE_ARMS:
            run = by_arm.get(name)
            if run is None:
                continue
            closure = gap_closure(
                frontier_quality=frontier_quality,
                static_open_quality=static_open_quality,
                adaptive_open_quality=run.result.quality.point,
            )
            closures[name] = closure.to_dict()
            if name == HEADLINE_ADAPTIVE_ARM:
                headline = closure

    treatment = by_arm.get(HEADLINE_ADAPTIVE_ARM)
    control = by_arm.get(FRONTIER_ARM)
    non_inferiority_result: dict[str, Any] | None = None
    if treatment is not None and control is not None:
        non_inferiority_result = non_inferiority.assess(
            treatment.result.quality, control.result.quality
        )

    findings = list(negative_findings(arms, STATIC_OPEN_ARM))
    failures = list(reporting_failures(arms, findings))
    failures += [f"{arm} did not attempt the whole corpus; reported as partial"
                 for arm in partial]
    failures += [f"{arm} violated protocol ({', '.join(items)}) and must re-run"
                 for arm, items in sorted(violations.items())]

    if headline is not None:
        allowed, claim_detail = readme_claim_allowed(arms, headline, min_n=min_n)
    else:
        allowed, claim_detail = False, (
            f"no gap-closure ratio was computed ({suppression})"
            + (f": {detail}" if detail else "")
        )

    underpowered = set(corpus.get("underpowered_strata") or [])
    return {
        "corpus": {
            key: corpus.get(key) for key in (
                "corpus_version", "considered", "included_n", "excluded_n",
                "exclusion_counts", "strata", "underpowered_strata",
                "pretraining_risk", "min_stratum_n", "notes",
            )
        },
        "arms": [
            {
                **run.result.to_dict(),
                "coverage": run.coverage.to_dict(),
                "manifest_digest": run.manifest.digest(),
                "results_digest": run.results_digest(),
                "conformance": conformance_rate(run).to_dict(),
                "underpowered_strata": sorted(
                    name for name in run.result.stratum_intervals()
                    if name in underpowered
                ),
            }
            for run in runs
        ],
        "protocol_violations": violations,
        "partial_arms": partial,
        "gap_closure": {
            "suppressed": str(suppression),
            "suppression_detail": detail,
            "headline_arm": HEADLINE_ADAPTIVE_ARM,
            "by_arm": closures,
        },
        "non_inferiority": non_inferiority_result,
        "cost_quality_frontier": cost_quality_frontier(arms),
        "negative_findings": findings,
        "reporting_failures": failures,
        "public_claim": {"allowed": allowed, "detail": claim_detail},
    }


def _interval_of(raw: Mapping[str, Any]) -> Interval:
    return Interval(
        successes=int(raw.get("successes", 0)),
        n=int(raw.get("n", 0)),
        low=float(raw.get("ci_low", 0.0)),
        high=float(raw.get("ci_high", 0.0)),
    )


def render_report(report: Mapping[str, Any]) -> str:
    """Markdown for humans. Every proportion carries its N and its interval."""
    corpus = report.get("corpus") or {}
    considered = corpus.get("considered", 0)
    lines = [
        "# Dynamic-DAG ablation results (chief-wiggum#391)",
        "",
        "Pre-registration: `docs/experiments/dynamic-dag/pre-registration.md`.",
        "Arms are capability tiers, never model names. The readable signal is",
        "the cross-arm difference under fixed conditions, not the absolute score.",
        "",
        "## Corpus",
        "",
        f"- version: `{corpus.get('corpus_version', 'unknown')}`",
        f"- considered: {considered}",
        f"- included: {corpus.get('included_n', 0)}/{considered}",
        f"- excluded: {corpus.get('excluded_n', 0)}/{considered}",
    ]
    for reason, count in sorted((corpus.get("exclusion_counts") or {}).items()):
        if count:
            lines.append(f"  - {reason}: {count}")
    underpowered = corpus.get("underpowered_strata") or []
    lines.append(
        f"- underpowered strata (below N={corpus.get('min_stratum_n', 0)}):"
        f" {len(underpowered)}"
        + (" - " + ", ".join(underpowered) if underpowered else "")
    )
    risky = corpus.get("pretraining_risk") or []
    lines += [
        f"- public-benchmark tasks that may sit in pretraining: {len(risky)}"
        f"/{corpus.get('included_n', 0)}. Stated, not excluded.",
        "",
        "## Arms",
        "",
        "| Arm | Tier | Process | Accepted-patch rate | Conformance | Cost |"
        " Escaped | Coverage |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for arm in report.get("arms") or []:
        quality = _interval_of(arm.get("quality") or {})
        conformance = _interval_of(arm.get("conformance") or {})
        coverage = arm.get("coverage") or {}
        state = "complete" if coverage.get("complete") else (
            f"**PARTIAL** {coverage.get('attempted', 0)}"
            f"/{coverage.get('corpus_n', 0)}"
        )
        lines.append(
            f"| {arm.get('arm')} | {arm.get('model_tier')} | {arm.get('process')}"
            f" | {quality.render()} | {conformance.render()}"
            f" | ${arm.get('model_cost', 0.0):.2f}"
            f" | {arm.get('escaped_defects', 0)} | {state} |"
        )

    closure = report.get("gap_closure") or {}
    lines += ["", "## Gap closure", ""]
    if closure.get("suppressed") != str(ClosureSuppression.NONE):
        lines += [f"**Not computed** ({closure.get('suppressed')}).", "",
                  closure.get("suppression_detail", "")]
    else:
        lines += [
            f"Headline arm: `{closure.get('headline_arm')}`, the arm the"
            " registered non-inferiority claim is about. Arms 3 and 4 are shown"
            " against the same denominator so the flattering one cannot be"
            " quietly promoted.",
            "",
            "| Arm | Status | Value | Detail |",
            "|---|---|---|---|",
        ]
        for name, entry in sorted((closure.get("by_arm") or {}).items()):
            value = entry.get("value")
            rendered = "not published" if value is None else f"{value:.1%}"
            lines.append(
                f"| {name} | {entry.get('status')} | {rendered}"
                f" | {entry.get('detail') or '-'} |"
            )

    non_inferiority = report.get("non_inferiority")
    lines += ["", "## Non-inferiority", ""]
    if not non_inferiority:
        lines.append("Not assessed: arm 1 or arm 5 is absent from the results.")
    else:
        verdict = "holds" if non_inferiority["non_inferior"] else "does NOT hold"
        lines += [
            "Claim under test: arm 5 is not worse than arm 1 on accepted-patch"
            " rate.",
            "",
            f"- pre-registered margin: {non_inferiority['margin']:.1%} absolute",
            f"- observed difference: {non_inferiority['difference']:+.1%}",
            "- worst case (arm 5 lower bound minus arm 1 upper bound):"
            f" {non_inferiority['worst_case_difference']:+.1%}",
            f"- **non-inferiority {verdict}**",
        ]

    lines += ["", "## Cost/quality frontier", "",
              "Quality is never reported without its cost.", "",
              "| Arm | Cost | Quality | On frontier |", "|---|---|---|---|"]
    for point in report.get("cost_quality_frontier") or []:
        low, high = (point.get("quality_ci") or [0.0, 0.0])[:2]
        lines.append(
            f"| {point.get('arm')} | ${point.get('cost', 0.0):.2f}"
            f" | {point.get('quality', 0.0):.1%} (N={point.get('n', 0)},"
            f" {low:.1%} to {high:.1%})"
            f" | {'yes' if point.get('on_frontier') else 'no'} |"
        )

    findings = report.get("negative_findings") or []
    lines += ["", "## Negative findings", ""]
    lines += ([f"- {item}" for item in findings] if findings else
              ["None recorded. Across five arms that is itself reviewable -"
               " see reporting failures below."])

    failures = report.get("reporting_failures") or []
    lines += ["", "## Reporting failures", ""]
    lines += ([f"- {item}" for item in failures] if failures else ["None."])

    claim = report.get("public_claim") or {}
    lines += [
        "", "## Public claim gate", "",
        f"- allowed: **{'yes' if claim.get('allowed') else 'no'}**",
        f"- {claim.get('detail', '')}",
        "",
    ]
    return "\n".join(lines)
