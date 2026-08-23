"""Corpus freeze, contamination controls, and verifier hashing (chief-wiggum#391).

Rung 1 of the pre-registered ladder cannot start until two things are true:
the corpus is frozen and the verifier is content-hashed. Both are recorded in
every run manifest, and `experiment.protocol_violations` reads them to prove
that nothing except the varied factor moved between arms.

Three decisions are mechanised here rather than left to whoever runs the arms:

1. **Contamination is decided per task, with a reason, and counted.** The
   pre-registration requires the exclusion count to be recorded. A task whose
   solution is reachable from the repo state the arm is given is not
   a task, it is a lookup. Reachability is asked of git, and a question git cannot
   answer excludes the task as `UNVERIFIABLE_BASE` rather than admitting it.
   That direction matters: an unverifiable task admitted is a silently
   contaminated corpus, and contamination inflates every arm that can read it.
2. **Strata are validated against a fixed vocabulary.** A typo in a task
   class would otherwise create a stratum of one, and a stratum of one is
   reported as a stratum. Unknown values exclude the task and are counted.
3. **Underpowered strata are reported, never dropped.** The pre-registration
   sets a 20-task floor for a stratum-level number to be quoted at all. Below
   it the stratum still exists and still contributes to the arm total; it is
   labelled underpowered with its N so the reader can see what was thin.

Pretraining risk is an annotation, not an exclusion. The pre-registration is
explicit that public-benchmark instances which may sit in a model's
pretraining are stated plainly, and that cross-arm comparison under fixed
conditions, not absolute score, is the readable signal.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes

# Pre-registered vocabularies. Anything outside them is an excluded task, not
# a new stratum.
TASK_CLASSES = ("feature", "bugfix", "refactor", "test-only", "frontend", "backend")
RISK_CLASSES = ("low", "medium", "high")
SIZE_CLASSES = ("small", "medium", "large")

# "Minimum 20 tasks per stratum for a stratum-level number to be reported at
# all" - pre-registration, Corpus and strata.
MIN_STRATUM_N = 20


class ExclusionReason(StrEnum):
    """Why a candidate task is not in the frozen corpus."""

    SOLUTION_IN_BASE = "SOLUTION_IN_BASE"
    SOLUTION_PREDATES_BASE = "SOLUTION_PREDATES_BASE"
    UNVERIFIABLE_BASE = "UNVERIFIABLE_BASE"
    DUPLICATE_ID = "DUPLICATE_ID"
    INVALID_STRATUM = "INVALID_STRATUM"


class Reachability(StrEnum):
    """The three answers to "can the arm read the solution from its base?"."""

    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TaskRecord:
    """One candidate task, with everything the freeze needs to judge it."""

    task_id: str
    source: str
    task_class: str
    risk: str
    size: str
    base_commit: str = ""
    base_date: str = ""
    solution_commit: str = ""
    solution_date: str = ""
    public_benchmark: bool = False

    @property
    def stratum(self) -> str:
        return f"{self.task_class}/{self.risk}/{self.size}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source": self.source,
            "task_class": self.task_class,
            "risk": self.risk,
            "size": self.size,
            "base_commit": self.base_commit,
            "base_date": self.base_date,
            "solution_commit": self.solution_commit,
            "solution_date": self.solution_date,
            "public_benchmark": self.public_benchmark,
            "stratum": self.stratum,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TaskRecord:
        missing = [key for key in ("task_id", "source", "task_class", "risk", "size")
                   if not str(raw.get(key, "")).strip()]
        if missing:
            raise ValueError(
                f"task record is missing required fields: {', '.join(missing)}"
            )
        return cls(
            task_id=str(raw["task_id"]),
            source=str(raw["source"]),
            task_class=str(raw["task_class"]),
            risk=str(raw["risk"]),
            size=str(raw["size"]),
            base_commit=str(raw.get("base_commit", "")),
            base_date=str(raw.get("base_date", "")),
            solution_commit=str(raw.get("solution_commit", "")),
            solution_date=str(raw.get("solution_date", "")),
            public_benchmark=bool(raw.get("public_benchmark", False)),
        )


@dataclass(frozen=True)
class Exclusion:
    """A task kept out of the corpus, with the reason it was kept out.

    Exclusions are retained rather than discarded: the pre-registration
    requires the exclusion count to be published, and a count with no reasons
    behind it cannot be audited.
    """

    task_id: str
    reason: ExclusionReason
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "reason": str(self.reason),
                "detail": self.detail}


def _dates_ordered(solution_date: str, base_date: str) -> bool | None:
    """True when the solution is dated at or before the base state.

    Returns None when either date is absent or unparseable, so the caller can
    tell "the solution predates the base" from "nobody knows". Comparison is
    lexicographic on ISO-8601 strings, which is why the format is checked
    rather than assumed: `2026-8-1` sorts before `2026-10-1` as text and after
    it in reality.
    """
    if not solution_date or not base_date:
        return None
    for value in (solution_date, base_date):
        parts = value.split("T")[0].split("-")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            return None
        if len(parts[0]) != 4 or len(parts[1]) != 2 or len(parts[2]) != 2:
            return None
    return solution_date <= base_date


def git_reachability(repo: str | Path) -> Callable[[str, str], Reachability]:
    """Ask git whether `solution_commit` is an ancestor of `base_commit`.

    A commit git does not have, a directory that is not a repository, or a git
    that fails for any other reason all answer UNKNOWN. UNKNOWN excludes the
    task upstream - the failure mode this avoids is the one where a broken
    subprocess call reads as "not contaminated" and quietly admits everything.
    """

    root = Path(repo)

    def check(solution_commit: str, base_commit: str) -> Reachability:
        if not solution_commit or not base_commit:
            return Reachability.UNKNOWN
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), "merge-base", "--is-ancestor",
                 solution_commit, base_commit],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return Reachability.UNKNOWN
        if completed.returncode == 0:
            return Reachability.REACHABLE
        if completed.returncode == 1:
            return Reachability.UNREACHABLE
        # Any other code is git refusing the question (unknown revision, not a
        # repository), never a clean "no".
        return Reachability.UNKNOWN

    return check


def _unasked(_solution: str, _base: str) -> Reachability:
    """The default oracle: nobody was asked, so nothing is known.

    UNKNOWN, never UNREACHABLE. An oracle that answers "not contaminated"
    because no repository was supplied is the fail-open version of this
    check - it would silently admit every task whose solution nobody looked
    for. UNKNOWN sends the task to the date fallback, and if the dates cannot
    settle it either, it is excluded as unverifiable.
    """
    return Reachability.UNKNOWN


@dataclass(frozen=True)
class FrozenCorpus:
    """An immutable, hashed corpus. Its digest is the manifest's corpus_version."""

    included: tuple[TaskRecord, ...]
    exclusions: tuple[Exclusion, ...]
    min_stratum_n: int = MIN_STRATUM_N
    notes: str = ""

    @property
    def strata(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.included:
            counts[task.stratum] = counts.get(task.stratum, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def underpowered(self) -> tuple[str, ...]:
        """Strata below the pre-registered floor. Reported, never dropped."""
        return tuple(name for name, count in self.strata.items()
                     if count < self.min_stratum_n)

    @property
    def pretraining_risk(self) -> tuple[str, ...]:
        """Included tasks that may sit in pretraining. Stated, not excluded."""
        return tuple(sorted(task.task_id for task in self.included
                            if task.public_benchmark))

    @property
    def exclusion_counts(self) -> dict[str, int]:
        counts = {str(reason): 0 for reason in ExclusionReason}
        for exclusion in self.exclusions:
            counts[str(exclusion.reason)] += 1
        return counts

    @property
    def considered(self) -> int:
        """The denominator. An exclusion count without one says nothing."""
        return len(self.included) + len(self.exclusions)

    def digest(self) -> str:
        """Content hash of the frozen corpus.

        The task list is sorted by id before hashing. That sort is NOT
        redundant here: `canonical_json_bytes` only auto-sorts collections of
        records carrying a DAG identity key, and a task record carries none -
        so without it the same corpus fed in a different order would hash
        differently and read as a CORPUS_CHANGED protocol violation.
        """
        payload = {
            "included": [task.to_dict() for task in
                         sorted(self.included, key=lambda task: task.task_id)],
            "min_stratum_n": self.min_stratum_n,
        }
        return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_version": self.digest(),
            "notes": self.notes,
            "min_stratum_n": self.min_stratum_n,
            "considered": self.considered,
            "included_n": len(self.included),
            "excluded_n": len(self.exclusions),
            "exclusion_counts": self.exclusion_counts,
            "strata": self.strata,
            "underpowered_strata": list(self.underpowered),
            "pretraining_risk": list(self.pretraining_risk),
            "included": [task.to_dict() for task in
                         sorted(self.included, key=lambda task: task.task_id)],
            "exclusions": [exclusion.to_dict() for exclusion in
                           sorted(self.exclusions, key=lambda item: item.task_id)],
        }

    def render(self) -> str:
        """Human summary. Every count carries its denominator."""
        lines = [
            f"corpus {self.digest()}",
            f"  considered: {self.considered}",
            f"  included:   {len(self.included)}/{self.considered}",
            f"  excluded:   {len(self.exclusions)}/{self.considered}",
        ]
        for reason, count in sorted(self.exclusion_counts.items()):
            if count:
                lines.append(f"    {reason}: {count}")
        lines.append(f"  strata: {len(self.strata)} (floor N={self.min_stratum_n})")
        for name, count in self.strata.items():
            flag = "  UNDERPOWERED" if count < self.min_stratum_n else ""
            lines.append(f"    {name}: {count}{flag}")
        risky = self.pretraining_risk
        lines.append(
            f"  pretraining risk: {len(risky)}/{len(self.included)} included tasks"
            " are public-benchmark instances that may be in pretraining;"
            " read cross-arm differences, not absolute scores"
        )
        return "\n".join(lines)


def freeze_corpus(
    candidates: Iterable[TaskRecord],
    *,
    reachable: Callable[[str, str], Reachability] | None = None,
    min_stratum_n: int = MIN_STRATUM_N,
    notes: str = "",
) -> FrozenCorpus:
    """Apply the pre-registered inclusion rules and freeze what survives.

    The rules run in a fixed order so the same candidate list always produces
    the same corpus: identity, then stratum vocabulary, then contamination.
    """
    oracle = reachable or _unasked
    included: list[TaskRecord] = []
    exclusions: list[Exclusion] = []
    seen: set[str] = set()

    for task in candidates:
        if task.task_id in seen:
            exclusions.append(Exclusion(
                task.task_id, ExclusionReason.DUPLICATE_ID,
                "task id appears more than once in the candidate list",
            ))
            continue
        seen.add(task.task_id)

        bad = [f"{name}={value!r}" for name, value, allowed in (
            ("task_class", task.task_class, TASK_CLASSES),
            ("risk", task.risk, RISK_CLASSES),
            ("size", task.size, SIZE_CLASSES),
        ) if value not in allowed]
        if bad:
            exclusions.append(Exclusion(
                task.task_id, ExclusionReason.INVALID_STRATUM,
                "outside the pre-registered vocabulary: " + ", ".join(bad),
            ))
            continue

        verdict = _contamination(task, oracle)
        if verdict is not None:
            exclusions.append(verdict)
            continue
        included.append(task)

    return FrozenCorpus(
        included=tuple(included),
        exclusions=tuple(exclusions),
        min_stratum_n=min_stratum_n,
        notes=notes,
    )


def _contamination(
    task: TaskRecord, oracle: Callable[[str, str], Reachability]
) -> Exclusion | None:
    """None when the task is clean; an Exclusion when it is not, or cannot be judged."""
    # A task with no solution to leak - a live task, or one whose fix does not
    # exist yet - cannot be contaminated by reachability.
    if not task.solution_commit and not task.solution_date:
        if task.public_benchmark:
            return None
        if not task.base_commit:
            return Exclusion(
                task.task_id, ExclusionReason.UNVERIFIABLE_BASE,
                "no base commit recorded, so the repo state given to the arm"
                " cannot be established",
            )
        return None

    if not task.base_commit and not task.base_date:
        return Exclusion(
            task.task_id, ExclusionReason.UNVERIFIABLE_BASE,
            "no base commit or base date, so reachability cannot be decided",
        )

    verdict = oracle(task.solution_commit, task.base_commit)
    if verdict is Reachability.REACHABLE:
        return Exclusion(
            task.task_id, ExclusionReason.SOLUTION_IN_BASE,
            f"solution {task.solution_commit[:12]} is an ancestor of base"
            f" {task.base_commit[:12]}; the arm can read the answer",
        )
    if verdict is Reachability.UNREACHABLE:
        return None

    # The oracle could not answer. Fall back to dates, and if those cannot
    # answer either, exclude - an unverifiable task admitted is a corpus
    # nobody can defend.
    ordered = _dates_ordered(task.solution_date, task.base_date)
    if ordered is True:
        return Exclusion(
            task.task_id, ExclusionReason.SOLUTION_PREDATES_BASE,
            f"solution dated {task.solution_date} at or before base"
            f" {task.base_date}, and reachability could not be decided",
        )
    if ordered is False:
        return None
    return Exclusion(
        task.task_id, ExclusionReason.UNVERIFIABLE_BASE,
        "reachability could not be decided and the dates do not settle it;"
        " excluded rather than admitted, because an unverifiable task admitted"
        " is a silently contaminated corpus",
    )


@dataclass(frozen=True)
class VerifierFingerprint:
    """The frozen verifier. Hashed before any arm runs, identical across arms."""

    files: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    command: str = ""

    def digest(self) -> str:
        payload = {
            "command": self.command,
            "files": [{"path": path, "sha256": sha} for path, sha in self.files],
        }
        return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "verifier_hash": self.digest(),
            "command": self.command,
            "files": [{"path": path, "sha256": sha} for path, sha in self.files],
        }


def fingerprint_verifier(
    paths: Sequence[str | Path], *, root: str | Path = ".", command: str = ""
) -> VerifierFingerprint:
    """Content-hash the verifier's files.

    A path that does not exist raises rather than being skipped. "The verifier
    file was missing so we hashed the rest" is how two arms end up sharing a
    hash while running different verifiers, which is the exact thing the hash
    exists to make impossible.
    """
    base = Path(root)
    entries: list[tuple[str, str]] = []
    for candidate in paths:
        target = Path(candidate)
        absolute = target if target.is_absolute() else base / target
        if not absolute.is_file():
            raise FileNotFoundError(
                f"verifier file not found: {absolute}. The verifier is frozen"
                " before any arm runs; a missing file is a stop, not a skip."
            )
        digest = hashlib.sha256(absolute.read_bytes()).hexdigest()
        relative = str(target).replace("\\", "/")
        entries.append((relative, digest))
    entries.sort()
    return VerifierFingerprint(files=tuple(entries), command=command)
