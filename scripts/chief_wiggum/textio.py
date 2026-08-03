"""Decode-defensive text reader for BULK SOURCE SCANS (chief-wiggum#282).

``check_single_writer.py``'s write-site scan and ``check_traceability.py``'s
``@cw-trace`` annotation scan both walk arbitrary repo-wide source files with a
bare ``path.read_text()``. That assumes UTF-8: a UTF-16 (or otherwise
non-UTF-8) file anywhere in the scanned population crashes the ENTIRE gate
with a bare ``UnicodeDecodeError`` — no verdict at all, and the traceback
names the reader, not the offending file. Worse, the pre-existing
``except OSError: continue`` guards around those reads do NOT catch it —
``UnicodeDecodeError`` is a ``ValueError`` subclass, not an ``OSError``.

``read_text_safe`` is the shared decode boundary for this class of scan:

1. BOM-sniff UTF-8 / UTF-16 (LE + BE) and decode accordingly.
2. If there's no BOM (or the BOM-implied decode itself fails — e.g. a
   truncated/corrupted UTF-16 file), fall back to UTF-8 with
   ``errors="replace"``.

A write-site/annotation regex scan does not need byte-perfect fidelity, and
skipping a file outright is worse than a replaced character — so step 2 is
deliberately lossy rather than a second failure mode (binding decision,
chief-wiggum#282). Because ``errors="replace"`` never raises for any byte
sequence, a decode itself can no longer fail; the only way this helper reports
a skip is a genuine I/O failure reading the file at all (permissions, a race
where the file vanished mid-walk, a broken symlink, ...) — surfaced as a
reason, never a silent ``continue``, so the caller can record the path as
unscanned instead of dropping it invisibly.

This is deliberately NOT for config/model reads (``json.loads(path.read_text())``
on an epic model or ``scope.json``-style document) — a decode failure there is
a real, actionable error and should surface loudly, naming the file. Route
those through a plain ``path.read_text()`` (or ``read_bytes()`` + explicit
decode) so the exception propagates.
"""

from __future__ import annotations

from pathlib import Path

_BOM_UTF8 = b"\xef\xbb\xbf"
_BOM_UTF16_LE = b"\xff\xfe"
_BOM_UTF16_BE = b"\xfe\xff"


def read_text_safe(path: Path) -> tuple[str | None, str | None]:
    """Read ``path`` for a bulk regex/source scan, decoding defensively.

    Returns ``(text, skip_reason)``. On success ``skip_reason`` is ``None``
    and ``text`` is the (possibly lossily-decoded) content — including when a
    BOM was sniffed but the fallback lossy decode had to be used. ``text`` is
    ``None`` only when the file could not be READ at all; ``skip_reason``
    then names the problem so the caller can record the path as unscanned,
    never silently skip it.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"unreadable: {exc}"

    if raw.startswith(_BOM_UTF8):
        try:
            return raw.decode("utf-8-sig"), None
        except UnicodeDecodeError:
            pass
    elif raw.startswith(_BOM_UTF16_LE) or raw.startswith(_BOM_UTF16_BE):
        try:
            return raw.decode("utf-16"), None
        except UnicodeDecodeError:
            pass
    else:
        try:
            return raw.decode("utf-8"), None
        except UnicodeDecodeError:
            pass

    # Final fallback: errors="replace" never raises for any byte sequence, so
    # this line always returns successfully — a replaced character beats
    # dropping the file from the scan outright.
    return raw.decode("utf-8", errors="replace"), None
