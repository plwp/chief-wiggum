#!/usr/bin/env python3
"""duplication.py — production copy/paste ratio via jscpd.

Literature signal (GitClear [VENDOR]): duplicated/copy-pasted code is one of the
"AI-slop" signals the field converged on — code "written to be added, not
refactored/reused." GitClear baselines: pre-AI 2020 ~8.3% duplicated blocks,
AI-assisted 2024 ~12.3%. We measure PRODUCTION code only (tests, node_modules,
docs, vendor, build output excluded) so the figure is comparable to those bands.

Runs the ``jscpd`` CLI (npm i -g jscpd, or ``npx jscpd``) and parses its
``jscpd-report.json`` ``statistics.total``. Requires node + jscpd; if either is
absent, ``analyze`` returns ``{"skipped": ...}`` rather than raising.

As a module:
    from quality.duplication import analyze
    result = analyze("/path/to/repo", workdir="/tmp/dup")

As a CLI:
    python3 -m quality.duplication <repo> --workdir <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys

from . import cache, population

# Production-only: exclude tests, generated, vendored, docs, node output.
IGNORE = ",".join([
    "**/node_modules/**", "**/dist/**", "**/build/**", "**/out/**", "**/.next/**",
    "**/vendor/**", "**/.venv/**", "**/venv/**", "**/__pycache__/**",
    "**/coverage/**", "**/docs/**", "**/migrations/**",
    "**/*_test.go", "**/*.test.*", "**/*.spec.*",
    "**/test_*.py", "**/*_test.py", "**/tests/**", "**/__tests__/**", "**/e2e/**",
])
# Kept in step with quality.complexity.EXT_LANG / config/languages.json —
# a format missing here means those files are invisible to clone detection
# (#259: 8,316 .cs files contributed 0 clone classes).
FORMATS = "python,go,typescript,tsx,javascript,jsx,csharp"

# #265: jscpd ran ~177s and died at the default ~4 GB V8 ceiling, and the crash
# was recorded as an ordinary skip. Both axes now have a ceiling, and the child
# gets an explicit heap so the limit is ours rather than whatever V8 defaults
# to. Overridable per-run for a genuinely large corpus.
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_OLD_SPACE_MB = 4096
# argv is finite (`getconf ARG_MAX`, 1 MiB on macOS, and the environment counts
# against it too). A quarter of that is ~6k repo-relative paths — far beyond any
# "narrow domain scope", and we degrade LOUDLY rather than silently past it.
ARGV_BUDGET_BYTES = 256 * 1024


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _run_capture(cmd: list[str], *, cwd: str | None, env: dict, timeout: int):
    """Run ``cmd`` capturing output, killing the whole PROCESS GROUP on timeout.

    ``subprocess.run(timeout=...)`` kills only the direct child. jscpd is often
    reached via ``npx``, which spawns node as a grandchild — so a plain timeout
    would reap the wrapper and leave the process that is actually exhausting the
    heap running. Same discipline as ``consult_ai._run_group``."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            # AttributeError: killpg/getpgid are POSIX-only. Letting it escape
            # would abort the whole run with a traceback on Windows instead of
            # returning the structured `crashed` payload this change exists for.
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (AttributeError, ProcessLookupError, PermissionError, OSError):
            proc.kill()
        proc.wait(timeout=10)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _round(v: float | None, ndigits: int = 2) -> float | None:
    return round(v, ndigits) if isinstance(v, (int, float)) else v


def _jscpd_cmd() -> list[str] | None:
    """Resolve how to invoke jscpd: direct binary, else npx. None if node absent."""
    direct = shutil.which("jscpd")
    if direct:
        return [direct]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "jscpd"]
    return None


def _build_scratch_corpus(repo: str, files: list[str], workdir: str) -> str:
    """Mirror ``files`` (repo-relative) into a scratch tree under ``workdir``,
    preserving relative paths, so ONE jscpd invocation can scan an
    argv-over-budget corpus without widening to the repo root (#279).

    Symlinked where possible (cheap, no copy of file contents); falls back to
    a real copy PER FILE where the platform disallows symlinks, so a handful
    of unsymlinkable files degrade gracefully rather than aborting the whole
    build. Raises ``OSError`` if the scratch tree itself cannot be built (e.g.
    the workdir is unwritable) — the caller treats that as the genuinely
    unavoidable widening this ticket still allows.
    """
    scratch_root = os.path.join(workdir, "corpus")
    if os.path.isdir(scratch_root):
        shutil.rmtree(scratch_root)
    os.makedirs(scratch_root, exist_ok=True)
    for rel in files:
        src = os.path.join(repo, rel)
        dest = os.path.join(scratch_root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            os.symlink(os.path.abspath(src), dest)
        except OSError:
            shutil.copy2(src, dest)
    return scratch_root


def run_jscpd(repo: str, workdir: str, files: list[str] | None = None,
              timeout_seconds: int | None = None,
              max_old_space_mb: int | None = None) -> tuple[dict | None, dict | None]:
    """The ONE jscpd invocation both consumers share (#214): this module's
    aggregate percentage and ``clones.py``'s clone-class clustering. Returns
    ``(report, None)`` with the full parsed ``jscpd-report.json`` (statistics
    AND per-clone ``duplicates`` locations), or ``(None, problem)``.

    ``files`` is the EXPLICIT corpus (repo-relative paths). ``None`` keeps the
    historical whole-repo walk — the aggregate percentage in :func:`analyze` is
    calibrated against GitClear's repo-wide bands, so narrowing it silently
    would change what that number means. ``clones.py`` passes a scope-narrowed
    list instead, so a narrow ``scope.json`` genuinely reduces the work (#265):
    before this, scope was applied only AFTER jscpd had already scanned
    everything, and a 61-file scope still exhausted a 4 GB heap.

    ``problem`` distinguishes two things that used to look identical:
      * ``{"status": "skipped"}`` — a declared limitation (the tool is absent).
      * ``{"status": "crashed"}`` — the tool was expected to run and DIED.
        A crash is a defect, not a known gap. The legacy ``skipped`` key is kept
        on both so existing consumers still branch correctly on "no data"."""
    cmd = _jscpd_cmd()
    if not cmd:
        return None, {
            "status": "skipped",
            "skipped": "jscpd/node not found",
            "note": "duplication requires node + jscpd (npm i -g jscpd)",
        }

    # #328/#214: this is the ONE invocation duplication.analyze's aggregate
    # percentage and clones.py's clone-class clustering are meant to share —
    # but they are two separate CLI processes in /close-epic (quality_slop_
    # gate.py and debt_inventory.py), so an in-memory memo alone would not
    # help. cache_key is a content hash of the exact corpus (whole-repo when
    # `files` is None — the common unscoped case both consumers hit — or the
    # explicit scope-narrowed list), built via chief_wiggum.manifest so a
    # dirty/untracked edit busts it even with HEAD unchanged (the doctrine:
    # never reuse a stale report because a re-run "looked" unchanged).
    cache_key = cache.manifest_key(repo, files)
    cached = cache.load(repo, "jscpd", cache_key)
    if cached is not None:
        return cached, None

    os.makedirs(workdir, exist_ok=True)
    timeout = timeout_seconds or _env_int("CW_JSCPD_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    heap_mb = max_old_space_mb or _env_int("CW_JSCPD_MAX_OLD_SPACE_MB", DEFAULT_MAX_OLD_SPACE_MB)

    fallback: str | None = None
    corpus_root = repo
    scratch_used = False
    if files is None:
        targets = [repo]
    elif sum(len(f) + 1 for f in files) > ARGV_BUDGET_BYTES:
        # #279: build a scratch tree mirroring just the in-scope files and
        # scan THAT in a single invocation, instead of widening to the repo
        # root. The repo-root widening is now reserved for the genuinely
        # unavoidable case — the scratch build itself failing (e.g. an
        # unwritable workdir) — and is still reported LOUDLY via
        # `corpus_fallback`, same discipline as #265.
        try:
            scratch_root = _build_scratch_corpus(repo, files, workdir)
        except OSError as exc:
            targets = [repo]
            fallback = (f"argv budget exceeded ({len(files)} files) and the scratch "
                        f"corpus tree could not be built ({exc}) — scanned the repo "
                        "root instead; clone findings are NOT scope-narrowed")
        else:
            targets = [scratch_root]
            corpus_root = scratch_root
            scratch_used = True
    else:
        targets = list(files)

    # `cwd` ONLY for the explicit small-corpus case, whose paths are
    # repo-relative. Setting it for a whole-repo (or scratch-tree) scan would
    # re-anchor a RELATIVE target against itself ("src/app" ->
    # "src/app/src/app"), which jscpd resolves to nothing and reports as 0
    # sources — a silent empty scan, not even a crash. The scratch tree is
    # handed to jscpd as an absolute path for exactly this reason (mirrors
    # the whole-repo-scan convention below), and `clones.py` remaps the
    # absolute scratch-relative paths jscpd reports back to repo-relative
    # ones via `_corpus_root`.
    cwd = repo if (files is not None and not fallback and not scratch_used) else None

    env = dict(os.environ)
    node_opts = f"{env.get('NODE_OPTIONS', '')} --max-old-space-size={heap_mb}".strip()
    env["NODE_OPTIONS"] = node_opts

    report = os.path.join(workdir, "jscpd-report.json")
    # A run is proven by the report IT wrote. `workdir` may be reused, so a
    # report left by an earlier successful run would let a crashed jscpd parse
    # stale results and return `measured` — reinstating the exact silent-success
    # failure this change exists to remove.
    try:
        os.unlink(report)
    except FileNotFoundError:
        pass

    try:
        proc = _run_capture(
            [
                *cmd, *targets,
                "--reporters", "json",
                "--output", workdir,
                "--ignore", IGNORE,
                "--format", FORMATS,
                "--mode", "strict",
                "--silent",
            ],
            cwd=cwd, env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, {
            "status": "crashed",
            "crashed": f"jscpd timed out after {timeout}s",
            "skipped": f"jscpd timed out after {timeout}s",
            "note": f"corpus: {len(targets)} target(s); raise CW_JSCPD_TIMEOUT_SECONDS to allow longer",
            "exit_code": None,
            "corpus_fallback": fallback,
        }
    except OSError as exc:
        # The child could not be started at all (unreadable cwd, exec failure).
        # Expected-to-run and did not: a crash, not a declared limitation.
        return None, {
            "status": "crashed",
            "crashed": f"jscpd could not be started: {exc}",
            "skipped": f"jscpd could not be started: {exc}",
            "exit_code": None,
            "corpus_fallback": fallback,
        }
    if not os.path.exists(report):
        # The #265 shape: jscpd was present, was expected to run, and died —
        # historically indistinguishable from a language with no clone tier.
        return None, {
            "status": "crashed",
            "crashed": "jscpd produced no report",
            "skipped": "jscpd produced no report",
            "note": (proc.stderr or proc.stdout or "").strip()[:400],
            "exit_code": proc.returncode,
            "corpus_fallback": fallback,
        }
    try:
        with open(report) as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        return None, {
            "status": "crashed",
            "crashed": f"unreadable jscpd report: {exc}",
            "skipped": f"unreadable jscpd report: {exc}",
            "exit_code": proc.returncode,
            "corpus_fallback": fallback,
        }
    if fallback or scratch_used:
        data = dict(data)
        if fallback:
            data["_corpus_fallback"] = fallback
        if scratch_used:
            # #279: tells clones.py what root jscpd's reported paths are
            # relative to (or absolute under), so it can remap them back to
            # repo-relative instead of leaving them pointed at this scratch dir.
            data["_corpus_root"] = corpus_root
    # Only a genuinely successful measurement is memoized — a crash/skip
    # (handled by the early returns above) must never be cached, or the very
    # next call would keep replaying a broken instrument's non-result forever.
    cache.store(repo, "jscpd", cache_key, data)
    return data, None


def _production_candidate_count(repo: str) -> int:
    """Independent candidate count of PRODUCTION (non-test) source files in
    the languages this engine scans — reuses ``quality.population``'s shared
    tracked/excluded/generated file population (the SAME definition the debt
    engines use), so this engine can't silently disagree with the rest of the
    quality battery about what counts as production source. Used ONLY to
    disambiguate a jscpd-reported ``sources: 0`` (#289): a genuinely
    source-free corpus (inapplicable, honest absence) is otherwise
    indistinguishable from a wrong ignore glob or language mismatch hiding
    real content from the scanner (a broken instrument)."""
    return sum(
        1 for f in population.tracked_source(repo) if not population.is_test_file(f)
    )


def analyze(repo: str, workdir: str, name: str | None = None) -> dict:
    """Run jscpd over production code and return the duplication statistics."""
    name = name or repo.rstrip("/").split("/")[-1]
    data, skip = run_jscpd(repo, workdir)
    if skip is not None:
        return {"repo": name, **skip}
    try:
        stats = data["statistics"]["total"]
    except KeyError as exc:
        return {
            "repo": name, "status": "crashed",
            "crashed": f"unreadable jscpd report: missing {exc}",
            "skipped": f"unreadable jscpd report: missing {exc}",
        }

    if not stats.get("sources"):
        # #289: the audit's exact defect — a zero-source corpus reporting
        # "0.0% duplication (beats the pre-AI human baseline)", the healthiest
        # band available. Disambiguate with an INDEPENDENT candidate count
        # rather than trusting jscpd's own zero at face value.
        candidates = _production_candidate_count(repo)
        if candidates == 0:
            return {
                "repo": name, "status": "inapplicable", "sources": 0,
                "inapplicable": (
                    f"no production source files found in the languages this "
                    f"engine scans ({FORMATS}) — nothing to measure"
                ),
            }
        return {
            "repo": name, "status": "crashed", "sources": 0,
            "crashed": (
                f"jscpd scanned 0 sources despite {candidates} production "
                "source file(s) present — an ignore glob or language "
                "mismatch hid real content from the scanner"
            ),
            "skipped": (
                f"jscpd scanned 0 sources despite {candidates} production "
                "source file(s) present"
            ),
        }

    return {
        "repo": name,
        "lines": stats.get("lines"),
        "tokens": stats.get("tokens"),
        "sources": stats.get("sources"),
        "clones": stats.get("clones"),
        "duplicated_lines": stats.get("duplicatedLines"),
        "duplicated_tokens": stats.get("duplicatedTokens"),
        "duplication_pct_lines": _round(stats.get("percentage")),
        "duplication_pct_tokens": _round(stats.get("percentageTokens")),
        "baselines": {
            "gitclear_pre_ai_2020": 8.3,
            "gitclear_ai_2024": 12.3,
            "note": "GitClear [VENDOR] copy/paste baselines (% duplicated blocks); "
                    "direction is credible, exact multiples are framing-dependent.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="production code duplication (jscpd)")
    parser.add_argument("repo", help="path to the git repository")
    parser.add_argument("--workdir", required=True, help="scratch dir for jscpd output")
    parser.add_argument("--name", default=None, help="display name for the repo")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="force a fresh jscpd run, bypassing the content-hash result cache (#328)",
    )
    args = parser.parse_args()
    if args.no_cache:
        os.environ[cache.NO_CACHE_ENV] = "1"
    print(json.dumps(analyze(args.repo, args.workdir, name=args.name), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
