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

# Bytes inspected when sniffing a BOM-less encoding.
_SNIFF_BYTES = 4096
# NUL density above which a BOM-less file is treated as UTF-16 rather than
# UTF-8. ASCII-in-UTF-16 is ~50% NUL; genuine UTF-8 source is ~0%.
_MIN_NUL_RATIO = 0.25
# Above this share of U+FFFD the file was not meaningfully decoded, so it is
# reported unscanned instead of scanned-as-garbage. Set well above the "a few
# corrupt bytes inside real source" case (~10%), which MUST still be scanned
# lossily — a replaced character beats a skipped file and cannot fabricate a
# write site. A genuine binary blob sits far higher (~70%+).
_MAX_REPLACEMENT_RATIO = 0.30


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
        # BOM-less UTF-16 must be caught BEFORE the UTF-8 attempt. ASCII text
        # in UTF-16-LE is `s\x00t\x00r\x00...`, and NUL is perfectly valid
        # UTF-8 — so `raw.decode("utf-8")` SUCCEEDS, yields zero replacement
        # characters, and hands back text no source regex can ever match. The
        # scan then reports the file clean while never having read a word of
        # it: an unsanctioned second writer in such a file passes the gate
        # silently. That is the exact "not measured renders as clean" shape
        # this ticket's sibling umbrella (#289) exists to kill, so a lossy
        # decode is NOT acceptable here and a replacement-ratio heuristic
        # cannot see it (the ratio is 0%).
        decoded = _decode_nul_interleaved(raw)
        if decoded is not None:
            return decoded, None
        try:
            return raw.decode("utf-8"), None
        except UnicodeDecodeError:
            pass

    # Fallback: errors="replace" never raises, so a mildly-dirty file is still
    # scanned — a replaced character beats dropping the file outright, and
    # cannot manufacture a false-positive write site.
    text = raw.decode("utf-8", errors="replace")
    # ...but a file that is mostly replacement characters was not meaningfully
    # read. Returning it as if scanned would be the same silent-clean lie as
    # above, so report it unscanned WITH its path and let the caller surface it.
    if text and (text.count("�") / len(text)) > _MAX_REPLACEMENT_RATIO:
        return None, "undecodable: not valid UTF-8 or UTF-16 (binary?)"
    return text, None


def _decode_nul_interleaved(raw: bytes) -> str | None:
    """Decode BOM-less UTF-16 by detecting NUL interleaving, else ``None``.

    Real UTF-8 source files essentially never contain NUL bytes; UTF-16 text
    of ASCII-ish source is ~50% NUL. Which half the NULs occupy gives the
    endianness: LE puts them at odd offsets (``s\\x00``), BE at even
    (``\\x00s``).
    """
    sample = raw[:_SNIFF_BYTES]
    if not sample or sample.count(0) / len(sample) < _MIN_NUL_RATIO:
        return None
    odd_nuls = sample[1::2].count(0)
    even_nuls = sample[0::2].count(0)
    for encoding in ("utf-16-le", "utf-16-be") if odd_nuls >= even_nuls else ("utf-16-be", "utf-16-le"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None
