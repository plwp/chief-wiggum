#!/usr/bin/env python3
"""adopt.py — brownfield adoption mechanics for /adopt (chief-wiggum#215).

The missing entry arrow for repos CW didn't build. Deliberately LIGHTWEIGHT:
survey, elect, baseline, grandfather, record — it never infers contracts
(contract inference is deferred; trigger and posture in docs/adopt.md).

Subcommands mirror the adoption sequence:

- ``survey``       shape survey (age, size, languages, test presence + a REAL
                   coverage-baseline attempt, CI presence) and a persisted
                   per-gate applicability verdict (applicable / report-only /
                   inapplicable) for the 7 shipped gates + the debt inventory.
                   Written to ``<meta root>/adoption/survey.json`` (stamped
                   with ``target_sha``).
- ``elect``        footprint-mode election via ``artifacts.elect``. Defaults:
                   ``sidecar`` + whole-repo scope; ``--scope-from-codeowners``
                   seeds ``scope.json`` from CODEOWNERS paths when present
                   (skip-with-note when absent).
- ``baseline``     (a) ``ratchet.py init``-equivalent + a REAL test run for
                   the pass-set baseline (never ``--no-tests``), journaled as
                   a merged ``baseline`` record; quality dimensions baseline
                   at current values (``score`` handles this). (b) the #214
                   debt inventory, written to the resolver quality dir.
- ``grandfather``  ``<meta root>/adoption/grandfathered.json`` from the
                   current debt.json + current gate findings — entries modeled
                   on the JUSTIFIED-waiver shape (reason/owner/expiry). Expiry
                   is VISIBLE PRESSURE, not amnesty: grandfathered findings
                   stay in the inventory, labeled; only NEW findings are
                   gate-eligible. Re-running against an EXISTING
                   grandfathered.json REFUSES by default (a second sweep would
                   amnesty POST-adoption findings) and prints the exact delta;
                   ``--extend`` performs it explicitly and loudly, preserving
                   the original ``created_at`` and the original entries'
                   expiry (only NEW entries get a fresh expiry).
- ``record``       ``<meta root>/adoption/adoption.json`` — THE brownfield
                   switch (#216 reads it; /architect reads it in place of the
                   ``IS_NEW_PRODUCT`` file-existence heuristic).
- ``run``          the whole sequence, printing each step. The election runs
                   first (it decides where the survey persists); then survey →
                   baseline → grandfather → record. A target whose
                   adoption.json already exists refuses unless ``--re-adopt``
                   — re-adoption is an explicit operator act, never a side
                   effect of re-running the arrow.

Standalone ``survey``/``baseline``/``grandfather``/``record`` require a prior
footprint election (``adopt elect`` / ``adopt run``): with no election file the
resolver silently defaults to EMBEDDED and would write the target's own tree —
embedded mode must be an explicit choice, so the un-elected case refuses
(exit 2) instead of guessing.

The coverage-baseline attempt is honest by construction: it runs the test
command the way ``run_verification.py`` detects it and parses pass/fail counts
from the runner's own output — counts it cannot parse are reported as
unparsed, never fabricated; no detected runner is stated as exactly that.

/adopt never writes the target tree in sidecar mode: survey/grandfather/record
artifacts live under the resolver meta root, junit reports are re-pointed to
the workdir, and every test run (the survey's coverage attempt AND the
baseline's ratchet score) suppresses Python bytecode and the pytest cache via
the CHILD ENVIRONMENT — ``PYTHONDONTWRITEBYTECODE=1`` plus
``PYTEST_ADDOPTS="-p no:cacheprovider"`` — so the suppression reaches pytest
even when the suite command goes through ``make``/a wrapper script.

Usage:
    python3 scripts/adopt.py survey  [owner/repo] [--repo PATH] [--format text|json]
    python3 scripts/adopt.py elect   [owner/repo] [--repo PATH] [--mode sidecar|embedded]
                                     [--backing local|git] [--scope-from-codeowners]
    python3 scripts/adopt.py baseline [owner/repo] [--repo PATH] [--workdir DIR]
    python3 scripts/adopt.py grandfather [owner/repo] [--repo PATH] [--extend]
                                     [--expiry YYYY-MM-DD | --expiry-days N] [--owner NAME]
    python3 scripts/adopt.py record  [owner/repo] [--repo PATH]
    python3 scripts/adopt.py run     [owner/repo] [--repo PATH] [--re-adopt] [...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifacts  # noqa: E402 — #213 meta-location resolver
import debt_inventory  # noqa: E402 — #214 inventory (the debt baseline)
import ratchet  # noqa: E402 — pass-set/quality baseline + journal
from chief_wiggum import verification  # noqa: E402 — test-command detection
from chief_wiggum.hashing import hash_epic_definitions  # noqa: E402
from quality import population  # noqa: E402 — the shared language population

SURVEY_SCHEMA = "adoption-survey/1"
GRANDFATHER_SCHEMA = "grandfather/1"
ADOPTION_SCHEMA = "adoption/1"
ADOPTION_DIRNAME = "adoption"
SURVEY_NAME = "survey.json"
GRANDFATHER_NAME = "grandfathered.json"
ADOPTION_NAME = "adoption.json"
DEFAULT_EXPIRY_DAYS = 90
DEFAULT_OWNER = "unassigned"

# The 7 gates with shipped validation records (docs/quality/validation/) whose
# applicability the survey rules on; debt_inventory joins them as the eighth
# verdict (report-only by design, no validation record yet).
SHIPPED_GATES = (
    "check_traceability",
    "check_single_writer",
    "check_architecture",
    "ratchet",
    "ci_scaffold",
    "quality_slop_gate",
    "saas_gate",
)

CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
CODEOWNERS_CATCH_ALL = {"*", "**", "/**"}

CI_MARKERS = (
    ".gitlab-ci.yml", ".circleci/config.yml", "Jenkinsfile",
    "azure-pipelines.yml", ".buildkite",
)


class AdoptError(Exception):
    """Usage/state problem. Maps to exit 2."""


def _git(target: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(target), *args],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def adoption_dir(resolver: artifacts.Resolver) -> Path:
    return resolver.meta_root / ADOPTION_DIRNAME


def _require_election(target: Path) -> artifacts.Resolver:
    """Resolver for a target that HAS a footprint election (F4 on #215).

    Standalone survey/baseline/grandfather/record must never fall through to
    the resolver's silent embedded default and write the target's own tree:
    with no election file they refuse (exit 2) — embedded is a choice the
    operator records (``adopt elect --mode embedded``), never a fallback.
    ``elect`` and ``run`` are unaffected (they ARE the electing steps).
    """
    target_id = artifacts.derive_target_id(target)
    if artifacts.load_election(target_id) is None:
        raise AdoptError(
            "no footprint election for this target — run `adopt elect` "
            "(or `adopt run`) first; embedded mode is an explicit choice")
    return artifacts.Resolver.resolve(target)


@contextmanager
def _no_cache_child_env():
    """Suppress Python bytecode + the pytest cache for CHILD processes (F3 on
    #215): ``PYTHONDONTWRITEBYTECODE=1`` and ``PYTEST_ADDOPTS`` gaining
    ``-p no:cacheprovider`` in ``os.environ`` for the duration. Environment-
    based on purpose — ratchet's ``run_suite`` inherits the parent env
    (``shell=True``), so this reaches pytest even when the suite cmd is
    ``make test`` or a wrapper script, where flag-appending cannot."""
    saved = {k: os.environ.get(k) for k in ("PYTHONDONTWRITEBYTECODE", "PYTEST_ADDOPTS")}
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTEST_ADDOPTS"] = (
        (saved["PYTEST_ADDOPTS"] or "") + " -p no:cacheprovider"
    ).strip()
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


# --- shape survey ----------------------------------------------------------------


def repo_age(target: Path) -> dict:
    """First-commit date + age in days. ``git log`` is newest-first, so the
    LAST line is the root commit's date."""
    log = _git(target, "log", "--format=%cI")
    if not log:
        return {"first_commit": None, "age_days": None}
    first = log.splitlines()[-1].strip()
    try:
        dt = datetime.fromisoformat(first)
        age_days = (datetime.now(timezone.utc) - dt).days
    except ValueError:
        age_days = None
    return {"first_commit": first, "age_days": age_days}


def repo_size(target: Path) -> dict:
    """Tracked-file count + per-language source counts, via the SAME
    ``quality.population`` the debt engines scan — one definition of 'which
    files are in play', not a fresh heuristic."""
    tracked = _git(target, "ls-files")
    tracked_count = len(tracked.splitlines()) if tracked else 0
    source = population.tracked_source(str(target))
    langs: dict[str, int] = {}
    for f in source:
        lang = population.lang_of(f)
        if lang:
            langs[lang] = langs.get(lang, 0) + 1
    return {
        "tracked_files": tracked_count,
        "source_files": len(source),
        "languages": langs,
        "unknown_extensions": population.unknown_language_files(str(target)),
    }


def test_presence(target: Path) -> dict:
    """Per-language test-file counts (``population.is_test_file``)."""
    counts: dict[str, int] = {}
    for f in population.tracked_source(str(target)):
        if population.is_test_file(f):
            lang = population.lang_of(f) or "unknown"
            counts[lang] = counts.get(lang, 0) + 1
    return {"test_files": counts, "total": sum(counts.values())}


_PYTEST_COUNT_RE = {
    "passed": re.compile(r"(\d+) passed"),
    "failed": re.compile(r"(\d+) failed"),
    "errors": re.compile(r"(\d+) error"),
}


def _parse_test_counts(tool: str, output: str) -> tuple[int | None, int | None, str | None]:
    """(passed, failed, note) parsed from the runner's OWN output — parsed or
    honestly declared unparsed, never fabricated."""
    if tool == "python":
        p = _PYTEST_COUNT_RE["passed"].search(output)
        f = _PYTEST_COUNT_RE["failed"].search(output)
        e = _PYTEST_COUNT_RE["errors"].search(output)
        if p or f or e:
            failed = (int(f.group(1)) if f else 0) + (int(e.group(1)) if e else 0)
            return (int(p.group(1)) if p else 0), failed, None
        if "no tests ran" in output:
            return 0, 0, None
        return None, None, "pass/fail counts unparsed from pytest output — see log tail"
    if tool == "go":
        ok = len(re.findall(r"(?m)^ok\s", output))
        fail = len(re.findall(r"(?m)^FAIL\s", output))
        if ok or fail:
            return ok, fail, "package-level counts (go test without -json)"
        return None, None, "pass/fail counts unparsed from go test output — see log tail"
    return None, None, f"pass/fail counts not parseable for tool {tool!r} — see log tail"


def coverage_baseline(target: Path) -> dict:
    """A REAL run of the test command the way ``run_verification.py`` detects
    it (``chief_wiggum.verification`` detection + planning), with pass/fail
    counts parsed from the output. No detected runner is SAID, not papered
    over. The run is sandboxed against tree writes in the CHILD ENV — Python
    bytecode and the pytest cache are suppressed via PYTHONDONTWRITEBYTECODE
    + PYTEST_ADDOPTS, which reaches pytest even through make/wrappers (the
    target tree must stay clean)."""
    detection = verification.detect_project(target)
    planned = verification.plan_steps(target, ["test"], detection)
    if not planned:
        return {
            "detected": False,
            "steps": [],
            "note": "no test runner detected — cannot establish a coverage "
                    "baseline; configure a suite in ratchet.json by hand "
                    "(detection: chief_wiggum.verification.detect_project)",
        }
    steps: list[dict] = []
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": (
            (os.environ.get("PYTEST_ADDOPTS") or "") + " -p no:cacheprovider"
        ).strip(),
    }
    for step in planned:
        cmd = list(step.command)
        try:
            proc = subprocess.run(
                cmd, cwd=step.cwd, capture_output=True, text=True,
                timeout=1800, env=env,
            )
            exit_code: int | None = proc.returncode
            output = (proc.stdout or "") + (proc.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as exc:
            exit_code, output = None, f"runner error: {exc}"
        passed, failed, note = _parse_test_counts(step.tool, output)
        steps.append({
            "tool": step.tool,
            "command": " ".join(cmd),
            "exit_code": exit_code,
            "ok": exit_code == 0,
            "passed": passed,
            "failed": failed,
            "note": note,
            "log_tail": "\n".join(output.splitlines()[-15:]),
        })
    return {"detected": True, "detection": detection.to_dict(), "steps": steps}


def ci_presence(target: Path) -> dict:
    workflows = sorted(
        p.name for p in (target / ".github" / "workflows").glob("*.y*ml")
    ) if (target / ".github" / "workflows").is_dir() else []
    other = [m for m in CI_MARKERS if (target / m).exists()]
    return {
        "present": bool(workflows or other),
        "github_workflows": workflows,
        "other": other,
    }


def _has_single_writer_invariants(epics_dir: Path) -> bool:
    """Any epic artifact declaring a single-write-path invariant
    (``controls_field`` in a model JSON, or an ``@cw-writes`` tag in
    invariants.md) — the applicability trigger for check_single_writer."""
    if not epics_dir.is_dir():
        return False
    for p in epics_dir.rglob("*"):
        try:
            if p.suffix == ".json" and "controls_field" in p.read_text():
                return True
            if p.suffix == ".md" and "@cw-writes" in p.read_text():
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


def _has_architecture_model(target: Path, resolver: artifacts.Resolver) -> bool:
    return (
        (target / "docs" / "system" / "architecture.json").is_file()
        or (resolver.meta_root / "system" / "architecture.json").is_file()
    )


def gate_verdicts(
    *,
    has_epic_ids: bool,
    has_single_writer_invariants: bool,
    has_architecture_model: bool,
    suites: list[dict],
    ci_present: bool,
    source_files: int,
    unknown_extensions: dict[str, int],
    age_days: int | None,
) -> dict[str, dict]:
    """Per-gate applicability verdict — applicable / report-only /
    inapplicable, each with a one-line reason. Pure function of survey facts
    so the rules are unit-testable."""
    v: dict[str, dict] = {}

    if has_epic_ids:
        v["check_traceability"] = {
            "verdict": "applicable",
            "reason": "epic contracts/invariants define stable IDs — the coverage graph applies",
        }
    else:
        v["check_traceability"] = {
            "verdict": "report-only",
            "reason": "no epic contracts yet — runs report `inapplicable`, never green; "
                      "becomes applicable when /architect authors the first epic",
        }

    if has_single_writer_invariants:
        v["check_single_writer"] = {
            "verdict": "applicable",
            "reason": "single-write-path invariants declared (controls_field/@cw-writes) — "
                      "writer inventory applies",
        }
    else:
        v["check_single_writer"] = {
            "verdict": "inapplicable",
            "reason": "no single-write-path invariants declared — reports `inapplicable`, "
                      "not green; applicable once an epic names controls_field + "
                      "sanctioned_writers",
        }

    if has_architecture_model:
        v["check_architecture"] = {
            "verdict": "applicable",
            "reason": "a declared architecture model (docs/system/architecture.json) exists — "
                      "static consistency checks apply",
        }
    else:
        v["check_architecture"] = {
            "verdict": "inapplicable",
            "reason": "no declared architecture model (docs/system/architecture.json) — "
                      "nothing to check until one is authored",
        }

    if suites:
        names = ", ".join(s.get("name", "?") for s in suites)
        v["ratchet"] = {
            "verdict": "applicable",
            "reason": f"test runner detected ({names}) — applicable once baselined "
                      "(`adopt baseline` records the real pass-set)",
        }
    else:
        v["ratchet"] = {
            "verdict": "report-only",
            "reason": "no test runner detected — the pass-set has nothing to hold; add a "
                      "suite to ratchet.json by hand (the contract-hash ratchet activates "
                      "with the first epic)",
        }

    if ci_present:
        v["ci_scaffold"] = {
            "verdict": "applicable",
            "reason": "CI configuration present — the scaffold/report checks apply",
        }
    else:
        v["ci_scaffold"] = {
            "verdict": "report-only",
            "reason": "no CI configuration detected — report-only until a workflow exists "
                      "(ci_scaffold --scaffold can write one)",
        }

    if age_days is None:
        # F9 (#215): "git history present" may only be claimed when a commit
        # actually exists — an empty/unreadable history has no survival or
        # duplication signal to compute.
        v["quality_slop_gate"] = {
            "verdict": "report-only",
            "reason": "no readable commit history — survival/duplication signals need "
                      "git history; report-only until the first commit exists",
        }
    else:
        slop_reason = "git history present — report-only survival/duplication signals computable"
        if age_days < 14:
            slop_reason += f" (repo is {age_days} day(s) old — 2-week survival self-skips until "
            slop_reason += "history reaches 14 days)"
        v["quality_slop_gate"] = {"verdict": "applicable", "reason": slop_reason}

    v["saas_gate"] = {
        "verdict": "inapplicable",
        "reason": "requires a deployed base URL (--base-url) — no deployment is knowable "
                  "from the tree; elect when one exists",
    }

    if source_files:
        v["debt_inventory"] = {
            "verdict": "applicable",
            "reason": f"{source_files} known-language source file(s) — the four debt engines "
                      "apply (report-only by design)",
        }
    else:
        uns = ", ".join(f"{k}: {n}" for k, n in sorted(unknown_extensions.items())) or "none"
        v["debt_inventory"] = {
            "verdict": "report-only",
            "reason": "no known-language source files — the engines have nothing to scan "
                      f"(unscanned extensions: {uns}); absence of findings is NOT health",
        }
    return v


def build_survey(target: Path, resolver: artifacts.Resolver) -> dict:
    age = repo_age(target)
    size = repo_size(target)
    tests = test_presence(target)
    run = coverage_baseline(target)
    ci = ci_presence(target)
    epics_dir = resolver.epics_dir()
    has_epic_ids = bool(epics_dir.is_dir() and hash_epic_definitions(epics_dir))
    suites = ratchet.detect_suites(target)
    gates = gate_verdicts(
        has_epic_ids=has_epic_ids,
        has_single_writer_invariants=_has_single_writer_invariants(epics_dir),
        has_architecture_model=_has_architecture_model(target, resolver),
        suites=suites,
        ci_present=ci["present"],
        source_files=size["source_files"],
        unknown_extensions=size["unknown_extensions"],
        age_days=age["age_days"],
    )
    return resolver.stamp({
        "schema": SURVEY_SCHEMA,
        "generated_at": _now_iso(),
        "age": age,
        "size": size,
        "tests": tests,
        "test_run": run,
        "ci": ci,
        "detected_suites": suites,
        "gates": gates,
    })


def format_survey(doc: dict) -> str:
    lines = ["## Adoption survey"]
    age = doc["age"]
    size = doc["size"]
    lines.append(
        f"age: {age['age_days']} day(s) (first commit {age['first_commit']}); "
        f"{size['tracked_files']} tracked file(s), {size['source_files']} source"
    )
    if size["languages"]:
        lines.append("languages: " + ", ".join(
            f"{k}: {n}" for k, n in sorted(size["languages"].items())))
    if size["unknown_extensions"]:
        lines.append("unscanned extensions: " + ", ".join(
            f"{k}: {n}" for k, n in sorted(size["unknown_extensions"].items())))
    tf = doc["tests"]["test_files"]
    lines.append("test files: " + (", ".join(
        f"{k}: {n}" for k, n in sorted(tf.items())) if tf else "none"))
    run = doc["test_run"]
    if not run["detected"]:
        lines.append(f"coverage baseline: {run['note']}")
    else:
        for s in run["steps"]:
            counts = (
                f"{s['passed']} passed, {s['failed']} failed"
                if s["passed"] is not None else (s["note"] or "counts unparsed")
            )
            lines.append(
                f"coverage baseline [{s['tool']}]: exit {s['exit_code']} — {counts}")
    ci = doc["ci"]
    lines.append("CI: " + ("present (" + ", ".join(ci["github_workflows"] + ci["other"]) + ")"
                           if ci["present"] else "none detected"))
    lines.append("")
    lines.append("Per-gate applicability:")
    for gate, verdict in sorted(doc["gates"].items()):
        lines.append(f"- {gate}: {verdict['verdict']} — {verdict['reason']}")
    return "\n".join(lines)


def cmd_survey(args) -> int:
    target = Path(resolve_target(args.owner_repo, args.repo))
    resolver = _require_election(target)
    doc = build_survey(target, resolver)
    out = adoption_dir(resolver) / SURVEY_NAME
    _write_json(out, doc)
    if args.format == "json":
        print(json.dumps(doc, indent=2))
    else:
        print(format_survey(doc))
        print(f"\nadopt: survey written to {out}")
    return 0


# --- elect -----------------------------------------------------------------------


def codeowners_scope(target: Path) -> tuple[list[str] | None, str | None]:
    """(include-globs, source-path) from CODEOWNERS, or (None, None) when no
    CODEOWNERS file exists.

    Documented mapping (CODEOWNERS gitignore-ish patterns -> scope.json
    fnmatch globs; Python fnmatch's ``*`` crosses ``/``):

    - a leading ``/`` (repo-root anchor) is stripped;
    - a trailing ``/`` (directory) becomes ``<dir>/*``;
    - bare patterns (``*.proto``) are kept as-is — fnmatch matches them at any
      depth;
    - catch-alls (``*``, ``**``, ``/**``) are skipped: they mean whole-repo
      ownership, which is the scope default anyway (an empty result keeps
      whole-repo scope).
    """
    rel_found = None
    for rel in CODEOWNERS_LOCATIONS:
        if (target / rel).is_file():
            rel_found = rel
            break
    if rel_found is None:
        return None, None
    globs: list[str] = []
    for line in (target / rel_found).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pattern = line.split()[0]
        if pattern in CODEOWNERS_CATCH_ALL:
            continue
        g = pattern.lstrip("/")
        if g.endswith("/"):
            g = g + "*"
        if g and g not in globs:
            globs.append(g)
    return globs, rel_found


def cmd_elect(args) -> int:
    target = Path(resolve_target(args.owner_repo, args.repo))
    record = artifacts.elect(target, args.mode, backing=args.backing)
    resolver = artifacts.Resolver.resolve(target)
    print(f"adopt: elected mode={record['mode']} backing={record['backing']} "
          f"for {record['target_id']} — meta root: {resolver.meta_root}")
    if args.scope_from_codeowners:
        globs, src = codeowners_scope(target)
        if globs is None:
            print("adopt: no CODEOWNERS found (.github/, repo root, docs/) — "
                  "scope stays whole-repo")
        elif not globs:
            print("adopt: CODEOWNERS carries only catch-all patterns — "
                  "whole-repo scope kept (nothing to narrow to)")
        else:
            scope = {
                "include": globs,
                "$comment": (
                    f"seeded from {src} by adopt.py --scope-from-codeowners; mapping: "
                    "leading '/' stripped, trailing '/' -> '<dir>/*', bare patterns "
                    "kept (fnmatch semantics — '*' crosses '/'), catch-alls skipped"
                ),
            }
            _write_json(resolver.scope_path(), scope)
            print(f"adopt: scope.json seeded from {src}: include = {globs}")
    else:
        print(f"adopt: scope: {resolver.scope_summary()}")
    return 0


# --- baseline --------------------------------------------------------------------


def _retarget_suite_reports(cfg_path: Path, workdir: Path) -> None:
    """Point autodetected junit reports OUTSIDE the target tree (the baseline
    run must not write the tree being adopted), and suppress pytest's cache
    dir. Only repo-relative report paths whose token appears verbatim in the
    suite cmd are re-pointed — a hand-authored suite is never second-guessed."""
    cfg = json.loads(cfg_path.read_text())
    changed = False
    for suite in cfg.get("suites", []):
        report = suite.get("report")
        if (suite.get("parser") == "junit-xml" and report
                and not os.path.isabs(report) and report in suite.get("cmd", "")):
            new = str(workdir / f"{suite.get('name', 'suite')}-junit.xml")
            suite["cmd"] = suite["cmd"].replace(report, f'"{new}"')
            suite["report"] = new
            changed = True
        if "pytest" in suite.get("cmd", "") and "no:cacheprovider" not in suite["cmd"]:
            suite["cmd"] += " -p no:cacheprovider"
            changed = True
    if changed:
        workdir.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")


def _workdir(args, target: Path) -> Path:
    if args.workdir:
        return Path(args.workdir).expanduser()
    import env  # session temp dir under ~/.chief-wiggum/tmp — never the target repo
    return Path(str(env.create_tmp())) / "adopt" / target.name


def cmd_baseline(args) -> int:
    target = Path(resolve_target(args.owner_repo, args.repo))
    resolver = _require_election(target)
    workdir = _workdir(args, target)
    workdir.mkdir(parents=True, exist_ok=True)

    # (a) ratchet: init (module call), re-point junit reports out of the tree,
    # then a REAL scored run — never --no-tests when suites exist: the pass-set
    # baseline is the honest outcome of running the detected suites, whatever
    # that outcome is. When NO suite exists (or none yields a single case), an
    # empty pass-set must NOT be journaled as a real test run (F7 on #215):
    # score falls back to --no-tests semantics (tests_run: false) and the
    # journal note says exactly that.
    print("adopt: [baseline] ratchet init...")
    rc = ratchet.cmd_init(argparse.Namespace(repo=str(target), force=False))
    if rc != 0:
        raise AdoptError(f"ratchet init failed (exit {rc})")
    cfg_path = resolver.quality_dir() / ratchet.CONFIG_NAME
    _retarget_suite_reports(cfg_path, workdir)
    no_suites = not ratchet.load_config(target).suites
    if no_suites:
        print("adopt: [baseline] no test suites detected — scoring with "
              "--no-tests semantics (tests_run: false)...")
    else:
        print("adopt: [baseline] ratchet score (real test run — never --no-tests)...")

    def _score(no_tests: bool) -> None:
        with _no_cache_child_env():
            rc = ratchet.cmd_score(argparse.Namespace(
                repo=str(target), no_tests=no_tests, no_quality=False,
                venv=None, gobin=None))
        if rc != 0:
            raise AdoptError(f"ratchet score failed (exit {rc})")

    _score(no_tests=no_suites)
    empty_baseline = no_suites
    if not no_suites:
        sc = json.loads((resolver.quality_dir() / ratchet.SCORECARD_NAME).read_text())
        if not sc.get("pass_set"):
            # Suites were configured but produced ZERO passing cases — an
            # empty pass-set journaled as tests_run would look like a real,
            # honest baseline. Re-score as not-run and say so.
            _score(no_tests=True)
            empty_baseline = True
    notes = "adoption baseline"
    if empty_baseline:
        print("adopt: no test suites ran — pass-set baseline EMPTY (recorded as not-run)")
        survey = _load_json(adoption_dir(resolver) / SURVEY_NAME)
        if survey and not (survey.get("test_run") or {}).get("detected", True):
            # Survey verdict cross-check: the survey already said no runner —
            # the baseline agrees rather than silently diverging.
            print("adopt: consistent with the survey verdict — no test runner detected")
        notes = ("adoption baseline — no test suites ran; pass-set EMPTY, "
                 "recorded as not-run (tests_run: false)")
    rc = ratchet.cmd_record(argparse.Namespace(
        repo=str(target), event="baseline", ref="adoption", gate="pass",
        merged=True, notes=notes,
        amend=None, retire=None, amend_verifier=None, retire_verifier=None))
    if rc != 0:
        raise AdoptError(f"ratchet record failed (exit {rc})")
    cfg = ratchet.load_config(target)
    rid = ratchet.load_journal(cfg)[-1]["record_id"]

    # (b) debt inventory baseline (#214) into the resolver quality dir.
    print("adopt: [baseline] debt inventory...")
    out_path = resolver.quality_dir() / "debt.json"
    envelope = debt_inventory.build_inventory(
        str(target), str(workdir / "debt"), out_path, resolver=resolver)
    out_path.write_text(json.dumps(envelope, indent=2) + "\n")
    debt_sha = hashlib.sha256(out_path.read_bytes()).hexdigest()

    sc = json.loads((resolver.quality_dir() / ratchet.SCORECARD_NAME).read_text())
    print(f"adopt: baseline recorded — ratchet {rid} "
          f"({len(sc.get('pass_set', []))} passing case(s)); "
          f"debt.json {len(envelope['items'])} item(s) (sha256 {debt_sha[:12]}…)")
    return 0


# --- grandfather -----------------------------------------------------------------


def _gate_finding_entries(resolver: artifacts.Resolver, target: Path,
                          owner: str, expiry: str) -> list[dict]:
    """Grandfather entries for CURRENT gate findings beyond the debt
    inventory: traceability coverage gaps and single-writer violations, when
    epics exist. On a freshly-adopted brownfield repo these are typically
    empty (no epics -> both gates inapplicable); best-effort by design — a
    gate that cannot run must never block adoption."""
    entries: list[dict] = []
    epics = resolver.epics_dir()
    if not epics.is_dir():
        return entries
    try:
        import check_single_writer  # noqa: PLC0415
        import check_traceability as ct  # noqa: PLC0415
        for epic_dir in sorted(p for p in epics.iterdir() if p.is_dir()):
            report = ct.check(epic_dir, target)
            # Key format consumed by check_traceability's grandfather reading
            # (chief_wiggum.grandfather):
            #   check_traceability:uncovered:<STABLE-ID>
            #   check_traceability:untested:<STABLE-ID>
            for cid in report.uncovered_contracts:
                entries.append(_entry(f"check_traceability:uncovered:{cid}",
                                      owner, expiry, "check_traceability"))
            for cid in report.untested_contracts:
                entries.append(_entry(f"check_traceability:untested:{cid}",
                                      owner, expiry, "check_traceability"))
            sw = check_single_writer.check(epic_dir, target)
            for v in sw.violations:
                # Key format consumed by check_single_writer's grandfather
                # reading (chief_wiggum.grandfather):
                #   check_single_writer:<INV-id>:<field>:<file>
                key = ":".join(str(v.get(k, "?")) for k in ("invariant_id", "field", "file"))
                entries.append(_entry(f"check_single_writer:{key}",
                                      owner, expiry, "check_single_writer"))
    except Exception as exc:  # noqa: BLE001 — surfaced, never adoption-blocking
        print(f"adopt: note — gate-finding sweep skipped ({exc})", file=sys.stderr)
    return entries


def _entry(finding_id: str, owner: str, expiry: str, engine: str) -> dict:
    # Modeled on the JUSTIFIED-waiver shape (chief_wiggum.trace_links):
    # reason/owner/expiry per entry, scoped to a finding ID.
    return {
        "id": finding_id,
        "reason": "pre-adoption baseline",
        "owner": owner,
        "expiry": expiry,
        "source_engine": engine,
    }


def cmd_grandfather(args) -> int:
    target = Path(resolve_target(args.owner_repo, args.repo))
    resolver = _require_election(target)
    debt_path = resolver.quality_dir() / "debt.json"
    debt = _load_json(debt_path)
    if debt is None:
        raise AdoptError(
            f"no debt inventory at {debt_path} — run `adopt.py baseline` first "
            "(the grandfather file waives the BASELINE, not a guess)")
    if args.expiry:
        try:
            date.fromisoformat(args.expiry)
        except ValueError as exc:
            raise AdoptError(f"--expiry must be an ISO date (YYYY-MM-DD): {exc}") from exc
        expiry = args.expiry
    else:
        expiry = (date.today() + timedelta(days=args.expiry_days)).isoformat()

    entries = [
        _entry(item["id"], args.owner, expiry, item.get("engine", "unknown"))
        for item in (debt.get("items") or [])
        if isinstance(item, dict) and item.get("id")
    ]
    entries += _gate_finding_entries(resolver, target, args.owner, expiry)

    out = adoption_dir(resolver) / GRANDFATHER_NAME
    existing = _load_json(out)
    body = resolver.stamp({
        "schema": GRANDFATHER_SCHEMA,
        "created_at": _now_iso(),
        "default_expiry": expiry,
        "entries": entries,
    })
    amnestied = 0
    if existing is not None:
        # F1 (#215): re-grandfathering is AMNESTY for every finding that
        # appeared since adoption. Refuse by default, printing the exact
        # delta; --extend performs it explicitly and loudly, preserving the
        # ORIGINAL created_at and the original entries' expiry (only the NEW
        # entries get a fresh expiry from now).
        old_entries = [e for e in (existing.get("entries") or [])
                       if isinstance(e, dict) and e.get("id")]
        old_ids = {e["id"] for e in old_entries}
        new_entries = [e for e in entries if e["id"] not in old_ids]
        new_ids = sorted(e["id"] for e in new_entries)
        if not getattr(args, "extend", False):
            raise AdoptError(
                f"grandfathered.json already exists at {out} — refusing to "
                "re-grandfather (a second sweep would amnesty POST-adoption "
                f"findings). {len(new_ids)} finding(s) would be newly added: "
                f"{', '.join(new_ids) or '(none)'}. Re-run with --extend to "
                "amnesty them explicitly.")
        print(f"adopt: --extend amnestying {len(new_ids)} POST-adoption "
              f"finding(s) (new-entry expiry {expiry}): "
              f"{', '.join(new_ids) or '(none)'}")
        amnestied = len(new_ids)
        body = resolver.stamp({
            "schema": GRANDFATHER_SCHEMA,
            "created_at": existing.get("created_at"),  # original, preserved
            "extended_at": _now_iso(),
            "default_expiry": existing.get("default_expiry"),
            "entries": old_entries + new_entries,  # original expiries kept
        })
    _write_json(out, body)
    if existing is None:
        print(f"adopt: grandfathered {len(entries)} finding(s) (expiry {expiry}) -> {out}")
    else:
        print(f"adopt: grandfathered {len(body['entries'])} finding(s) "
              f"({amnestied} newly amnestied) -> {out}")
    print("adopt: expiry is visible pressure, not amnesty — grandfathered items stay "
          "in the inventory, labeled; only NEW findings are gate-eligible")
    return 0


# --- record ----------------------------------------------------------------------


def _last_baseline_rid(target: Path) -> str | None:
    try:
        cfg = ratchet.load_config(target)
        for rec in reversed(ratchet.load_journal(cfg)):
            if rec.get("event") == "baseline":
                return rec.get("record_id")
    except (ratchet.RatchetError, ratchet.TamperError):
        return None
    return None


def _nearest_expiry(entries: list[dict]) -> str | None:
    expiries = sorted(e.get("expiry") for e in entries if e.get("expiry"))
    return expiries[0] if expiries else None


def cmd_record(args) -> int:
    target = Path(resolve_target(args.owner_repo, args.repo))
    resolver = _require_election(target)
    adir = adoption_dir(resolver)
    survey = _load_json(adir / SURVEY_NAME)
    if survey is None:
        raise AdoptError(
            f"no survey at {adir / SURVEY_NAME} — run `adopt.py survey` first "
            "(the adoption record snapshots the survey verdicts)")

    debt_path = resolver.quality_dir() / "debt.json"
    debt = _load_json(debt_path)
    baseline = {
        "ratchet_record_id": _last_baseline_rid(target),
        "debt_sha256": (
            hashlib.sha256(debt_path.read_bytes()).hexdigest()
            if debt_path.is_file() else None
        ),
        "debt_items": len(debt.get("items", [])) if debt else None,
    }
    gf = _load_json(adir / GRANDFATHER_NAME)
    grandfather = None
    if gf is not None:
        grandfather = {
            "file": str(adir / GRANDFATHER_NAME),
            "entries": len(gf.get("entries") or []),
            "nearest_expiry": _nearest_expiry(gf.get("entries") or []),
        }

    body = resolver.stamp({
        "schema": ADOPTION_SCHEMA,
        "adopted_at": _now_iso(),
        # THE brownfield switch: /architect reads it in place of IS_NEW_PRODUCT;
        # #216 keys scope discipline off it. A property of the REPO, not a ticket.
        "brownfield": True,
        "mode": resolver.mode,
        "backing": resolver.backing,
        "scope": resolver.scope_summary(),
        "gates": survey.get("gates", {}),
        "baseline": baseline,
        "grandfather": grandfather,
    })
    out = adir / ADOPTION_NAME
    _write_json(out, body)
    print(f"adopt: adoption record written -> {out}")
    print(f"adopt: brownfield=true mode={resolver.mode} "
          f"ratchet={baseline['ratchet_record_id']} "
          f"grandfathered={grandfather['entries'] if grandfather else 0}")
    return 0


# --- run (the whole sequence) ----------------------------------------------------


def cmd_run(args) -> int:
    # F1 (#215): re-running the whole arrow on an already-adopted target is a
    # re-adoption — an explicit operator act, never a side effect. The check
    # uses the CURRENT resolver state (an adopted target has an election, so
    # the meta root resolves to where the record actually lives).
    target = Path(resolve_target(args.owner_repo, args.repo))
    rec = _load_json(adoption_dir(artifacts.Resolver.resolve(target)) / ADOPTION_NAME)
    if rec is not None and not getattr(args, "re_adopt", False):
        raise AdoptError(
            f"already adopted (adopted_at {rec.get('adopted_at')}) — "
            "re-adoption is an explicit operator act; re-run with --re-adopt "
            "(and `grandfather --extend` semantics if you mean to amnesty "
            "post-adoption findings)")
    # The election comes FIRST: it decides where every subsequent artifact
    # (survey included) persists. Surveying before electing would write the
    # survey to the embedded default — i.e. into the very target tree a
    # sidecar adoption exists to keep clean.
    steps = (
        ("elect", cmd_elect),
        ("survey", cmd_survey),
        ("baseline", cmd_baseline),
        ("grandfather", cmd_grandfather),
        ("record", cmd_record),
    )
    for name, fn in steps:
        print(f"\n=== adopt: {name} ===")
        rc = fn(args)
        if rc != 0:
            print(f"adopt: step {name!r} failed (exit {rc})", file=sys.stderr)
            return rc
    print("\nadopt: sequence complete — /status shows the adoption section; "
          "next: /architect per feature epic, or /plan-epic --from-debt")
    return 0


# --- CLI -------------------------------------------------------------------------


def resolve_target(owner_repo: str | None, repo_path: str | None) -> str:
    """Resolve the target repo to a local absolute path (mirrors debt_inventory)."""
    if repo_path:
        p = Path(repo_path).expanduser().resolve()
        if not (p / ".git").exists():
            raise AdoptError(f"{p} is not a git repository")
        return str(p)
    if owner_repo:
        from repo import resolve_repo  # noqa: PLC0415 — needs gh; only when asked
        return str(resolve_repo(owner_repo))
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AdoptError("not inside a git repo; pass owner/repo or --repo PATH")
    return proc.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="/adopt mechanics — brownfield entry: survey, elect, baseline, "
                    "grandfather, record (chief-wiggum#215)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("owner_repo", nargs="?", default=None,
                        help="owner/repo to resolve+clone (optional)")
        sp.add_argument("--repo", default=None, help="direct local repo path")

    sp = sub.add_parser("survey", help="shape survey + per-gate applicability verdicts")
    common(sp)
    sp.add_argument("--format", choices=["text", "json"], default="text")

    def elect_flags(sp):
        sp.add_argument("--mode", choices=list(artifacts.MODES), default="sidecar",
                        help="footprint mode (default: sidecar — the brownfield default)")
        sp.add_argument("--backing", choices=list(artifacts.BACKINGS), default="local",
                        help="sidecar backing store (default: local directory)")
        sp.add_argument("--scope-from-codeowners", action="store_true",
                        help="seed scope.json from CODEOWNERS paths (skip-with-note "
                             "when absent; default is whole-repo scope)")

    sp = sub.add_parser("elect", help="footprint election (defaults: sidecar, whole-repo scope)")
    common(sp)
    elect_flags(sp)

    sp = sub.add_parser("baseline", help="ratchet baseline (REAL test run) + debt inventory")
    common(sp)
    sp.add_argument("--workdir", default=None, help="scratch dir (default: session tmp)")

    def grandfather_flags(sp):
        sp.add_argument("--expiry", default=None, metavar="YYYY-MM-DD",
                        help=f"explicit expiry date (default: today + {DEFAULT_EXPIRY_DAYS}d)")
        sp.add_argument("--expiry-days", type=int, default=DEFAULT_EXPIRY_DAYS,
                        help=f"expiry horizon in days (default {DEFAULT_EXPIRY_DAYS})")
        sp.add_argument("--owner", default=DEFAULT_OWNER,
                        help="owner recorded on every grandfather entry")
        sp.add_argument("--extend", action="store_true",
                        help="explicitly amnesty POST-adoption findings into an EXISTING "
                             "grandfathered.json (refused by default; original created_at "
                             "and original entries' expiry are preserved)")

    sp = sub.add_parser("grandfather",
                        help="waive the baseline findings (JUSTIFIED-waiver shape, with expiry)")
    common(sp)
    grandfather_flags(sp)

    sp = sub.add_parser("record", help="write the adoption record (the brownfield switch)")
    common(sp)

    sp = sub.add_parser("run", help="elect -> survey -> baseline -> grandfather -> record")
    common(sp)
    elect_flags(sp)
    sp.add_argument("--workdir", default=None, help="scratch dir (default: session tmp)")
    grandfather_flags(sp)
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.add_argument("--re-adopt", dest="re_adopt", action="store_true",
                    help="explicitly re-run the arrow on an already-adopted target "
                         "(refused by default — re-adoption is an operator act)")

    args = parser.parse_args(argv)
    dispatch = {
        "survey": cmd_survey, "elect": cmd_elect, "baseline": cmd_baseline,
        "grandfather": cmd_grandfather, "record": cmd_record, "run": cmd_run,
    }
    try:
        return dispatch[args.cmd](args)
    except AdoptError as exc:
        print(f"adopt: {exc}", file=sys.stderr)
        return 2
    except ratchet.RatchetError as exc:
        print(f"adopt: ratchet: {exc}", file=sys.stderr)
        return 2
    except ratchet.TamperError as exc:
        print(f"adopt: ratchet: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
