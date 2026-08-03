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
    """A file that is unreadable at the OS level — permissions, race, broken
    symlink — must be surfaced as a skip reason, never a silent ``continue``
    and never a crash.

    (This is not the only skip case: a file that decodes to mostly replacement
    characters is also reported unscanned rather than scanned-as-garbage — see
    ``test_binary_file_is_reported_unscanned_not_scanned_as_garbage``.)"""
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


# --- BOM-less UTF-16: the silent-clean hole (chief-wiggum#282, #289) ----------
#
# These are the cases a naive `utf-8, errors="replace"` fallback gets WRONG in
# the most dangerous possible way. ASCII text encoded UTF-16-LE is
# `s\x00t\x00r\x00...`; NUL is valid UTF-8, so `raw.decode("utf-8")` SUCCEEDS
# with ZERO replacement characters and returns text no source regex can match.
# The scan then reports the file clean having never read a word of it — so a
# replacement-ratio heuristic cannot catch it either (the ratio is 0%).

_WRITE_SITE = 'func ChangePlan() { bson.M{"$set": bson.M{"stripe_plan": v}} }\n'


def test_bom_less_utf16_le_is_decoded_not_silently_blanked(tmp_path):
    p = tmp_path / "nobom_le.go"
    p.write_bytes(_WRITE_SITE.encode("utf-16-le"))
    text, reason = read_text_safe(p)
    assert reason is None, reason
    assert "stripe_plan" in text, (
        "a BOM-less UTF-16-LE file decoded to text the write-site regex cannot "
        "match — the scan would report it clean without reading it (#282/#289)"
    )


def test_bom_less_utf16_be_is_decoded_not_silently_blanked(tmp_path):
    p = tmp_path / "nobom_be.go"
    p.write_bytes(_WRITE_SITE.encode("utf-16-be"))
    text, reason = read_text_safe(p)
    assert reason is None, reason
    assert "stripe_plan" in text


def test_binary_file_is_reported_unscanned_not_scanned_as_garbage(tmp_path):
    # Mostly-replacement-character output means the file was not meaningfully
    # read. Returning it as if scanned is the same silent-clean lie.
    p = tmp_path / "blob.go"
    p.write_bytes(bytes(range(256)) * 4)
    text, reason = read_text_safe(p)
    assert text is None
    assert reason is not None and "undecodable" in reason


def test_plain_utf8_and_latin1_are_unaffected(tmp_path):
    # No new noise on the overwhelmingly common cases: clean UTF-8 must decode
    # exactly, and a mildly-dirty latin-1 file must still be SCANNED (lossily)
    # rather than dropped — a replaced accent cannot fabricate a write site.
    p8 = tmp_path / "clean.go"
    p8.write_text(_WRITE_SITE, encoding="utf-8")
    text, reason = read_text_safe(p8)
    assert reason is None and text == _WRITE_SITE

    p1 = tmp_path / "accents.go"
    p1.write_bytes("func Ré() { stripe_plan }\n".encode("latin-1"))
    text, reason = read_text_safe(p1)
    assert reason is None, reason
    assert "stripe_plan" in text
