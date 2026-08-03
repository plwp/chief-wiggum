"""Tests for chief_wiggum.textio.read_text_safe (chief-wiggum#282).

A bare ``path.read_text()`` assumes UTF-8 and crashes with a bare
``UnicodeDecodeError`` on a UTF-16 (or otherwise non-UTF-8) file — no verdict,
and the traceback names the reader, not the offending file. ``read_text_safe``
is the shared decode boundary for BULK SOURCE SCANS (check_single_writer's
write-site scan, check_traceability's annotation scan): BOM-sniff UTF-8/UTF-16
(LE+BE), then fall back to UTF-8 with ``errors="replace"`` — a regex scan does
not need byte-perfect fidelity, and skipping a file outright is worse than a
replaced character (binding decision, #282).

Note config/model reads (``json.loads(path.read_text())``) are a DIFFERENT
contract and must NOT route through this helper — a decode failure there is a
real, actionable error and should surface loudly, naming the file.
"""

from __future__ import annotations

import os
import sys

import pytest
from chief_wiggum.textio import read_text_safe


def test_plain_utf8_round_trips(tmp_path):
    p = tmp_path / "plain.go"
    p.write_text("package main\nfunc f() {}\n", encoding="utf-8")
    text, reason = read_text_safe(p)
    assert reason is None
    assert text == "package main\nfunc f() {}\n"


def test_utf16_le_bom_is_decoded_correctly(tmp_path):
    p = tmp_path / "legacy.go"
    content = "package main\nfunc Bad(){ provider.stripe_plan = \"x\" }\n"
    p.write_bytes(content.encode("utf-16"))  # Python's utf-16 encoder writes a BOM
    text, reason = read_text_safe(p)
    assert reason is None
    assert text == content


def test_utf16_be_bom_is_decoded_correctly(tmp_path):
    p = tmp_path / "legacy_be.go"
    content = "package main\nfunc Bad(){ provider.stripe_plan = \"x\" }\n"
    p.write_bytes(b"\xfe\xff" + content.encode("utf-16-be"))
    text, reason = read_text_safe(p)
    assert reason is None
    assert text == content


def test_utf8_bom_is_stripped(tmp_path):
    p = tmp_path / "sig.py"
    p.write_bytes(b"\xef\xbb\xbf" + b"def f(): pass\n")
    text, reason = read_text_safe(p)
    assert reason is None
    assert text == "def f(): pass\n"


def test_invalid_utf8_without_bom_falls_back_to_lossy_replace(tmp_path):
    """No BOM, not valid UTF-8: falls back to errors='replace' rather than
    crashing or being dropped — a replaced character beats a skipped file."""
    p = tmp_path / "garbage.go"
    p.write_bytes(b"package main\n\xff\xfe\x80\x81 // garbage\n")
    text, reason = read_text_safe(p)
    assert reason is None
    assert text is not None
    assert "package main" in text  # the decodable prefix survives
    assert "�" in text  # invalid bytes were replaced, not dropped


def test_truncated_utf16_bom_falls_back_instead_of_crashing(tmp_path):
    """A BOM is present but the remaining bytes don't form valid UTF-16 (odd
    byte count) — real-world corruption. Must not raise; falls through to the
    UTF-8 lossy fallback rather than surfacing UnicodeDecodeError."""
    p = tmp_path / "truncated.go"
    p.write_bytes(b"\xff\xfe\x00binary")  # BOM + an odd, non-utf16 tail
    text, reason = read_text_safe(p)
    assert reason is None
    assert text is not None


def test_genuinely_unreadable_file_reports_skip_reason_with_no_crash(tmp_path):
    """The ONE case that is truly 'undecodable' under an errors='replace'
    fallback (which never raises for any byte sequence) is the file simply
    being unreadable at the OS level — permissions, race, broken symlink. This
    must be surfaced as a skip reason, never a silent ``continue`` and never a
    crash."""
    if sys.platform.startswith("win") or os.geteuid() == 0:
        pytest.skip("permission bits are not enforceable as root or on Windows")
    p = tmp_path / "noaccess.go"
    p.write_text("package main\n", encoding="utf-8")
    os.chmod(p, 0o000)
    try:
        text, reason = read_text_safe(p)
    finally:
        os.chmod(p, 0o644)  # restore so tmp_path cleanup can remove it
    assert text is None
    assert reason is not None
    assert "unreadable" in reason.lower() or "permission" in reason.lower()


def test_missing_file_reports_skip_reason(tmp_path):
    p = tmp_path / "gone.go"
    text, reason = read_text_safe(p)
    assert text is None
    assert reason is not None
