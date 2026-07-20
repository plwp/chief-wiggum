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
so lowering the bar by editing state is detectable and fails closed.

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

Exit codes: 0 = ok, 1 = gate violation, 2 = usage/config error,
3 = no scorecard (run `score` first), 4 = journal tamper detected.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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

CONFIG_NAME = "ratchet.json"
JOURNAL_NAME = "ratchet-journal.jsonl"
HIGHWATER_NAME = "ratchet-highwater.json"
SCORECARD_NAME = "ratchet-scorecard.json"
DEFAULT_STATE_DIR = "docs/quality"

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
    parser: str  # go-test-json | junit-xml | pass-fail-lines
    cwd: str = "."
    report: str | None = None  # junit-xml: file the cmd writes, repo-relative


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
    path = repo / DEFAULT_STATE_DIR / CONFIG_NAME
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


def parse_pass_fail_lines(stdout: str) -> set[str]:
    passed, failed = set(), set()
    for line in stdout.splitlines():
        m = re.match(r"^\s*(PASS|FAIL)[:\s]+(\S+)", line)
        if not m:
            continue
        (passed if m.group(1) == "PASS" else failed).add(m.group(2))
    return passed - failed


def run_suite(cfg: Config, suite: Suite) -> set[str]:
    """Run one suite and return its passing case IDs, namespaced by suite name.

    A non-zero exit is expected when tests fail — the parsed per-case results
    are the signal, not the exit code.
    """
    proc = subprocess.run(
        suite.cmd, shell=True, cwd=cfg.repo / suite.cwd, capture_output=True, text=True
    )
    if suite.parser == "go-test-json":
        passed = parse_go_test_json(proc.stdout)
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
    elif suite.parser == "pass-fail-lines":
        passed = parse_pass_fail_lines(proc.stdout)
    else:
        raise RatchetError(f"suite {suite.name!r}: unknown parser {suite.parser!r}")
    if not passed and proc.returncode != 0:
        sys.stderr.write(
            f"ratchet: suite {suite.name!r} produced no passing cases "
            f"(exit {proc.returncode}):\n{proc.stderr[-2000:]}\n"
        )
    return {f"{suite.name}::{cid}" for cid in passed}


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
    comp = _complexity.analyze(repo, venv=venv, gobin=gobin)
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
    ch = _churn.analyze(repo, no_merges=True)
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
    for rec in records:
        if rec.get("merged"):
            sc = rec.get("scorecard", {}) or {}
            pass_set.update(sc.get("pass_set", []) or [])
            for cid, h in (sc.get("contract_hashes", {}) or {}).items():
                contract_hashes.setdefault(canonical_id(cid), h)
        for cid, h in (rec.get("amended", {}) or {}).items():
            contract_hashes[canonical_id(cid)] = h
        for cid in rec.get("retired", []) or []:
            contract_hashes.pop(canonical_id(cid), None)
    return {
        "pass_set": sorted(pass_set),
        "contract_hashes": contract_hashes,
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
    return {"missing_tests": missing, "weakened_contracts": weakened, "removed_contracts": removed}


# ---- subcommands ---------------------------------------------------------------


def _read_scorecard(cfg: Config) -> dict:
    if not cfg.scorecard.is_file():
        sys.stderr.write("ratchet: no scorecard — run `ratchet.py score` first.\n")
        sys.exit(3)
    return json.loads(cfg.scorecard.read_text())


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


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
    if (repo / "package.json").is_file() and not suites:
        # JS runners need a junit reporter configured; leave a skeleton the
        # operator fills in (e.g. vitest --reporter=junit, jest-junit).
        suites.append(
            {"name": "js", "cmd": "npm test", "cwd": ".", "parser": "junit-xml", "report": "junit.xml"}
        )
    return suites


def cmd_init(args) -> int:
    repo = repo_root(args.repo)
    path = repo / DEFAULT_STATE_DIR / CONFIG_NAME
    if path.is_file() and not args.force:
        print(f"ratchet: config already exists at {path}")
        return 0
    cfg = {
        "suites": detect_suites(repo),
        "epic_docs": "docs/epics",
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
    if not args.no_tests:
        for suite in cfg.suites:
            pass_set |= run_suite(cfg, suite)
    quality = {"skipped": "quality metrics disabled (--no-quality)"}
    if not args.no_quality:
        quality = score_quality(cfg, venv=args.venv, gobin=args.gobin)
    sc = {
        "passed": len(pass_set),
        "pass_set": sorted(pass_set),
        "contract_hashes": contract_hashes,
        "tests_run": not args.no_tests,
        "quality": quality,
    }
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
        f"{len(contract_hashes)} contract definition(s); {qmsg}"
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
    sidecar = load_sidecar(cfg.repo / SIDECAR_RELPATH)
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
    if args.format == "json":
        print(json.dumps({**hard, "quality_regressions": qregs, "suspect_links": susp}, indent=2))
    else:
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
    body = {
        "record_id": f"rec-{len(records) + 1:05d}",
        "event": args.event,
        "ref": args.ref,
        "gate_result": args.gate,
        "merged": bool(args.merged),
        "scorecard": sc,
        "amended": amended,
        "retired": sorted(canonical_id(c) for c in (args.retire or [])),
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


def cmd_protected(args) -> int:
    cfg = load_config(repo_root(args.repo))
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{args.base}...HEAD"],
        cwd=cfg.repo, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RatchetError(f"git diff failed: {proc.stderr.strip()}")
    hits = protected_hits(cfg, proc.stdout.splitlines())
    if hits:
        sys.stderr.write(
            "ratchet: PROTECTED PATHS TOUCHED — park for human review, do not merge:\n"
            + "".join(f"  {h}\n" for h in hits)
        )
        return 1
    print("ratchet: no protected paths touched")
    return 0


def _scanner_version() -> str:
    """Hash-derived ``--scanner-version``: the source of this module plus its
    finding-affecting local dependencies (hashing.py for
    stable_hash/hash_epic_definitions, trace_ids.py for the shared stable-ID
    grammar, trace_links.py for suspect-link propagation, and the lazily
    imported quality engines churn.py/complexity.py that shape the
    quality_regressions findings ``check`` reports). No hand-bumped constant to
    forget (INV-fh-005).
    @cw-trace guards CTR-fh-040 CTR-fh-041 CTR-fh-042 INV-fh-005"""
    here = Path(__file__).resolve()
    cw_dir = here.parent / "chief_wiggum"
    q_dir = here.parent / "quality"
    return scanner_version(
        here,
        cw_dir / "hashing.py",
        cw_dir / "trace_ids.py",
        cw_dir / "trace_links.py",
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
    sp.add_argument("--amend", action="append", metavar="ID",
                    help="accept ID's current definition hash as the new baseline (human-approved)")
    sp.add_argument("--retire", action="append", metavar="ID",
                    help="drop ID from the high-water mark (human-approved)")

    sp = sub.add_parser("protected", help="flag branch diffs touching the protected pathset")
    common(sp)
    sp.add_argument("--base", default="origin/main")

    args = p.parse_args()
    dispatch = {
        "init": cmd_init, "score": cmd_score, "check": cmd_check,
        "regressed": cmd_regressed, "record": cmd_record, "recent": cmd_recent,
        "highwater": cmd_highwater, "protected": cmd_protected,
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
