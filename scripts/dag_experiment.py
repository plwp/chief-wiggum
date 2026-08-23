#!/usr/bin/env python3
"""CLI for the dynamic-DAG ablation (chief-wiggum#391).

The pre-registration is committed and binding; this is the machinery that runs
under it. The order of the subcommands is the order of rung 1:

    freeze-corpus   apply the inclusion rules, count the exclusions, hash it
    hash-verifier   content-hash the verifier before any arm runs
    manifest        pin one arm's conditions and hash them
    check-protocol  prove every arm ran under the same conditions
    record          fold raw per-task outcomes in, journal their digest
    report          assemble the result, or refuse to

`check-protocol` runs BEFORE `report` on purpose. The pre-registration stops an
arm on a protocol violation and re-runs it from a clean manifest, and that
decision has to be reachable without anyone having looked at a score yet.

Exit codes are distinct: 0 success, 1 a finding the command is gating on (an
underpowered corpus under `--gate`, a protocol violation, a reporting failure
under `--gate`), 2 bad input, 3 a failure of the tool itself. A gate that
cannot run must never read as a gate that passed.

Every gate here is report-only by default and blocks only under `--gate`
(docs/gate-rollout.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chief_wiggum.dag import corpus as corpus_mod  # noqa: E402
from chief_wiggum.dag import report as report_mod  # noqa: E402
from chief_wiggum.dag.experiment import NonInferiority, RunManifest  # noqa: E402

EXIT_OK = 0
EXIT_FINDING = 1
EXIT_INPUT = 2
EXIT_ERROR = 3

# The margin registered in docs/experiments/dynamic-dag/pre-registration.md.
# It is not a flag. A margin the operator can pass on the command line is a
# margin chosen after seeing results.
REGISTERED_MARGIN = 0.05
REGISTERED_MARGIN_JUSTIFICATION = (
    "5 percentage points absolute, registered before any arm ran: below the"
    " run-to-run noise of the existing suite, and below the ~19-point interval"
    " the README already reports at N=20. See the pre-registration."
)
REGISTERED_MIN_N = 100


def _fail(message: str, code: int = EXIT_ERROR) -> int:
    print(json.dumps({"ok": False, "error": message}, indent=2), file=sys.stderr)
    return code


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text())


def _write_json(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _pairs(values: list[str] | None, *, as_int: bool = False) -> dict[str, Any]:
    """Parse repeated `key=value` flags.

    Integers only where the manifest hashes them: the canonical encoding
    rejects floats, so a budget passed as `usd=10.5` is refused here with an
    explanation rather than deep inside the hasher.
    """
    out: dict[str, Any] = {}
    for item in values or []:
        if "=" not in item:
            raise ValueError(f"expected key=value, got {item!r}")
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"empty key in {item!r}")
        if as_int:
            try:
                out[key] = int(value)
            except ValueError:
                raise ValueError(
                    f"{key}={value!r} must be a whole integer; manifests are"
                    " hashed and the canonical encoding rejects floats."
                    " Express money in cents and time in whole seconds."
                ) from None
        else:
            out[key] = value
    return out


def _freeze_corpus(args: argparse.Namespace) -> int:
    try:
        raw = _load_json(args.candidates)
    except (OSError, json.JSONDecodeError) as exc:
        return _fail(f"cannot read candidates: {exc}", EXIT_INPUT)
    entries = raw.get("tasks", raw) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return _fail("candidates must be a list, or an object with a 'tasks' list",
                     EXIT_INPUT)
    try:
        candidates = [corpus_mod.TaskRecord.from_dict(entry) for entry in entries]
    except (ValueError, TypeError, AttributeError) as exc:
        return _fail(f"bad candidate record: {exc}", EXIT_INPUT)

    oracle = corpus_mod.git_reachability(args.repo) if args.repo else None
    frozen = corpus_mod.freeze_corpus(
        candidates,
        reachable=oracle,
        min_stratum_n=args.min_stratum_n,
        notes=args.notes,
    )
    print(frozen.render())
    if args.out:
        _write_json(args.out, frozen.to_dict())
        print(f"\nwrote {args.out}")
    if not frozen.included:
        return _fail("every candidate was excluded; the corpus is empty",
                     EXIT_FINDING if args.gate else EXIT_OK)
    if args.gate and frozen.underpowered:
        return _fail(
            f"{len(frozen.underpowered)} of {len(frozen.strata)} strata are below"
            f" the pre-registered floor of N={frozen.min_stratum_n}:"
            f" {', '.join(frozen.underpowered)}",
            EXIT_FINDING,
        )
    return EXIT_OK


def _hash_verifier(args: argparse.Namespace) -> int:
    try:
        fingerprint = corpus_mod.fingerprint_verifier(
            args.path, root=args.root, command=args.command
        )
    except FileNotFoundError as exc:
        return _fail(str(exc), EXIT_INPUT)
    payload = fingerprint.to_dict()
    print(json.dumps(payload, indent=2))
    if args.out:
        _write_json(args.out, payload)
    return EXIT_OK


def _manifest(args: argparse.Namespace) -> int:
    if args.arm not in report_mod.ARM_SPECS:
        return _fail(
            f"unknown arm {args.arm!r}; the registered arms are "
            + ", ".join(sorted(report_mod.ARM_SPECS)),
            EXIT_INPUT,
        )
    try:
        frozen = _load_json(args.corpus)
        verifier = _load_json(args.verifier)
        manifest = RunManifest(
            arm=args.arm,
            corpus_version=str(frozen["corpus_version"]),
            provider_roster=_pairs(args.roster),
            seeds=_pairs(args.seed, as_int=True),
            budgets=_pairs(args.budget, as_int=True),
            verifier_hash=str(verifier["verifier_hash"]),
            environment=_pairs(args.env),
        )
        digest = manifest.digest()
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        return _fail(f"cannot build manifest: {exc}", EXIT_INPUT)
    except ValueError as exc:
        return _fail(str(exc), EXIT_INPUT)
    payload = {**manifest.to_dict(), "manifest_digest": digest}
    print(json.dumps(payload, indent=2))
    if args.out:
        _write_json(args.out, payload)
    return EXIT_OK


def _read_manifest(path: str | Path) -> RunManifest:
    raw = _load_json(path)
    return RunManifest(
        arm=str(raw["arm"]),
        corpus_version=str(raw["corpus_version"]),
        provider_roster=dict(raw.get("provider_roster") or {}),
        seeds=dict(raw.get("seeds") or {}),
        budgets=dict(raw.get("budgets") or {}),
        verifier_hash=str(raw["verifier_hash"]),
        environment=dict(raw.get("environment") or {}),
    )


def _check_protocol(args: argparse.Namespace) -> int:
    try:
        manifests = {}
        for path in args.manifest:
            manifest = _read_manifest(path)
            if manifest.arm in manifests:
                return _fail(f"two manifests for {manifest.arm}", EXIT_INPUT)
            manifests[manifest.arm] = manifest
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        return _fail(f"cannot read manifests: {exc}", EXIT_INPUT)

    baseline = manifests.get(report_mod.FRONTIER_ARM)
    if baseline is None:
        return _fail(
            f"no manifest for {report_mod.FRONTIER_ARM}; it is the baseline every"
            " other arm is compared against",
            EXIT_INPUT,
        )
    from chief_wiggum.dag.experiment import protocol_violations

    findings: dict[str, list[str]] = {}
    for arm, manifest in sorted(manifests.items()):
        if arm == report_mod.FRONTIER_ARM:
            continue
        violations = protocol_violations(baseline, manifest)
        if violations:
            findings[arm] = [str(item) for item in violations]

    print(json.dumps({
        "baseline": report_mod.FRONTIER_ARM,
        "arms_checked": sorted(manifests),
        "violations": findings,
    }, indent=2, sort_keys=True))
    if findings:
        print(
            "\nEverything except the varied factor must be fixed. Each arm above"
            " stops and re-runs from a clean manifest before any result from it"
            " is read.",
            file=sys.stderr,
        )
        return EXIT_FINDING if args.gate else EXIT_OK
    return EXIT_OK


def _strata_by_task(frozen: dict[str, Any]) -> dict[str, str]:
    """Task id -> stratum, from the corpus FILE.

    Reports a missing field as bad input rather than raising KeyError out of a
    dict comprehension: a hand-built or truncated corpus file is an operator
    mistake with a clear fix, and a bare traceback reads like a tool bug.
    """
    strata: dict[str, str] = {}
    for task in frozen.get("included", []):
        task_id = str(task.get("task_id", "")).strip()
        stratum = str(task.get("stratum", "")).strip()
        if not task_id or not stratum:
            raise ValueError(
                f"corpus task {task_id or '<no id>'} is missing its"
                " task_id/stratum; re-run freeze-corpus rather than editing"
                " the corpus file by hand"
            )
        strata[task_id] = stratum
    return strata


def _record(args: argparse.Namespace) -> int:
    try:
        frozen = _load_json(args.corpus)
        manifest = _read_manifest(args.manifest)
        raw = _load_json(args.outcomes)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        return _fail(f"cannot read inputs: {exc}", EXIT_INPUT)
    entries = raw.get("outcomes", raw) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return _fail("outcomes must be a list, or an object with an 'outcomes' list",
                     EXIT_INPUT)
    if manifest.arm != args.arm:
        return _fail(
            f"manifest is for {manifest.arm} but --arm says {args.arm};"
            " recording an arm's results against another arm's conditions is"
            " the protocol violation the manifest exists to prevent",
            EXIT_INPUT,
        )
    if manifest.corpus_version != frozen.get("corpus_version"):
        return _fail(
            f"manifest pins corpus {manifest.corpus_version} but --corpus is"
            f" {frozen.get('corpus_version')}",
            EXIT_INPUT,
        )
    try:
        outcomes = [report_mod.TaskOutcome.from_dict(entry) for entry in entries]
    except (ValueError, TypeError, AttributeError) as exc:
        return _fail(f"bad outcome record: {exc}", EXIT_INPUT)

    tier, process = report_mod.ARM_SPECS[manifest.arm]
    try:
        strata = _strata_by_task(frozen)
    except ValueError as exc:
        return _fail(str(exc), EXIT_INPUT)
    run = report_mod.build_arm_run(
        arm=manifest.arm, model_tier=tier, process=process, manifest=manifest,
        outcomes=outcomes, strata_by_task=strata,
    )
    payload = run.to_dict()
    if args.out:
        _write_json(args.out, payload)

    journalled = None
    if args.journal:
        try:
            from chief_wiggum.experiment_journal import append_experiment_record

            journalled = append_experiment_record(
                args.journal, manifest.arm,
                results_digest=run.results_digest(),
                manifest_digest=manifest.digest(),
                corpus_version=manifest.corpus_version,
                verifier_hash=manifest.verifier_hash,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            return _fail(f"could not journal the record: {exc}", EXIT_ERROR)

    coverage = run.coverage
    print(json.dumps({
        "arm": manifest.arm,
        "results_digest": run.results_digest(),
        "manifest_digest": manifest.digest(),
        "coverage": coverage.to_dict(),
        "quality": run.result.quality.render(),
        "journal_record": journalled,
    }, indent=2, sort_keys=True))
    if not coverage.complete:
        print(
            f"\nPARTIAL: {coverage.attempted}/{coverage.corpus_n} corpus tasks"
            " attempted. A partial arm is reported as partial and produces no"
            " gap-closure ratio.",
            file=sys.stderr,
        )
        return EXIT_FINDING if args.gate else EXIT_OK
    return EXIT_OK


def _report(args: argparse.Namespace) -> int:
    try:
        frozen = _load_json(args.corpus)
        strata = _strata_by_task(frozen)
        runs = []
        for path in args.run:
            raw = _load_json(path)
            manifest = RunManifest(
                arm=str(raw["manifest"]["arm"]),
                corpus_version=str(raw["manifest"]["corpus_version"]),
                provider_roster=dict(raw["manifest"].get("provider_roster") or {}),
                seeds=dict(raw["manifest"].get("seeds") or {}),
                budgets=dict(raw["manifest"].get("budgets") or {}),
                verifier_hash=str(raw["manifest"]["verifier_hash"]),
                environment=dict(raw["manifest"].get("environment") or {}),
            )
            if manifest.corpus_version != frozen.get("corpus_version"):
                return _fail(
                    f"{path}: recorded against corpus"
                    f" {manifest.corpus_version} but --corpus is"
                    f" {frozen.get('corpus_version')}. `record` pins this and"
                    " `report` re-checks it: without the check the report"
                    " labels the results with a corpus they never ran"
                    " against, including its exclusion counts.",
                    EXIT_INPUT,
                )
            outcomes = [report_mod.TaskOutcome.from_dict(entry)
                        for entry in raw.get("outcomes", [])]
            tier, process = report_mod.ARM_SPECS[manifest.arm]
            run = report_mod.build_arm_run(
                arm=manifest.arm, model_tier=tier, process=process,
                manifest=manifest, outcomes=outcomes, strata_by_task=strata,
            )
            recorded = raw.get("results_digest")
            if recorded and recorded != run.results_digest():
                return _fail(
                    f"{path}: results_digest {recorded} does not match the"
                    f" records it contains ({run.results_digest()}); the file"
                    " changed after it was recorded",
                    EXIT_INPUT,
                )
            runs.append(run)
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        return _fail(f"cannot read runs: {exc}", EXIT_INPUT)

    if not runs:
        return _fail("no runs given", EXIT_INPUT)

    try:
        report = report_mod.assemble_report(
            corpus=frozen,
            runs=runs,
            non_inferiority=NonInferiority(
                margin=REGISTERED_MARGIN,
                justification=REGISTERED_MARGIN_JUSTIFICATION,
            ),
            min_n=REGISTERED_MIN_N,
        )
    except ValueError as exc:
        return _fail(str(exc), EXIT_INPUT)
    markdown = report_mod.render_report(report)
    print(markdown)
    if args.out_json:
        _write_json(args.out_json, report)
    if args.out_md:
        target = Path(args.out_md)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown)
    if args.gate and report["reporting_failures"]:
        return EXIT_FINDING
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dynamic-DAG ablation harness (chief-wiggum#391)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str, func: Any) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text)
        command.set_defaults(func=func)
        command.add_argument(
            "--gate", action="store_true",
            help="block on findings (report-only by default; docs/gate-rollout.md)",
        )
        return command

    freeze = add("freeze-corpus", "apply the inclusion rules and freeze the corpus",
                 _freeze_corpus)
    freeze.add_argument("--candidates", required=True)
    freeze.add_argument("--out", default="")
    freeze.add_argument(
        "--repo", default="",
        help="repo to ask whether a task's solution is reachable from its base."
             " Without it reachability is UNKNOWN, not clean: each task falls"
             " back to its dates, and a task whose dates cannot settle it is"
             " excluded as unverifiable rather than admitted",
    )
    freeze.add_argument("--min-stratum-n", type=int,
                        default=corpus_mod.MIN_STRATUM_N)
    freeze.add_argument("--notes", default="")

    verifier = add("hash-verifier", "content-hash the verifier before any arm runs",
                   _hash_verifier)
    verifier.add_argument("--path", action="append", required=True)
    verifier.add_argument("--root", default=".")
    verifier.add_argument("--command", default="")
    verifier.add_argument("--out", default="")

    manifest = add("manifest", "pin and hash one arm's conditions", _manifest)
    manifest.add_argument("--arm", required=True)
    manifest.add_argument("--corpus", required=True)
    manifest.add_argument("--verifier", required=True)
    manifest.add_argument("--roster", action="append",
                          help="role=provider, repeatable")
    manifest.add_argument("--seed", action="append", help="name=int, repeatable")
    manifest.add_argument("--budget", action="append",
                          help="name=int (cents, whole seconds), repeatable")
    manifest.add_argument("--env", action="append", help="key=value, repeatable")
    manifest.add_argument("--out", default="")

    protocol = add("check-protocol",
                   "prove every arm ran under the same conditions", _check_protocol)
    protocol.add_argument("--manifest", action="append", required=True)

    record = add("record", "fold an arm's raw outcomes in and journal the digest",
                 _record)
    record.add_argument("--arm", required=True)
    record.add_argument("--corpus", required=True)
    record.add_argument("--manifest", required=True)
    record.add_argument("--outcomes", required=True)
    record.add_argument("--out", default="")
    record.add_argument("--journal", default="",
                        help="ratchet journal to append the record digest to")

    report = add("report", "assemble the result, or refuse to", _report)
    report.add_argument("--corpus", required=True)
    report.add_argument("--run", action="append", required=True)
    report.add_argument("--out-json", default="")
    report.add_argument("--out-md", default="")

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
