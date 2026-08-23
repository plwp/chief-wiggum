"""Secret-provisioning instructions must not pipe from `echo` (chief-wiggum#370).

Prod's `internal-reconcile-secret-prod` was created with `echo`, so its value
carried a trailing `0x0a`. Cloud Run injects secret bytes verbatim; the service
compared the header against the env value untrimmed; and **an HTTP header value
can never end in a newline**.

The prod reconcile endpoint was therefore structurally unreachable by every
possible caller from the day it shipped, and nobody knew for a month. Staging's
secret happened to lack the newline, so tests and staging never surfaced it.

One byte, invisible in every dashboard that renders it.

The same class breaks webhook HMAC verification (the signature is computed over
the wrong bytes) and any outbound `Authorization: Bearer` header (Go's net/http
hard-rejects newlines in header values).

CW ships the instructions that create these secrets. This test keeps every one
of them on `printf %s`, which emits exactly the bytes given and nothing more.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Directories whose text CW hands to a human or an agent to execute.
SEARCH_ROOTS = ("patterns", "skills", ".claude/commands", "docs", "templates")
SUFFIXES = {".md", ".sh", ".yml", ".yaml"}

# Commands that WRITE a secret value. Each of these reads the value from stdin
# or an argument, and is where a stray newline enters the system.
SECRET_WRITE = re.compile(
    r"gcloud\s+secrets\s+versions\s+add"
    r"|aws\s+secretsmanager\s+(put-secret-value|create-secret)"
    r"|vault\s+kv\s+put"
    r"|az\s+keyvault\s+secret\s+set",
)

# `echo` appends a newline unless -n is passed, and `echo -n` is not portable
# (it prints "-n" literally in some shells). `printf %s` is the only form that
# is both exact and portable, so it is the only one accepted.
ECHO = re.compile(r"(^|[|;&(]\s*)echo\b")


def candidate_files() -> list[Path]:
    files: list[Path] = []
    for root in SEARCH_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        files.extend(p for p in sorted(base.rglob("*"))
                     if p.is_file() and p.suffix in SUFFIXES)
    return files


def secret_write_lines() -> list[tuple[Path, int, str]]:
    """Backslash continuations are JOINED before matching.

    The shipped instruction wraps, putting `--data-file=-` on the line after
    the command. Matching line-by-line silently skipped the stdin check on the
    only real instruction in the repo — found by mutation testing, not by
    reading it.
    """
    out = []
    for path in candidate_files():
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        joined, start, buf = [], 1, ""
        for lineno, line in enumerate(text.split("\n"), 1):
            if not buf:
                start = lineno
            if line.rstrip().endswith("\\"):
                buf += line.rstrip()[:-1] + " "
                continue
            joined.append((start, buf + line))
            buf = ""
        if buf:
            joined.append((start, buf))
        for lineno, line in joined:
            if SECRET_WRITE.search(line):
                out.append((path, lineno, line))
    return out


def test_there_are_files_and_instructions_to_check():
    """A denominator. If the globs ever stop matching, this file must fail
    loudly rather than pass by checking nothing — the shape of vacuous pass
    this repo keeps paying for (#289)."""
    assert len(candidate_files()) > 50
    assert len(secret_write_lines()) >= 1, (
        "no secret-provisioning instruction found anywhere — either the search "
        "roots are wrong or the instructions moved; either way this guard is "
        "no longer guarding anything")


def test_no_secret_is_provisioned_from_echo():
    offenders = []
    for path, lineno, line in secret_write_lines():
        if ECHO.search(line):
            offenders.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()[:120]}")
    assert offenders == [], (
        "secret-provisioning instruction pipes from `echo`, which appends a "
        "trailing newline (chief-wiggum#370). An HTTP header value can never end "
        "in a newline, so the endpoint becomes unreachable by every caller. Use "
        "`printf %s`:\n" + "\n".join(offenders)
    )


def test_every_stdin_provisioning_instruction_uses_printf():
    """Stronger than "not echo": a value piped from anything unstated is a
    value nobody has checked the bytes of."""
    offenders = []
    for path, lineno, line in secret_write_lines():
        reads_stdin = "--data-file=-" in line or "--data-file -" in line
        if reads_stdin and "printf %s" not in line and "printf '%s'" not in line:
            offenders.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()[:120]}")
    assert offenders == [], (
        "a secret is piped in from something other than `printf %s`; the bytes "
        "written are then whatever that producer chose to append "
        "(chief-wiggum#370):\n" + "\n".join(offenders)
    )


def test_the_reasoning_is_recorded_where_the_pattern_stamps_it():
    """The rule is only durable if the WHY travels with it — a future author
    reading `printf %s` with no explanation will 'simplify' it back to echo."""
    pattern = REPO / "patterns" / "fetch-on-webhook-reconcile" / "pattern.md"
    text = pattern.read_text()
    assert "trailing newline" in text.lower()
    assert "printf" in text
    assert "TrimSpace" in text
