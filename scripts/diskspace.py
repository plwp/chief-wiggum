#!/usr/bin/env python3
"""
Find where the gigabytes are on this disk, and reclaim them safely.

    diskspace.py scan [ROOTS...] [--min-size 500M] [--json PATH] [--top 40]
    diskspace.py clean --from SCAN.json [--dry-run] [--include-ask]
    diskspace.py docker

`scan` walks each root with os.scandir (no symlinks followed, no crossing onto
another mounted volume, PermissionError skipped) and measures ON-DISK size
(st_blocks * 512), not apparent size: a sparse Docker.raw can claim 460 GB and
occupy 86 GB. Every directory big enough to matter becomes a row with a
category from RULES below and one of three safety classes:

  regenerable  Build artefacts, caches, virtualenvs, terraform providers, clean
               and fully pushed git worktrees, session temp older than 7 days.
               Deleting them costs a rebuild or a re-download, nothing more.
               `clean` pre-selects them and removes them with shutil.rmtree,
               because moving node_modules to the Trash frees nothing.
  check        Worth a look before deleting: worktrees or clones with dirty or
               unpushed work, session temp under 7 days, Downloads, anything
               uncategorised. Off by default, the reason is printed, and a
               selected row is MOVED to ~/.Trash/diskspace-<timestamp>/, never
               deleted outright.
  ask          Docker's disk image and anything under Documents, Desktop or
               Pictures. Listed, but `clean` will not select it unless run
               with --include-ask, and then it too goes to the Trash.

Hard rules `clean` enforces whatever is selected:
  * nothing outside the scan's roots, no scan root itself, no ancestor of a
    root, and none of the protected home folders (~/Library, ~/Documents...);
  * a git worktree is always unregistered from its main repo (`git worktree
    remove --force`, or `git worktree prune` after a move to the Trash);
  * a worktree whose HEAD has commits on no remote ref is refused unless its
    row was a `check` row Pat selected explicitly. Git state is re-read at
    delete time, not trusted from the scan;
  * a dry run (the plan and its total) is printed before anything happens,
    and `--dry-run` stops there.

`docker` reports Docker.raw on-disk vs apparent size, asks the engine for
`docker system df` with a 20 s timeout, and offers the prunes behind y/N
prompts. A hung engine is reported as hung, with the one manual step.

Standard library only. See docs/diskspace.md.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

HOME = Path.home()
CLAUDE_TMP = Path(f"/private/tmp/claude-{os.getuid()}")
CW_DIR = HOME / ".chief-wiggum"
DOCKER_RAW = HOME / "Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw"

SAFETY_ORDER = ("regenerable", "check", "ask")
SESSION_TMP_FRESH_DAYS = 7
ACTIVE_HOURS = 24          # a checkout git touched this recently is someone's live work
GIT_TIMEOUT = 20

# ---------------------------------------------------------------- rule table
# Directory names that are build output wherever they appear.
BUILD_NAMES_ANYWHERE = {"__pycache__", ".pytest_cache", ".terraform", ".next",
                        ".mypy_cache", ".ruff_cache", ".turbo", ".parcel-cache"}
# node_modules counts only beside a package.json: a global prefix such as
# ~/.nvm/versions/node/*/lib/node_modules holds installed tools, not build output.
# Names that are build output only inside a git checkout (a `build/` in
# ~/Library is somebody's data). `target` additionally needs a Cargo.toml
# beside it or a CACHEDIR.TAG inside it.
BUILD_NAMES_IN_REPO = {"dist", "build", "target", ".venv", "venv"}
# Directory names that are caches wherever they appear.
CACHE_NAMES = {"go-build", "_cacache"}
# Every child of these is a cache.
CACHE_PARENTS = (HOME / "Library/Caches", HOME / ".cache")
CACHE_PATHS = (HOME / "Library/Developer/Xcode/DerivedData", HOME / "go/pkg/mod",
               HOME / ".cargo/registry", HOME / ".gradle/caches", HOME / ".ollama/models")
# Caches that are regenerable in principle but slow to refill and used by
# tools day to day: shown as `check`, not pre-selected.
HEAVY_CACHES = {
    HOME / ".ollama/models": "ollama models; `ollama pull` re-downloads them",
    HOME / ".cache/huggingface": "Hugging Face models; re-downloaded on next use",
    HOME / ".cache/whisper": "whisper models used by /transcribe",
    HOME / "Library/Caches/ms-playwright": "Playwright browsers in use by tools",
}
DOCKER_PATHS = (HOME / "Library/Containers/com.docker.docker",
                HOME / "Library/Group Containers/group.com.docker")
SESSION_TMP_PARENTS = (CW_DIR / "tmp",)
ASK_PARENTS = (HOME / "Documents", HOME / "Desktop", HOME / "Pictures")
DOWNLOADS = HOME / "Downloads"
# Never deleted, whatever is selected.
PROTECTED = {HOME, HOME / "Library", HOME / "Documents", HOME / "Desktop", HOME / "Pictures",
             HOME / "Downloads", HOME / "Movies", HOME / "Music", HOME / "repos", CW_DIR,
             HOME / ".Trash", HOME / "Library/Caches", HOME / ".cache", Path("/"),
             Path("/private/tmp"), CLAUDE_TMP}
# Not walked at all: slow, private, or TCC-guarded.
SKIP_PATHS = {HOME / "Library/Mail", HOME / ".Trash", HOME / "Library/Messages",
              HOME / "Library/Metadata/CoreSpotlight"}
SKIP_SUFFIXES = (".photoslibrary",)
# Other apps' sandbox containers raise a macOS "access data from other apps"
# prompt per app; only Docker's is walked unless --deep-containers.
CONTAINER_PARENTS = (HOME / "Library/Containers", HOME / "Library/Group Containers")
# Categories whose insides are never broken into further rows.
OPAQUE = {"build-artefact", "cache", "venv", "docker", "session-tmp"}


@dataclass
class Row:
    path: str
    size: int                 # on-disk bytes, whole subtree
    apparent: int             # st_size bytes, whole subtree
    own: int                  # size minus nested rows (what the aggregate counts)
    mtime: float              # directory's own mtime
    category: str
    safety: str = "check"
    reason: str = ""
    root: str = ""
    git: dict = field(default_factory=dict)
    worktrees: list = field(default_factory=list)   # nested git worktrees
    protected: bool = False


# ---------------------------------------------------------------- helpers
def human(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit in ("B", "K") else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def parse_size(text: str) -> int:
    text = text.strip().upper().rstrip("B")
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    if text and text[-1] in mult:
        return int(float(text[:-1]) * mult[text[-1]])
    return int(text)


def age(mtime: float, now: float | None = None) -> str:
    days = ((now or time.time()) - mtime) / 86400
    if days < 1:
        return f"{max(days * 24, 0):.0f}h"
    if days < 60:
        return f"{days:.0f}d"
    if days < 730:
        return f"{days / 30:.0f}mo"
    return f"{days / 365:.0f}y"


def under(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                          timeout=GIT_TIMEOUT)


def git_state(path: Path, worktree: bool) -> dict:
    """Branch, dirty count, unpushed count and main repo for a checkout."""
    state: dict = {}
    try:
        top = git(path, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            state["broken"] = (top.stderr.strip() or "not a git checkout")[:120]
            state["main"] = str(_main_from_gitfile(path) or "")
            return state
        # Read activity stamps before anything else touches the gitdir.
        gitdir = git(path, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip()
        stamps = [path, Path(gitdir) / "index", Path(gitdir) / "HEAD", Path(gitdir) / "logs" / "HEAD"]
        state["last_active"] = max((p.stat().st_mtime for p in stamps if p.exists()), default=0)
        head = git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        state["branch"] = head
        status = git(path, "--no-optional-locks", "status", "--porcelain", "--untracked-files=normal")
        state["dirty"] = len([ln for ln in status.stdout.splitlines() if ln.strip()])
        scope = ["HEAD"] if worktree else ["--branches"]
        unpushed = git(path, "rev-list", "--count", *scope, "--not", "--remotes")
        state["unpushed"] = int(unpushed.stdout.strip() or 0) if unpushed.returncode == 0 else -1
        up = git(path, "rev-parse", "--abbrev-ref", "@{u}")
        if up.returncode == 0:
            state["upstream"] = up.stdout.strip()
            state["on_upstream"] = git(path, "merge-base", "--is-ancestor", "HEAD",
                                       "@{u}").returncode == 0
        else:
            state["upstream"] = None
            state["on_upstream"] = False
        common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common.returncode == 0:
            state["main"] = str(Path(common.stdout.strip()).parent)
        if not worktree:
            wts = git(path, "worktree", "list", "--porcelain").stdout
            entries = sum(1 for ln in wts.splitlines() if ln.startswith("worktree "))
            state["linked_worktrees"] = max(entries - 1, 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        state["broken"] = f"git failed: {exc}"[:120]
    return state


def _main_from_gitfile(path: Path) -> Path | None:
    """Main repo of a worktree from its `.git` file, even if git cannot run there."""
    try:
        text = (path / ".git").read_text().strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (path / gitdir).resolve()
    # <main>/.git/worktrees/<name>
    if gitdir.parent.name == "worktrees":
        return gitdir.parent.parent.parent
    return None


def git_reason(st: dict, worktree: bool) -> tuple[str, bool]:
    """(reason text, at_risk) for a checkout's git state."""
    if "broken" in st:
        return f"git: {st['broken']}", True
    bits, risk = [], False
    if st.get("dirty"):
        bits.append(f"dirty: {st['dirty']} files")
        risk = True
    if st.get("unpushed", 0) != 0:
        what = f"branch {st.get('branch')}" if worktree else "local branches"
        bits.append(f"{what} not pushed ({st['unpushed']} commits on no remote)"
                    if st["unpushed"] > 0 else "push state unknown")
        risk = True
    if not worktree and st.get("linked_worktrees"):
        bits.append(f"has {st['linked_worktrees']} linked worktrees")
        risk = True
    hours = (time.time() - st.get("last_active", 0)) / 3600
    if hours < ACTIVE_HOURS:
        bits.append(f"in use: git activity {hours:.0f}h ago")
        risk = True
    if not bits:
        bits.append(f"{st.get('branch')} clean, pushed")
    return "; ".join(bits), risk


# ---------------------------------------------------------------- scan
class Scanner:
    def __init__(self, roots: list[Path], min_size: int, deep_containers: bool = False):
        self.roots = roots
        self.min_size = min_size
        self.deep_containers = deep_containers
        self.rows: list[Row] = []
        self.skipped: list[str] = []
        self.errors = 0
        self.seen_inodes: set[tuple[int, int]] = set()
        self.now = time.time()
        # The walk is syscall-bound (one lstat per entry), so the top levels
        # fan out to threads, one pool per level. A level-k worker waits only
        # on level k+1, and the last level never waits, so this cannot deadlock.
        self.pools: list[ThreadPoolExecutor] = []

    def classify(self, path: Path, names: set[str], repo: Path | None) -> str | None:
        name = path.name
        if any(path == p for p in DOCKER_PATHS):
            return "docker"
        if any(path.parent == p for p in SESSION_TMP_PARENTS):
            return "session-tmp"
        if name == "scratchpad" and under(path, CLAUDE_TMP):
            return "session-tmp"
        if ".git" in names:
            return "git-worktree" if (path / ".git").is_file() else "repo-clone"
        if name in BUILD_NAMES_ANYWHERE:
            return "build-artefact"
        if name == "node_modules" and (path.parent / "package.json").exists():
            return "build-artefact"
        if repo is not None and name in BUILD_NAMES_IN_REPO:
            if name == "target":
                if (path.parent / "Cargo.toml").exists() or "CACHEDIR.TAG" in names:
                    return "build-artefact"
            elif name in (".venv", "venv"):
                if "pyvenv.cfg" in names:
                    return "build-artefact"
            else:
                return "build-artefact"
        if "pyvenv.cfg" in names:
            return "venv"
        if name in CACHE_NAMES or any(path.parent == p for p in CACHE_PARENTS) \
                or any(path == p for p in CACHE_PATHS):
            return "cache"
        if path == DOWNLOADS:
            return "downloads"
        return None

    def skip(self, path: Path) -> bool:
        if path in SKIP_PATHS or path.name.endswith(SKIP_SUFFIXES):
            return True
        if not self.deep_containers and any(path.parent == p for p in CONTAINER_PARENTS):
            return not any(path == p for p in DOCKER_PATHS)
        return False

    def run(self, workers: int = 16, levels: int = 3) -> list[Row]:
        self.pools = [ThreadPoolExecutor(workers) for _ in range(levels)] if workers > 1 else []
        try:
            for root in self.roots:
                try:
                    st = os.lstat(root)
                except OSError:
                    self.skipped.append(f"{root} (missing)")
                    continue
                self._walk(root, st, st.st_dev, None, False, root, 0)
        finally:
            for pool in self.pools:
                pool.shutdown()
        return self.rows

    def _walk(self, path: Path, st: os.stat_result, dev: int, repo: Path | None,
              opaque: bool, root: Path, depth: int) -> tuple[int, int, int, list[str]]:
        """Returns (size, apparent, accounted-by-rows, nested worktrees)."""
        size = st.st_blocks * 512
        apparent = st.st_size
        accounted = 0
        worktrees: list[str] = []
        subdirs: list[tuple[Path, os.stat_result]] = []
        names: set[str] = set()
        try:
            with os.scandir(path) as it:
                for entry in it:
                    names.add(entry.name)
                    try:
                        est = entry.stat(follow_symlinks=False)
                    except OSError:
                        self.errors += 1
                        continue
                    if stat.S_ISDIR(est.st_mode):
                        subdirs.append((Path(entry.path), est))
                        continue
                    if est.st_nlink > 1:
                        key = (est.st_dev, est.st_ino)
                        if key in self.seen_inodes:
                            continue
                        self.seen_inodes.add(key)
                    size += est.st_blocks * 512
                    apparent += est.st_size
        except OSError:
            self.errors += 1
            return size, apparent, 0, []

        category = None if opaque else self.classify(path, names, repo)
        if ".git" in names and category != "git-worktree" and (path / ".git").is_file():
            worktrees.append(str(path))   # a worktree inside an opaque row
        if path == root and category not in (None, "downloads"):
            category = None   # a root is never itself a deletable unit
        if ".git" in names:
            repo = path
        if category == "git-worktree":
            worktrees.append(str(path))
        inner_opaque = opaque or category in OPAQUE

        jobs = []
        for sub, sst in subdirs:
            if sst.st_dev != dev:
                self.skipped.append(f"{sub} (other volume)")
                continue
            if self.skip(sub):
                self.skipped.append(str(sub))
                continue
            # A .git directory is walked for its size but never offered as a row.
            job = (sub, sst, dev, repo, inner_opaque or sub.name == ".git", root, depth + 1)
            if depth < len(self.pools):
                jobs.append(self.pools[depth].submit(self._walk, *job))
            else:
                jobs.append(job)
        for job in jobs:
            s, a, acc, wts = job.result() if hasattr(job, "result") else self._walk(*job)
            size += s
            apparent += a
            accounted += acc
            worktrees.extend(wts)

        if opaque:
            return size, apparent, 0, worktrees
        own = size - accounted
        emit = (path == root or (category is not None and size >= self.min_size)
                or (category is None and own >= self.min_size))
        if emit:
            self.rows.append(self.make_row(path, size, apparent, own, st.st_mtime,
                                           category or self.user_category(path), root, worktrees))
            return size, apparent, size, worktrees
        return size, apparent, accounted, worktrees

    def user_category(self, path: Path) -> str:
        return "downloads" if under(path, DOWNLOADS) else "user-data"

    def make_row(self, path: Path, size: int, apparent: int, own: int, mtime: float,
                 category: str, root: Path, worktrees: list[str]) -> Row:
        row = Row(str(path), size, apparent, own, mtime, category, root=str(root),
                  worktrees=[w for w in worktrees if w != str(path)])
        row.protected = path in PROTECTED or any(under(r, path) for r in self.roots)
        classify_safety(row, self.now)
        return row


def classify_safety(row: Row, now: float) -> None:
    path = Path(row.path)
    cat = row.category
    if path in HEAVY_CACHES:
        row.safety, row.reason = "check", HEAVY_CACHES[path]
    elif cat in ("build-artefact", "cache", "venv"):
        row.safety, row.reason = "regenerable", {"cache": "re-created on demand",
                                                 "venv": "reinstall from requirements",
                                                 "build-artefact": "rebuilt by the build"}[cat]
    elif cat == "session-tmp":
        days = (now - row.mtime) / 86400
        row.safety = "regenerable" if days >= SESSION_TMP_FRESH_DAYS else "check"
        row.reason = f"session temp, {age(row.mtime, now)} old"
    elif cat == "git-worktree":
        row.git = git_state(path, worktree=True)
        row.reason, risk = git_reason(row.git, worktree=True)
        row.safety = "check" if risk else "regenerable"
    elif cat == "repo-clone":
        row.git = git_state(path, worktree=False)
        row.reason, _ = git_reason(row.git, worktree=False)
        row.safety = "check"
    elif cat == "docker":
        row.safety, row.reason = "ask", "use `diskspace.py docker`; Docker Desktop owns this"
    elif cat == "downloads":
        row.safety, row.reason = "check", "your downloads"
    else:
        row.safety, row.reason = "check", "uncategorised"
    if any(under(path, p) for p in ASK_PARENTS):
        row.safety, row.reason = "ask", "under Documents/Desktop/Pictures"
    if row.worktrees and row.safety == "regenerable":
        for wt in row.worktrees:
            st = git_state(Path(wt), worktree=True)
            text, risk = git_reason(st, worktree=True)
            if risk:
                row.safety, row.reason = "check", f"contains worktree {wt}: {text}"
                break
    if row.protected:
        row.reason = "protected (scan root or system folder); listed for size only"


def default_roots() -> list[Path]:
    return [HOME, CLAUDE_TMP, CW_DIR]


def normalise_roots(roots: list[Path]) -> list[Path]:
    """Absolute, existing, and with roots nested in another root dropped."""
    out: list[Path] = []
    for r in sorted({Path(os.path.abspath(os.path.expanduser(str(r)))) for r in roots}):
        if any(under(r, o) for o in out):
            continue
        out.append(r)
    return out


def scan(roots: list[Path], min_size: int, deep_containers: bool = False) -> dict:
    roots = normalise_roots(roots)
    started = time.time()
    sc = Scanner(roots, min_size, deep_containers)
    rows = sc.run()
    rows.sort(key=lambda r: r.size, reverse=True)
    totals: dict[str, int] = {}
    for r in rows:
        totals[r.category] = totals.get(r.category, 0) + r.own
    return {
        "version": 1,
        "scanned_at": started,
        "seconds": round(time.time() - started, 1),
        "roots": [str(r) for r in roots],
        "min_size": min_size,
        "rows": [asdict(r) for r in rows],
        "totals": totals,
        "skipped": sc.skipped[:500],
        "errors": sc.errors,
    }


def print_table(result: dict, top: int, out=sys.stdout) -> None:
    rows = [r for r in result["rows"] if not r.get("protected")]
    now = time.time()
    out.write(f"Scanned {', '.join(result['roots'])} in {result['seconds']}s "
              f"({result['errors']} unreadable entries skipped)\n\n")
    out.write("By category (each byte counted once):\n")
    for cat, n in sorted(result["totals"].items(), key=lambda kv: -kv[1]):
        out.write(f"  {human(n):>8}  {cat}\n")
    out.write(f"\nBiggest {min(top, len(rows))} of {len(rows)} rows "
              "(scan roots and system folders are in the JSON only):\n")
    out.write(f"  {'SIZE':>8}  {'AGE':>5}  {'CATEGORY':<14}  {'SAFETY':<11}  PATH\n")
    for r in rows[:top]:
        note = f"   [{r['reason']}]" if r["reason"] and r["safety"] != "regenerable" else ""
        out.write(f"  {human(r['size']):>8}  {age(r['mtime'], now):>5}  {r['category']:<14}  "
                  f"{r['safety']:<11}  {r['path']}{note}\n")


def default_json_path() -> Path:
    base = Path(os.environ["CW_TMP"]) if os.environ.get("CW_TMP") else CW_DIR / "tmp" / "diskspace"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"diskspace-scan-{time.strftime('%Y%m%d-%H%M%S')}.json"


# ---------------------------------------------------------------- clean
@dataclass
class Candidate:
    num: int
    row: dict
    selected: bool
    locked: bool


def candidates(result: dict, include_ask: bool) -> list[Candidate]:
    cands: list[Candidate] = []
    rows = [r for r in result["rows"] if not r.get("protected")]
    rows.sort(key=lambda r: (SAFETY_ORDER.index(r["safety"]), -r["size"]))
    for i, r in enumerate(rows, 1):
        locked = r["safety"] == "ask" and not include_ask
        cands.append(Candidate(i, r, selected=r["safety"] == "regenerable", locked=locked))
    return cands


def print_candidates(cands: list[Candidate], out=sys.stdout) -> None:
    current = None
    for c in cands:
        if c.row["safety"] != current:
            current = c.row["safety"]
            out.write(f"\n-- {current} --\n")
        box = "[-]" if c.locked else ("[x]" if c.selected else "[ ]")
        reason = f"   ({c.row['reason']})" if c.row["reason"] else ""
        out.write(f"{c.num:>4} {box} {human(c.row['size']):>8}  {c.row['category']:<14} "
                  f"{c.row['path']}{reason}\n")
    total = sum(c.row["size"] for c in cands if c.selected)
    out.write(f"\nSelected: {human(total)}\n")


def toggle(cands: list[Candidate], text: str, out=sys.stdout) -> None:
    by_num = {c.num: c for c in cands}
    for tok in text.replace(",", " ").split():
        if tok in ("none", "all"):
            for c in cands:
                if not c.locked:
                    c.selected = tok == "all"
            continue
        lo, _, hi = tok.partition("-")
        try:
            nums = range(int(lo), int(hi or lo) + 1)
        except ValueError:
            out.write(f"  ? {tok}\n")
            continue
        for n in nums:
            c = by_num.get(n)
            if c is None:
                out.write(f"  no row {n}\n")
            elif c.locked:
                out.write(f"  row {n} is `ask`: rerun with --include-ask to select it\n")
            else:
                c.selected = not c.selected


@dataclass
class Action:
    path: str
    size: int
    method: str           # rmtree | trash | refuse
    row: dict
    worktrees: list = field(default_factory=list)   # worktrees to unregister
    note: str = ""


def plan(cands: list[Candidate], roots: list[str], trash_dir: Path) -> list[Action]:
    chosen = [c for c in cands if c.selected and not c.locked]
    paths = [Path(c.row["path"]) for c in chosen]
    actions: list[Action] = []
    root_paths = [Path(r) for r in roots]
    for c in chosen:
        p = Path(c.row["path"])
        if any(other != p and under(p, other) for other in paths):
            continue   # covered by a selected ancestor
        act = Action(str(p), c.row["size"], "", c.row)
        refusal = refuse_reason(p, root_paths)
        wts = ([str(p)] if c.row["category"] == "git-worktree" else []) + list(c.row.get("worktrees", []))
        if not refusal:
            for wt in wts:
                st = git_state(Path(wt), worktree=True)
                act.worktrees.append((wt, st.get("main") or str(_main_from_gitfile(Path(wt)) or "")))
                text, risk = git_reason(st, worktree=True)
                if risk and c.row["safety"] != "check":
                    # Dirty, unpushed or busy since the scan: never rmtree it.
                    refusal = f"worktree {wt}: {text}; select it under `check` to override"
                    break
                if "broken" not in st and st.get("unpushed", 0) != 0:
                    act.note = f"worktree {wt} has unpushed commits (explicit check selection)"
        if refusal:
            act.method, act.note = "refuse", refusal
        elif c.row["safety"] == "regenerable":
            act.method = "rmtree"
        elif same_volume(p, trash_dir.parent):
            act.method = "trash"
        else:
            act.method, act.note = "refuse", "not on the Trash's volume; delete by hand if sure"
        actions.append(act)
    return actions


def refuse_reason(p: Path, roots: list[Path]) -> str:
    if not any(under(p, r) for r in roots):
        return "outside the scanned roots"
    if any(under(r, p) for r in roots):
        return "is (or contains) a scan root"
    if p in PROTECTED:
        return "protected folder"
    try:
        st = os.lstat(p)
    except OSError:
        return "no longer exists"
    if stat.S_ISLNK(st.st_mode):
        return "is a symlink"
    return ""


def same_volume(a: Path, b: Path) -> bool:
    try:
        b.mkdir(parents=True, exist_ok=True)
        return os.lstat(a).st_dev == os.lstat(b).st_dev
    except OSError:
        return False


def print_plan(actions: list[Action], out=sys.stdout) -> None:
    out.write("\nPlan:\n")
    for a in actions:
        verb = {"rmtree": "delete", "trash": "trash ", "refuse": "SKIP  "}[a.method]
        extra = f"   ({a.note})" if a.note else ""
        out.write(f"  {verb} {human(a.size):>8}  {a.path}{extra}\n")
    go = [a for a in actions if a.method != "refuse"]
    out.write(f"Would free {human(sum(a.size for a in go if a.method == 'rmtree'))} now, "
              f"move {human(sum(a.size for a in go if a.method == 'trash'))} to the Trash.\n")


def execute(actions: list[Action], trash_dir: Path, out=sys.stdout) -> None:
    for a in actions:
        if a.method == "refuse":
            continue
        p = Path(a.path)
        try:
            if a.method == "rmtree":
                for wt, main in a.worktrees:
                    if Path(wt).exists() and main and Path(main).exists():
                        r = git(Path(main), "worktree", "remove", "--force", "--force", wt)
                        if r.returncode != 0:
                            raise OSError(f"git worktree remove {wt}: {r.stderr.strip()}")
                if p.exists():
                    rmtree(p)
            else:
                trash_dir.mkdir(parents=True, exist_ok=True)
                dest = trash_dir / str(p).strip("/").replace("/", "__")
                os.rename(p, dest)
            for _wt, main in a.worktrees:
                if main and Path(main).exists():
                    git(Path(main), "worktree", "prune")
            out.write(f"  {'deleted' if a.method == 'rmtree' else 'trashed'} {a.path}\n")
        except (OSError, subprocess.TimeoutExpired) as exc:
            out.write(f"  FAILED {a.path}: {exc}\n")


def rmtree(p: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(p, onexc=_make_writable_and_retry)
    else:
        shutil.rmtree(p, onerror=_make_writable_and_retry)


def _make_writable_and_retry(func, path, _exc) -> None:
    """Go's module cache and some build outputs are read-only; rmtree needs write on the parent."""
    os.chmod(os.path.dirname(path), 0o700)
    if os.path.isdir(path) and not os.path.islink(path):
        os.chmod(path, 0o700)
    func(path)


def free_bytes(paths: list[str]) -> dict[int, int]:
    vols: dict[int, int] = {}
    for p in paths:
        try:
            dev = os.lstat(p).st_dev
            vols[dev] = shutil.disk_usage(p).free
        except OSError:
            pass
    return vols


def cmd_clean(args, inp: Callable[[str], str] = input, out=sys.stdout) -> int:
    result = json.loads(Path(args.from_).read_text())
    cands = candidates(result, args.include_ask)
    if not cands:
        out.write("Nothing to clean in that scan.\n")
        return 0
    trash_dir = HOME / ".Trash" / f"diskspace-{time.strftime('%Y%m%d-%H%M%S')}"
    if args.trash_dir:
        trash_dir = Path(args.trash_dir)
    while True:
        print_candidates(cands, out)
        try:
            line = inp("Toggle rows (e.g. 3 5-7, none), `go` to continue, `q` to quit: ").strip()
        except EOFError:
            return 1
        if line in ("q", "quit"):
            return 1
        if line == "go":
            break
        toggle(cands, line, out)
    actions = plan(cands, result["roots"], trash_dir)
    print_plan(actions, out)
    if args.dry_run or not any(a.method != "refuse" for a in actions):
        out.write("Dry run: nothing removed.\n" if args.dry_run else "Nothing to do.\n")
        return 0
    try:
        ok = inp("Proceed? [y/N] ").strip().lower() == "y"
    except EOFError:
        ok = False
    if not ok:
        out.write("Nothing removed.\n")
        return 1
    before = free_bytes(result["roots"])
    execute(actions, trash_dir, out)
    after = free_bytes(result["roots"])
    freed = sum(after.get(d, 0) - b for d, b in before.items())
    out.write(f"Free space change (df): {human(freed)}\n")
    if any(a.method == "trash" for a in actions):
        out.write(f"Trashed items are in {trash_dir}; empty the Trash to free that space.\n")
    return 0


# ---------------------------------------------------------------- docker
def cmd_docker(args, inp: Callable[[str], str] = input, out=sys.stdout,
               run: Callable = subprocess.run, raw: Path = DOCKER_RAW) -> int:
    try:
        st = os.stat(raw)
        out.write(f"Docker.raw: {human(st.st_blocks * 512)} on disk, "
                  f"{human(st.st_size)} apparent ({raw})\n")
    except OSError:
        out.write(f"Docker.raw not found at {raw}\n")
    try:
        r = run(["docker", "system", "df"], capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        out.write("docker CLI not found on PATH.\n")
        return 1
    except subprocess.TimeoutExpired:
        out.write("The Docker engine did not answer `docker system df` within 20 s: it is hung.\n"
                  "Manual step: quit and restart Docker Desktop (whale menu > Restart), "
                  "then rerun `diskspace.py docker`.\n")
        return 1
    if r.returncode != 0:
        out.write(f"`docker system df` failed: {r.stderr.strip()}\n"
                  "Manual step: start or restart Docker Desktop, then rerun.\n")
        return 1
    out.write(r.stdout + "\n")
    steps = [("safe", ["docker", "builder", "prune", "-af"], "build cache"),
             ("safe", ["docker", "image", "prune", "-af"], "images not used by a container"),
             ("ask", ["docker", "container", "prune", "-f"], "STOPPED containers (their data goes)"),
             ("ask", ["docker", "volume", "prune", "-f"], "unused VOLUMES (databases live here)")]
    for kind, cmd, what in steps:
        try:
            yes = inp(f"[{kind}] {' '.join(cmd)}  # removes {what}. Run? [y/N] ").strip().lower() == "y"
        except EOFError:
            break
        if yes:
            try:
                res = run(cmd, capture_output=True, text=True, timeout=600)
                out.write((res.stdout or res.stderr).strip() + "\n")
            except subprocess.TimeoutExpired:
                out.write("  timed out after 600 s\n")
    out.write("Docker.raw shrinks only after Docker Desktop reclaims space "
              "(it does so in the background, or on restart).\n")
    return 0


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="find the big directories (read-only)")
    s.add_argument("roots", nargs="*", type=Path)
    s.add_argument("--min-size", default="500M", help="smallest row to report (default 500M)")
    s.add_argument("--json", type=Path, help="where to write the scan (default: session tmp)")
    s.add_argument("--top", type=int, default=40, help="rows to print (default 40)")
    s.add_argument("--deep-containers", action="store_true",
                   help="also walk other apps' ~/Library/Containers (macOS may prompt per app)")
    c = sub.add_parser("clean", help="interactively remove rows from a scan")
    c.add_argument("--from", dest="from_", required=True, type=Path)
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--include-ask", action="store_true")
    c.add_argument("--trash-dir", help=argparse.SUPPRESS)
    sub.add_parser("docker", help="Docker.raw size and engine prunes")
    args = ap.parse_args(argv)

    if args.cmd == "scan":
        result = scan(args.roots or default_roots(), parse_size(args.min_size), args.deep_containers)
        print_table(result, args.top)
        path = args.json or default_json_path()
        path.write_text(json.dumps(result, indent=1))
        print(f"\nFull scan: {path}\nNext: diskspace.py clean --from {path}")
        return 0
    if args.cmd == "clean":
        return cmd_clean(args)
    return cmd_docker(args)


if __name__ == "__main__":
    sys.exit(main())
