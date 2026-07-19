"""Content-addressed file manifest helper (#160).

A **manifest** is ``{repo-relative path: content hash}`` for the set of files a
scanner would walk. It is the future cache-validity key (a scanner's findings
for a file are valid as long as the file's hash in the manifest hasn't
changed) and, right now, the basis for ``--changed-since <ref>`` scoping on
``check_single_writer.py`` / ``check_traceability.py``: diff the manifest at a
ref against the current manifest and only the paths whose hash differs need
re-scanning.

``build_manifest`` is deliberately git-native rather than re-implementing
"what changed" from scratch:

    manifest = (git ls-tree -r HEAD)  ∪  (git hash-object over dirty tracked
                                           + untracked non-ignored files)
               minus deletions

- ``git ls-tree -r HEAD`` gives the committed blob hash for every tracked path
  — free, no file I/O.
- Dirty tracked files (staged or unstaged changes vs HEAD) and untracked,
  non-ignored files are re-hashed from the working tree with
  ``git hash-object`` — the same hashing git itself would use if the file were
  added, so manifest hashes are comparable across "committed" and "dirty"
  paths without a scheme change.
- A path deleted in the working tree (vs HEAD) is dropped, never surfaced with
  a stale hash.

The result is filtered through a caller-supplied ``predicate(path) -> bool` —
each scanner has its own file-selection rule (extension allow-list, skipped
directories, ``--exclude`` globs), and the manifest must reflect exactly the
set of files that scanner would otherwise walk, no more and no less.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

Predicate = Callable[[str], bool]


def _run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout


def _ls_tree(repo_root: Path, ref: str) -> dict[str, str]:
    """path -> blob sha for every tracked blob at ``ref``."""
    out = _run_git(["ls-tree", "-r", "-z", ref], repo_root)
    manifest: dict[str, str] = {}
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) != 3:
            continue
        _mode, obj_type, sha = parts
        if obj_type != "blob":
            continue
        manifest[path] = sha
    return manifest


def _dirty_and_untracked(repo_root: Path) -> tuple[set[str], set[str]]:
    """(present, deleted): tracked paths that differ from HEAD (working tree +
    index) plus untracked non-ignored paths, split by whether the path still
    exists on disk. Rename detection is disabled (``--no-renames``) so a rename
    surfaces as a plain delete + add pair — simpler and unambiguous for a
    content-addressed manifest (the new path's content is hashed either way)."""
    present: set[str] = set()
    deleted: set[str] = set()
    diff_out = _run_git(["diff", "--no-renames", "--name-status", "-z", "HEAD"], repo_root)
    tokens = [t for t in diff_out.split("\0") if t]
    i = 0
    while i < len(tokens):
        status, path = tokens[i], tokens[i + 1]
        i += 2
        if status.startswith("D"):
            deleted.add(path)
        else:
            present.add(path)
    untracked_out = _run_git(["ls-files", "--others", "--exclude-standard", "-z"], repo_root)
    present.update(p for p in untracked_out.split("\0") if p)
    return present, deleted


def _hash_object(repo_root: Path, path: Path) -> str:
    return _run_git(["hash-object", str(path)], repo_root).strip()


def build_manifest(repo_root: str | Path, predicate: Predicate | None = None) -> dict[str, str]:
    """``{path: content_hash}`` for the current working tree: ``git ls-tree -r
    HEAD`` unioned with re-hashed dirty tracked + untracked non-ignored files,
    minus deletions, filtered through ``predicate`` (default: everything)."""
    root = Path(repo_root)
    pred = predicate or (lambda _p: True)
    manifest = _ls_tree(root, "HEAD")
    present, deleted = _dirty_and_untracked(root)
    for path in deleted:
        manifest.pop(path, None)
    for path in present:
        full = root / path
        if not full.is_file():
            # Raced out from under us (deleted/replaced by a dir) between the
            # git status read and the hash-object call — drop rather than stale.
            manifest.pop(path, None)
            continue
        manifest[path] = _hash_object(root, full)
    return {p: h for p, h in manifest.items() if pred(p)}


def tree_manifest(repo_root: str | Path, ref: str, predicate: Predicate | None = None) -> dict[str, str]:
    """Pure ``git ls-tree`` manifest at ``ref`` — no working-tree overlay. The
    baseline ``--changed-since <ref>`` diffs the current ``build_manifest()``
    against."""
    pred = predicate or (lambda _p: True)
    manifest = _ls_tree(Path(repo_root), ref)
    return {p: h for p, h in manifest.items() if pred(p)}


def changed_paths(repo_root: str | Path, ref: str, predicate: Predicate | None = None) -> set[str]:
    """Paths whose content differs between ``ref`` and the current working tree
    (dirty + untracked included), restricted to ``predicate``. This is what
    ``--changed-since <ref>`` scans instead of the whole repo."""
    current = build_manifest(repo_root, predicate)
    baseline = tree_manifest(repo_root, ref, predicate)
    return {p for p in set(current) | set(baseline) if current.get(p) != baseline.get(p)}
