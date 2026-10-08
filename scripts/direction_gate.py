#!/usr/bin/env python3
"""Direction gate: stop verdicts and open rulings bind until the operator rules.

A product CW built once ran 19 days past its own pre-committed evaluation.
The evaluation said ``stop_expanding`` on day 6 and 75 more tickets were filed
anyway. A later re-scope recommendation got no answer for a week, and the
orchestrator resumed the old queue on a general "go". Prose saying "honour the
verdict" was there both times. This script is the mechanism instead.

Ledger: ``<meta root>/direction/rulings.jsonl``, append-only and hash-chained
(each record carries the sha256 of the line before it). Records:

- ``stop``     a stop / re-scope verdict for one epic or the whole product,
               from an evaluation, a council, or the orchestrator itself.
- ``question`` a direction question the orchestrator raised to the operator.
               An unanswered question blocks exactly like a stop: silence is
               not a yes.
- ``ruling``   the operator's answer to one stop or question: ``continue``
               (unblocks) or ``stop`` (stays blocked, now confirmed).

``check`` exits 1 while any stop or question in scope has no ``continue``
ruling, naming each one and the command that clears it. ``/implement-wave``,
``/plan-epic``, ``/architect`` and ``/create-issue`` run it before starting.

**The ruling is a human-only act.** ``rule`` refuses unless stdin and stdout
are both a terminal, then asks the operator to type the item's id back and a
rationale. An agent's Bash tool has no terminal, so an agent cannot produce a
ruling by filling a field or passing a flag; it can only hand the command to
the operator. Editing the ledger by hand breaks the hash chain, and ``check``
fails closed on a broken chain. (Forging a terminal or recomputing the chain
is deliberate forgery, not a mistake this gate is meant to absorb. Keep the
ledger under the ratchet's protected paths so a worker diff touching it parks.)

``check --action wave|architect`` also requires the epic's reality probe
(``reality-probe.md``, template ``templates/reality-probe.md``) to exist and to
pass ``probe-check``. That check is presence AND content (a cited real data
source, an answer, a baseline result), and it is still not evidence on its
own: whoever reads the probe must open the cited source.

Exit codes: 0 = clear, 1 = blocked / refused, 2 = usage error.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import re
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LEDGER_RELPATH = Path("direction") / "rulings.jsonl"
PROBE_NAME = "reality-probe.md"
PRODUCT = "product"
GENESIS = "0" * 64
ACTIONS = ("wave", "plan-epic", "architect", "file-ticket")
# Actions that build on the epic's design, so the reality probe must exist first.
PROBE_ACTIONS = ("wave", "architect")
DECISIONS = ("continue", "stop")


class GateError(Exception):
    pass


# ---- ledger ------------------------------------------------------------------


def ledger_path(repo: str | Path) -> Path:
    import artifacts

    return artifacts.Resolver.resolve(repo).meta_root / LEDGER_RELPATH


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.encode()).hexdigest()


def load(path: Path) -> list[dict]:
    """Read and chain-verify the ledger. A missing ledger is empty; a broken
    chain raises — a ledger someone edited by hand proves nothing."""
    if not path.exists():
        return []
    records: list[dict] = []
    prev = GENESIS
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GateError(f"{path}:{n}: unparseable record ({exc})") from exc
        if rec.get("prev") != prev:
            raise GateError(
                f"{path}:{n}: hash chain broken — the ledger was edited outside "
                "direction_gate.py. Restore it from git; never hand-edit it."
            )
        prev = _line_hash(line)
        records.append(rec)
    return records


def append(path: Path, record: dict) -> dict:
    records = load(path)
    prev = GENESIS
    if records:
        last = [ln for ln in path.read_text().splitlines() if ln.strip()][-1]
        prev = _line_hash(last)
    kind_prefix = {"stop": "STOP", "question": "Q", "ruling": "R"}[record["kind"]]
    n = 1 + sum(1 for r in records if r.get("kind") == record["kind"])
    rec = {"id": f"{kind_prefix}-{n}", **record,
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "prev": prev}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def _scope(epic: str | None) -> str:
    return f"epic:{epic}" if epic else PRODUCT


def blocking(records: list[dict], epic: str | None) -> list[dict]:
    """Stops and questions that bind ``epic`` (its own, plus product-wide),
    each annotated with its latest ruling. Blocking unless that ruling is
    ``continue``."""
    scopes = {PRODUCT} | ({_scope(epic)} if epic else set())
    latest: dict[str, dict] = {}
    for r in records:
        if r.get("kind") == "ruling":
            latest[r.get("ref", "")] = r
    out = []
    for r in records:
        if r.get("kind") not in ("stop", "question") or r.get("scope") not in scopes:
            continue
        ruling = latest.get(r["id"])
        if ruling and ruling.get("decision") == "continue":
            continue
        out.append({**r, "ruling": ruling})
    return out


# ---- reality probe -----------------------------------------------------------

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"\{[^{}\n]+\}|\bTBD\b")
_SOURCE_RE = re.compile(r"(?:https?|gs|s3|bq)://\S+|`[^`\s]*[/.][^`\s]*`")
_NOT_REAL_RE = re.compile(r"fixture|mock|fake|sample|stub|/tests?/|^tests?/", re.IGNORECASE)
REQUIRED_SECTIONS = ("success question", "real-data hand check", "baseline")


def _sections(text: str) -> dict[str, str]:
    heads = list(_SECTION_RE.finditer(text))
    out = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out[m.group(1).strip().lower()] = text[m.end():end].strip()
    return out


def _field(body: str, name: str) -> str:
    m = re.search(rf"^{name}:\s*(.*)$", body, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def probe_problems(path: Path) -> list[str]:
    """Why ``path`` is not an acceptable reality probe (empty = acceptable)."""
    if not path.is_file():
        return [f"no reality probe at {path} — write it from templates/reality-probe.md "
                "before any contracts"]
    text = path.read_text()
    problems = []
    if _PLACEHOLDER_RE.search(text):
        problems.append("unfilled template placeholder ({...} or TBD) remains")
    secs = _sections(text)
    for name in REQUIRED_SECTIONS:
        if not secs.get(name):
            problems.append(f"section '## {name.title()}' is missing or empty")
    hand = secs.get("real-data hand check", "")
    sources = _SOURCE_RE.findall(hand)
    real = [s for s in sources if not _NOT_REAL_RE.search(s)]
    if hand and not real:
        problems.append(
            "the hand check cites no REAL data source (a URL, gs://, bq:// or a "
            "backticked path/table); fixtures, mocks and samples do not count"
            + (f" — rejected: {', '.join(sources)}" if sources else "")
        )
    if hand and not _field(hand, "Answer"):
        problems.append("the hand check has no 'Answer:' line — what did it find today?")
    base = secs.get("baseline", "")
    if base and not _field(base, "Result"):
        problems.append("the baseline has no 'Result:' line — what does the dumb check find?")
    return problems


# ---- commands ----------------------------------------------------------------


def _operator_cmd(repo: str, item: dict) -> str:
    return f'python3 "$CW_HOME/scripts/direction_gate.py" rule --repo "{repo}" --id {item["id"]}'


def cmd_check(args) -> int:
    path = ledger_path(args.repo)
    try:
        items = blocking(load(path), args.epic)
    except GateError as exc:
        print(f"direction gate: BLOCKED — {exc}", file=sys.stderr)
        return 1
    rc = 0
    for it in items:
        rc = 1
        what = "OPEN QUESTION (no operator ruling)" if it["kind"] == "question" else "STOP VERDICT"
        if it.get("ruling"):
            what = f"STOP CONFIRMED by operator {it['ruling'].get('operator')} on {it['ruling'].get('at')}"
        text = it.get("question") or it.get("verdict")
        print(f"direction gate: BLOCKED [{args.action}] by {it['id']} ({it['scope']}) — {what}\n"
              f"  {text}\n"
              f"  source: {it.get('source', 'orchestrator')}; evidence: {it.get('evidence') or '-'}\n"
              f"  Only the operator can clear this, in their own terminal:\n"
              f"    {_operator_cmd(args.repo, it)}",
              file=sys.stderr)
    if args.action in PROBE_ACTIONS and args.epic:
        import artifacts

        probe = Path(args.probe) if args.probe else (
            artifacts.Resolver.resolve(args.repo).epic_dir(args.epic) / PROBE_NAME)
        for p in probe_problems(probe):
            rc = 1
            print(f"direction gate: BLOCKED [{args.action}] reality probe: {p}", file=sys.stderr)
    if rc == 0:
        print(f"direction gate: clear for {args.action} on {_scope(args.epic)}")
    return rc


def cmd_record(args, kind: str) -> int:
    rec: dict = {"kind": kind, "scope": _scope(args.epic), "source": args.source,
                 "evidence": args.evidence}
    if kind == "stop":
        rec["verdict"] = args.verdict
    else:
        rec["question"] = args.question
    try:
        out = append(ledger_path(args.repo), rec)
    except GateError as exc:
        print(f"direction gate: {exc}", file=sys.stderr)
        return 1
    print(f"direction gate: recorded {out['id']} on {out['scope']} — every wave, ticket, "
          f"/plan-epic and /architect for it is blocked until the operator rules:\n"
          f"  {_operator_cmd(args.repo, out)}")
    return 0


def cmd_rule(args, *, is_tty: Callable[[], bool] | None = None,
             ask: Callable[[str], str] = input) -> int:
    """Operator-only. Refuses without a terminal on both stdin and stdout."""
    tty = is_tty() if is_tty else (sys.stdin.isatty() and sys.stdout.isatty())
    if not tty:
        print("direction gate: REFUSED — a ruling is an operator-only act and needs an "
              "interactive terminal. An agent must not answer for the operator; hand "
              "them this command to run in their own terminal:\n"
              f"  python3 \"$CW_HOME/scripts/direction_gate.py\" rule --repo \"{args.repo}\" "
              f"--id {args.id}", file=sys.stderr)
        return 1
    path = ledger_path(args.repo)
    try:
        records = load(path)
    except GateError as exc:
        print(f"direction gate: {exc}", file=sys.stderr)
        return 1
    item = next((r for r in records if r.get("id") == args.id
                 and r.get("kind") in ("stop", "question")), None)
    if item is None:
        print(f"direction gate: no stop or question {args.id} in {path}", file=sys.stderr)
        return 1
    print(f"{item['id']} ({item['scope']}, from {item.get('source')}):\n"
          f"  {item.get('question') or item.get('verdict')}\n"
          f"  evidence: {item.get('evidence') or '-'}")
    if ask(f"Type {item['id']} to rule on it: ").strip() != item["id"]:
        print("direction gate: id not confirmed — nothing recorded", file=sys.stderr)
        return 1
    decision = ask("Decision (continue | stop): ").strip().lower()
    if decision not in DECISIONS:
        print("direction gate: decision must be 'continue' or 'stop' — nothing recorded",
              file=sys.stderr)
        return 1
    rationale = ask("Rationale (one line, required): ").strip()
    if not rationale:
        print("direction gate: a rationale is required — nothing recorded", file=sys.stderr)
        return 1
    rec = append(path, {"kind": "ruling", "ref": item["id"], "scope": item["scope"],
                        "decision": decision, "rationale": rationale,
                        "operator": getpass.getuser(), "via": "tty"})
    print(f"direction gate: {rec['id']} recorded — {item['id']} → {decision}")
    return 0


def cmd_status(args) -> int:
    try:
        records = load(ledger_path(args.repo))
    except GateError as exc:
        print(f"direction gate: {exc}", file=sys.stderr)
        return 1
    for r in records:
        if args.epic and r.get("scope") not in (PRODUCT, _scope(args.epic)):
            continue
        body = r.get("question") or r.get("verdict") or f"{r.get('ref')} → {r.get('decision')}"
        print(f"{r['id']:8} {r['at']} {r['scope']:30} {body}")
    return 0


def cmd_probe_check(args) -> int:
    problems = probe_problems(Path(args.path))
    for p in problems:
        print(f"reality probe: {p}", file=sys.stderr)
    if not problems:
        print(f"reality probe: {args.path} has every section filled and cites a real source. "
              "A presence check is not evidence — open the cited source.")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def scoped(p):
        p.add_argument("--repo", required=True, help="target repo checkout")
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--epic", help="epic slug")
        g.add_argument("--product", action="store_true", help="the whole product")

    p = sub.add_parser("stop", help="record a stop / re-scope verdict")
    scoped(p)
    p.add_argument("--verdict", required=True)
    p.add_argument("--source", required=True, choices=["evaluation", "council", "orchestrator"])
    p.add_argument("--evidence", default="", help="path/URL of the evaluation or council output")

    p = sub.add_parser("ask", help="record a direction question raised to the operator")
    scoped(p)
    p.add_argument("--question", required=True)
    p.add_argument("--source", default="orchestrator", choices=["evaluation", "council", "orchestrator"])
    p.add_argument("--evidence", default="")

    p = sub.add_parser("rule", help="operator-only: rule on a stop or question (needs a TTY)")
    p.add_argument("--repo", required=True)
    p.add_argument("--id", required=True)

    p = sub.add_parser("check", help="exit 1 while a stop or open question binds this scope")
    p.add_argument("--repo", required=True)
    p.add_argument("--epic", help="epic slug (omit for product-level actions)")
    p.add_argument("--action", required=True, choices=ACTIONS)
    p.add_argument("--probe", help="reality probe path (default <epic dir>/reality-probe.md)")

    p = sub.add_parser("status", help="list the ledger")
    p.add_argument("--repo", required=True)
    p.add_argument("--epic")

    p = sub.add_parser("probe-check", help="validate a reality probe file")
    p.add_argument("path")

    args = ap.parse_args(argv)
    if args.cmd in ("stop", "ask"):
        if args.product:
            args.epic = None
        return cmd_record(args, "stop" if args.cmd == "stop" else "question")
    return {"rule": cmd_rule, "check": cmd_check, "status": cmd_status,
            "probe-check": cmd_probe_check}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
