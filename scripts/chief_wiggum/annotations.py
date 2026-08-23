"""Shared home for the ``@cw-<verb> <payload>`` code-annotation tag family.

Distinct from ``chief_wiggum.trace_ids`` (the ``@cw-trace <verb> <ID>``
grammar for STABLE IDS — ``BR-``/``CTR-``/``INV-``/etc). The tags in this
module mark a **code site** with a **free-form binding name**, not a stable
ID:

- ``@cw-writes <INV-ID> controls_field=... sanctioned_writers=...`` (#93,
  ``scripts/check_single_writer.py``) — marks an invariant's metadata, parsed
  out of prose ``invariants.md``.
- ``@cw-emits <binding-name>`` (#170, ``scripts/check_instrumentation.py``) —
  marks the code site that emits a declared telemetry span/event/metric.
- ``@cw-smoke <system-name> [case=<substring>]`` (#353,
  ``scripts/check_external_smoke.py``) — marks the test that performs ONE real
  round-trip against a declared external system.

Both are namespaced ``@cw-*`` tags read comment-agnostically (the regex
matches wherever the text appears; callers are expected to place it inside a
language comment, but nothing here parses comment syntax). Collecting the
family's regexes in one module means a THIRD tag never has to duplicate the
attribute-parsing helper, and a future audit of "every ``@cw-*`` tag this
repo recognizes" has exactly one place to look.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# --- @cw-writes (#93) --------------------------------------------------------
#
# `@cw-writes <INV-ID> controls_field=a,b sanctioned_writers=x,y [sink=db]`
# (order-free key=value attrs). See docs/single-writer.md.
WRITES_TAG_RE = re.compile(
    r"@cw-writes\s+(?P<id>INV-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{3})(?P<attrs>(?:\s+\w+=[^\s]+)+)",
    re.IGNORECASE,
)
ATTR_RE = re.compile(r"(\w+)=([^\s]+)")

# --- @cw-emits (#170) ---------------------------------------------------------
#
# `@cw-emits <binding-name>` where binding-name is an OTel span/event name or
# a k6/metrics-exporter metric name — e.g. `endpointing_latency_ms`,
# `llm.ttft`, `tts/ttfb`. Not a stable ID (no KIND-slug-NNN shape), so it does
# not reuse chief_wiggum.trace_ids.ID_BODY.
#
# Multiple names on one tag (a single emit site that fires more than one
# binding, e.g. a span that also bumps a counter) must be COMMA-separated:
#
#     # @cw-emits asr_latency, endpointing_latency_ms
#
# A bare space-separated token list is deliberately NOT accepted: the first
# token is the binding, and any space-separated prose after it is ignored
# ("# @cw-emits asr_latency records ASR latency" emits exactly one binding).
# Otherwise trailing prose words would become phantom bindings that could
# accidentally satisfy check_instrumentation's missing-binding check.
_BINDING_TOKEN = r"[A-Za-z0-9_][A-Za-z0-9_./:-]*"
EMITS_TAG_RE = re.compile(
    rf"@cw-emits\s+(?P<names>{_BINDING_TOKEN}(?:\s*,\s*{_BINDING_TOKEN})*)",
    re.IGNORECASE,
)

# --- @cw-smoke (#353) ---------------------------------------------------------
#
# `@cw-smoke <system-name> [case=<substring>]` marks the test that performs ONE
# real round-trip against an external system declared `"external": true` with an
# `external_system` name in contracts.json (#350).
#
#     // @cw-smoke SCP case=TestSCPLiveVenueInfo
#     func TestSCPLiveVenueInfo(t *testing.T) { ... }
#
# `case=` pins which junit result case proves the smoke ran, so the checker can
# tell "ran and passed" from "was skipped because credentials were absent" —
# the distinction the whole gate turns on. Without it the checker falls back to
# matching the annotated FILE against the result case's file/classname, which
# works within one language and is reported as the weaker match it is.
#
# ONE system per tag, deliberately unlike @cw-emits' comma list: a single test
# that round-trips two external systems is not one smoke, it is two smokes
# sharing a function, and each deserves its own annotation and its own verdict.
_SYSTEM_TOKEN = r"[A-Za-z0-9_][A-Za-z0-9_.\-]*"
SMOKE_TAG_RE = re.compile(
    rf"@cw-smoke\s+(?P<system>{_SYSTEM_TOKEN})"
    rf"(?:\s+case=(?P<case>[^\s]+))?",
    re.IGNORECASE,
)


def split_binding_names(raw: str) -> list[str]:
    """Split an ``EMITS_TAG_RE`` ``names`` capture into individual binding names.

    Commas are the ONLY multi-binding separator (see grammar note above);
    whitespace around each comma is tolerated. A space-separated list is one
    binding plus ignored prose — the regex never captures it as multiple names.
    """
    return [n for n in (part.strip() for part in raw.split(",")) if n]


# --- emit_site emission (#326) ------------------------------------------------
#
# check_instrumentation.py used to walk the source tree itself (its own raw
# rglob + EMITS_TAG_RE loop), bypassing the scripts/emitters/ registry
# check_traceability.py ("trace_annotation") and check_single_writer.py
# ("write_site") already share via chief_wiggum.manifest.walk_source_files —
# so, unlike its siblings, it never pruned submodules/nested git checkouts (a
# correctness gap, not just a cost one). EmitSite/emit_emit_sites give
# check_instrumentation.py a THIRD fact kind, "emit_site", so its scan can go
# through the exact same per-language emitter dispatch as its siblings.


@dataclass
class EmitSite:
    """A source-code site carrying an ``@cw-emits <name>`` annotation (#170).
    ``payload`` for the ``"emit_site"`` fact kind."""

    name: str
    file: str
    line: int
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


def emit_emit_sites(path: str, text: str) -> list[EmitSite]:
    """Per-file EMISSION: every ``@cw-emits`` annotation in one file's
    ``text``. Comment-agnostic like ``EMITS_TAG_RE`` itself — no suffix
    parameter needed (unlike ``emit_source_annotations``/``emit_write_sites``,
    which strip per-language line comments before matching). This is the
    function every ``emit_site`` emitter (language-specific or generic) under
    ``scripts/emitters/`` delegates to."""
    sites: list[EmitSite] = []
    for i, line in enumerate(text.splitlines()):
        for m in EMITS_TAG_RE.finditer(line):
            for name in split_binding_names(m.group("names")):
                sites.append(EmitSite(name=name, file=path, line=i + 1, text=line.strip()[:200]))
    return sites
