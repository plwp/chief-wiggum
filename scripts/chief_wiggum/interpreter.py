"""Resolve a VALIDATED Python interpreter for CW to run its scripts under.

The failure this prevents (chief-wiggum#374): skills hardcode `python3` at
roughly forty call sites, so CW's runtime interpreter is whatever the shell
happens to resolve. Homebrew bumping `python3` from 3.11 to 3.13 silently
stranded every dependency installed for the old one, and three scripts died
mid-pipeline on missing modules while a working `python3.11` sat right there.

The key word is validated. `check_deps.py --for core` verified keyring under
the interpreter IT was running as, which says nothing about the interpreter the
skills will actually invoke. So every import here is probed by running it
inside the candidate, as a subprocess, not by importing it in this process.

Resolution order, first validated candidate wins:

1. ``CW_PYTHON`` — an explicit operator override, always tried first.
2. ``~/.chief-wiggum/venv/bin/python`` — the canonical managed runtime.
3. The interpreter running this code.
4. Named fallbacks (``python3.13`` down to ``python3.11``, then ``python3``).

Nothing here installs anything. When no candidate validates, the error names
the missing modules and the exact uv command to fix it, because the operator
cost last time was three failed launches and three ad-hoc pip repairs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

CW_HOME_DIR = Path.home() / ".chief-wiggum"
VENV_PYTHON = CW_HOME_DIR / "venv" / "bin" / "python"
CACHE_PATH = CW_HOME_DIR / "interpreter.json"

# Import name -> the distribution that provides it, for the remediation line.
PACKAGE_FOR_IMPORT = {
    "keyring": "keyring",
    "jsonschema": "jsonschema",
    "referencing": "referencing",
    "google.genai": "google-genai",
    "google.cloud.aiplatform": "google-cloud-aiplatform",
    "anthropic": "anthropic",
    "yaml": "pyyaml",
}

# What each profile needs. Profiles mirror check_deps.py's vocabulary.
PROFILES: dict[str, tuple[str, ...]] = {
    "core": ("keyring",),
    "consult": ("keyring",),
    "formal": ("jsonschema", "referencing"),
    "vertex": ("google.genai", "google.cloud.aiplatform"),
}

FALLBACK_NAMES = ("python3.13", "python3.12", "python3.11", "python3")


class NoValidInterpreter(RuntimeError):
    """No candidate had the required modules. Carries the remediation command."""


@dataclass(frozen=True)
class Candidate:
    path: str
    source: str
    version: str = ""
    missing: tuple[str, ...] = ()
    error: str = ""

    @property
    def valid(self) -> bool:
        return not self.missing and not self.error

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "source": self.source,
            "version": self.version,
            "missing": list(self.missing),
            "error": self.error,
            "valid": self.valid,
        }


@dataclass
class Resolution:
    python: str
    source: str
    version: str
    profiles: tuple[str, ...] = ()
    considered: list[Candidate] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "python": self.python,
            "source": self.source,
            "version": self.version,
            "profiles": list(self.profiles),
            "considered": [candidate.to_dict() for candidate in self.considered],
        }


def modules_for(profiles: Iterable[str]) -> tuple[str, ...]:
    """Union of every named profile's required imports."""
    required: list[str] = []
    for profile in profiles:
        if profile not in PROFILES:
            raise ValueError(
                f"unknown profile {profile!r}; known profiles: {', '.join(sorted(PROFILES))}"
            )
        for module in PROFILES[profile]:
            if module not in required:
                required.append(module)
    return tuple(required)


def remediation(python: str, missing: Sequence[str]) -> str:
    """The exact command to fix it. uv, because that is the house rule."""
    packages = sorted({PACKAGE_FOR_IMPORT.get(module, module) for module in missing})
    return f"uv pip install --python {python} {' '.join(packages)}"


def probe(python: str, modules: Sequence[str], *, timeout: float = 30.0) -> Candidate:
    """Ask the CANDIDATE whether it can import these, by running it.

    Importing in this process would only ever describe this process. That gap
    is exactly what stranded the dependencies in the first place.
    """
    source = "explicit"
    resolved = shutil.which(python) or python
    script = (
        "import json,sys,importlib.util\n"
        f"mods={list(modules)!r}\n"
        "missing=[m for m in mods if importlib.util.find_spec(m) is None]\n"
        "print(json.dumps({'version': sys.version.split()[0], 'missing': missing}))\n"
    )
    try:
        result = subprocess.run(
            [resolved, "-c", script], capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Candidate(resolved, source, error=f"could not run {python}: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return Candidate(
            resolved, source,
            error=f"{python} exited {result.returncode}: {detail[-1] if detail else 'no output'}",
        )
    try:
        payload = json.loads(result.stdout.strip() or "{}")
    except ValueError as exc:
        return Candidate(resolved, source, error=f"unparseable probe output: {exc}")
    return Candidate(
        resolved, source,
        version=str(payload.get("version", "")),
        missing=tuple(payload.get("missing", [])),
    )


def candidates() -> list[tuple[str, str]]:
    """(path, source) pairs in resolution order, de-duplicated."""
    ordered: list[tuple[str, str]] = []
    override = os.environ.get("CW_PYTHON")
    if override:
        ordered.append((override, "CW_PYTHON"))
    if VENV_PYTHON.exists():
        ordered.append((str(VENV_PYTHON), "cw-venv"))
    ordered.append((sys.executable, "current"))
    for name in FALLBACK_NAMES:
        found = shutil.which(name)
        if found:
            ordered.append((found, f"path:{name}"))

    seen: set[str] = set()
    unique = []
    for path, source in ordered:
        resolved = shutil.which(path) or path
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append((path, source))
    return unique


def resolve(
    profiles: Sequence[str] = ("core",),
    *,
    use_cache: bool = True,
    cache_path: Path | None = None,
) -> Resolution:
    """Find the first interpreter that can import everything the profiles need."""
    required = modules_for(profiles)
    cache_file = cache_path if cache_path is not None else CACHE_PATH

    if use_cache:
        cached = _read_cache(cache_file, profiles, required)
        if cached is not None:
            return cached

    considered: list[Candidate] = []
    for path, source in candidates():
        candidate = probe(path, required)
        candidate = Candidate(candidate.path, source, candidate.version,
                              candidate.missing, candidate.error)
        considered.append(candidate)
        if candidate.valid:
            resolution = Resolution(candidate.path, source, candidate.version,
                                    tuple(profiles), considered)
            _write_cache(cache_file, resolution)
            return resolution

    raise NoValidInterpreter(_failure_message(required, considered))


def _failure_message(required: Sequence[str], considered: Sequence[Candidate]) -> str:
    lines = [
        f"no Python interpreter can import: {', '.join(required)}",
        "Tried:",
    ]
    for candidate in considered:
        why = candidate.error or ("missing " + ", ".join(candidate.missing))
        lines.append(f"  {candidate.path} ({candidate.source}): {why}")
    best = next(
        (c for c in considered if not c.error and c.missing),
        considered[0] if considered else None,
    )
    if best is not None:
        lines.append("")
        lines.append("Fix the closest candidate with:")
        lines.append(f"  {remediation(best.path, best.missing or required)}")
        lines.append(f"Or point CW at a working interpreter: export CW_PYTHON=/path/to/python")
    return "\n".join(lines)


def _read_cache(
    cache_file: Path, profiles: Sequence[str], required: Sequence[str]
) -> Resolution | None:
    """Reuse a previous answer, but never trust it blindly.

    The cache is re-validated against the interpreter it names, because the
    whole defect being fixed is an interpreter changing underneath CW.
    """
    try:
        data = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        return None
    python = data.get("python")
    if not python:
        return None
    # Re-probe against the FULL required set rather than checking the file
    # exists or comparing recorded profiles. Both of those would be redundant:
    # a vanished interpreter and one that no longer covers the asked-for
    # profiles both fail this probe, and this is the check that matters.
    candidate = probe(python, required)
    if not candidate.valid:
        return None
    return Resolution(python, str(data.get("source", "cache")), candidate.version,
                      tuple(profiles), [candidate])


def _write_cache(cache_file: Path, resolution: Resolution) -> None:
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            "python": resolution.python,
            "source": resolution.source,
            "version": resolution.version,
            "profiles": list(resolution.profiles),
        }, indent=2))
    except OSError:
        # A cache that cannot be written is a slower resolve, not a failure.
        pass
