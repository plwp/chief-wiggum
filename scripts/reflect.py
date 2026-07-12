#!/usr/bin/env python3
"""Reflect on how well the CW factory served a repo it built — mine the evidence.

The improvement-loop pattern turned back on the factory itself: read what CW leaves
behind in a target repo (git history, the ratchet journal, TBD/UNRESOLVED markers,
adopted-pattern records, epic retrospectives) and surface mechanical signals about:

  - **Which gates add value** — how often each gate appears in the history, and how
    often it was bypassed (`--force`).
  - **What's slipping through** — reverts and fix/hotfix commits (a bug a gate
    should have caught reached main).
  - **How well the factory fills assumptions** — TBD/UNRESOLVED markers still
    unresolved, and adopted-pattern invariants that were promised but never folded
    into an epic's invariants.md.
  - **Factory-log health** — ratchet regressions / forced merges; retrospective
    "what the loop caught" vs "deferred/not done".

This produces structured EVIDENCE + heuristic FINDINGS. The `/reflect` skill reasons
over them and drafts CW-improvement issues; this script does not create issues.

    python3 scripts/reflect.py /path/to/repo --prs prs.json --format json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import check_unresolved  # noqa: E402

# Known CW gates and the tokens that betray them in commit/PR text.
GATE_TOKENS = {
    "traceability": ["traceability", "check_traceability", "@cw-trace"],
    "single_writer": ["single_writer", "single-writer", "check_single_writer", "@cw-writes"],
    "unresolved": ["check_unresolved", "unresolved marker"],
    "ratchet": ["ratchet"],
    "check_patterns": ["check_patterns", "invariant cluster"],
    "saas_gate": ["saas-gate", "saas_gate"],
    "stitch_audit": ["stitch-audit", "stitch_audit"],
    "design_fidelity": ["design-fidelity", "design fidelity"],
    "portability": ["check_portability", "portability"],
}

CONVENTIONAL_RE = re.compile(r"^(feat|fix|chore|docs|test|refactor|perf|build|ci|revert)(\([^)]*\))?!?:")


@dataclass
class Finding:
    dimension: str   # gates | slippage | assumptions | patterns | factory-logs
    severity: str    # info | warn
    message: str
    evidence: list[str] = field(default_factory=list)


# ---- git history -------------------------------------------------------------

def parse_git_log(text: str) -> list[dict]:
    """Parse `git log --pretty=%H%x1f%s` output into {hash, subject} rows."""
    rows = []
    for line in text.splitlines():
        if "\x1f" in line:
            h, subject = line.split("\x1f", 1)
            rows.append({"hash": h.strip(), "subject": subject.strip()})
    return rows


def classify_commits(commits: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for c in commits:
        m = CONVENTIONAL_RE.match(c["subject"])
        kind = m.group(1) if m else "other"
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def detect_reverts(commits: list[dict]) -> list[dict]:
    return [c for c in commits if c["subject"].lower().startswith("revert")
            or c["subject"].lower().startswith("revert(")]


def slippage_commits(commits: list[dict]) -> list[dict]:
    """fix/hotfix/revert commits — a defect that reached main after the gate ran."""
    out = []
    for c in commits:
        s = c["subject"].lower()
        if s.startswith(("fix(", "fix:", "revert", "hotfix")) or "hotfix" in s:
            out.append(c)
    return out


def gate_mentions(texts: list[str]) -> dict[str, int]:
    joined = "\n".join(t.lower() for t in texts)
    return {gate: sum(joined.count(tok.lower()) for tok in toks)
            for gate, toks in GATE_TOKENS.items()}


def force_bypasses(texts: list[str]) -> int:
    joined = "\n".join(texts).lower()
    return joined.count("--force") + joined.count("--no-verify")


# ---- assumptions (TBD/UNRESOLVED) -------------------------------------------

def scan_markers(root: Path) -> dict:
    docs = root / "docs"
    targets = [docs] if docs.is_dir() else [root]
    findings = check_unresolved.scan(targets)
    by_marker: dict[str, int] = {}
    for f in findings:
        by_marker[f.marker] = by_marker.get(f.marker, 0) + 1
    return {"total": len(findings), "by_marker": by_marker,
            "locations": [f"{f.file}:{f.location}" for f in findings[:25]]}


# ---- ratchet journal ---------------------------------------------------------

def ratchet_health(records: list[dict]) -> dict:
    forced = failed = weakened = removed = merged = 0
    for r in records:
        if r.get("gate_result") == "forced" or r.get("forced"):
            forced += 1
        if r.get("gate_result") == "fail":
            failed += 1
        if r.get("merged"):
            merged += 1
        if isinstance(r.get("amended"), dict):
            weakened += len(r["amended"])
        removed += len(r.get("retired") or [])
    return {"records": len(records), "merged": merged, "gate_failed": failed,
            "forced_merges": forced, "amended_contracts": weakened, "retired_contracts": removed}


def load_journal(root: Path) -> list[dict]:
    path = root / "docs" / "quality" / "ratchet-journal.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ---- adopted-pattern coverage -----------------------------------------------

INV_ID_RE = re.compile(r"\bINV-[A-Za-z0-9][A-Za-z0-9-]*-[A-Za-z]?[0-9]+")


def pattern_coverage(adopted: dict, invariants_text: str) -> list[dict]:
    """For each adopted pattern, which promised INV ids actually appear in the epics."""
    present = set(INV_ID_RE.findall(invariants_text))
    out = []
    for pid, rec in (adopted.get("patterns") or {}).items():
        promised = [i for i in (rec.get("invariants") or []) if i]
        missing = [i for i in promised if i not in present]
        out.append({"pattern": pid, "promised": len(promised),
                    "folded": len(promised) - len(missing), "missing": missing,
                    "unresolved_params": rec.get("unresolved") or []})
    return out


def read_adopted(root: Path) -> dict:
    path = root / "docs" / "patterns" / "adopted.json"
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def read_epic_invariants(root: Path) -> str:
    return "\n".join(p.read_text(errors="ignore")
                     for p in sorted((root / "docs" / "epics").glob("*/invariants.md"))) \
        if (root / "docs" / "epics").is_dir() else ""


def read_retrospectives(root: Path) -> list[dict]:
    out = []
    epics = root / "docs" / "epics"
    if not epics.is_dir():
        return out
    for p in sorted(epics.glob("*/retrospective.md")):
        text = p.read_text(errors="ignore")
        headers = [ln.strip("# ").strip() for ln in text.splitlines() if ln.startswith("#")]
        out.append({"epic": p.parent.name, "sections": headers, "bytes": len(text)})
    return out


# ---- orchestration -----------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def collect(repo: Path, commits_limit: int = 400, prs: list[dict] | None = None) -> dict:
    log = _git(repo, "log", f"-{commits_limit}", "--pretty=%H%x1f%s")
    commits = parse_git_log(log)
    subjects = [c["subject"] for c in commits]
    pr_texts = [f"{p.get('title', '')}\n{p.get('body', '')}" for p in (prs or [])]

    reverts = detect_reverts(commits)
    slips = slippage_commits(commits)
    markers = scan_markers(repo)
    journal = ratchet_health(load_journal(repo))
    coverage = pattern_coverage(read_adopted(repo), read_epic_invariants(repo))
    retros = read_retrospectives(repo)

    findings: list[Finding] = []
    mentions = gate_mentions(subjects + pr_texts)
    for gate, n in mentions.items():
        if n == 0:
            findings.append(Finding("gates", "info",
                f"gate '{gate}' never appears in {len(commits)} commits / {len(prs or [])} PRs — "
                f"either it never fires here, or its value is invisible in the record"))
    forces = force_bypasses(subjects + pr_texts)
    if forces:
        findings.append(Finding("gates", "warn",
            f"{forces} gate-bypass token(s) (--force/--no-verify) in history — a gate operators route around erodes trust in every gate",
            [c["subject"] for c in commits if "--force" in c["subject"].lower()][:5]))
    if slips:
        findings.append(Finding("slippage", "warn",
            f"{len(slips)} fix/revert/hotfix commit(s) — candidate defects that reached main after the gates ran",
            [c["subject"] for c in slips[:8]]))
    if reverts:
        findings.append(Finding("slippage", "warn", f"{len(reverts)} revert(s) in history",
                                 [c["subject"] for c in reverts[:5]]))
    if markers["total"]:
        findings.append(Finding("assumptions", "warn",
            f"{markers['total']} unresolved assumption marker(s) still in docs ({markers['by_marker']}) — the factory guessed and never confirmed",
            markers["locations"][:8]))
    for cov in coverage:
        if cov["missing"]:
            findings.append(Finding("patterns", "warn",
                f"pattern '{cov['pattern']}' promised {cov['promised']} invariants but {len(cov['missing'])} were never folded into an epic: {cov['missing']}"))
        if cov["unresolved_params"]:
            findings.append(Finding("patterns", "info",
                f"pattern '{cov['pattern']}' still has unbound params: {cov['unresolved_params']}"))
    if journal["forced_merges"]:
        findings.append(Finding("factory-logs", "warn",
            f"{journal['forced_merges']} forced ratchet merge(s) — quality bar was lowered under override"))

    return {
        "repo": str(repo),
        "commits_scanned": len(commits),
        "commit_kinds": classify_commits(commits),
        "gate_mentions": mentions,
        "force_bypasses": forces,
        "reverts": len(reverts),
        "slippage_commits": len(slips),
        "assumptions": markers,
        "ratchet": journal,
        "pattern_coverage": coverage,
        "retrospectives": retros,
        "pr_count": len(prs or []),
        "findings": [asdict(f) for f in findings],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Mine a CW-built repo for factory-effectiveness evidence.")
    parser.add_argument("repo", type=Path, help="Local path to the target repo")
    parser.add_argument("--prs", type=Path, help="JSON file of `gh pr list --json ...` output (optional)")
    parser.add_argument("--commits", type=int, default=400, help="How many commits back to scan")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    if not (args.repo / ".git").exists():
        print(f"reflect: {args.repo} is not a git repo", file=sys.stderr)
        return 2

    prs = None
    if args.prs:
        try:
            prs = json.loads(args.prs.read_text())
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            print(f"reflect: could not read --prs: {exc}", file=sys.stderr)
            return 2

    report = collect(args.repo, commits_limit=args.commits, prs=prs)

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(f"reflect: {report['repo']} — {report['commits_scanned']} commits, {report['pr_count']} PRs")
        print(f"  commit kinds: {report['commit_kinds']}")
        print(f"  slippage: {report['slippage_commits']} fix/revert  |  bypasses: {report['force_bypasses']}  |  unresolved markers: {report['assumptions']['total']}")
        print(f"  ratchet: {report['ratchet']}")
        print(f"\n  {len(report['findings'])} finding(s):")
        for f in report["findings"]:
            print(f"  [{f['severity']}] ({f['dimension']}) {f['message']}")
            for e in f["evidence"]:
                print(f"      · {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
