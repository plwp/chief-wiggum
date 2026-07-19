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

Both are namespaced ``@cw-*`` tags read comment-agnostically (the regex
matches wherever the text appears; callers are expected to place it inside a
language comment, but nothing here parses comment syntax). Collecting the
family's regexes in one module means a THIRD tag never has to duplicate the
attribute-parsing helper, and a future audit of "every ``@cw-*`` tag this
repo recognizes" has exactly one place to look.
"""

from __future__ import annotations

import re

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
# not reuse chief_wiggum.trace_ids.ID_BODY. Multiple names may be listed on one
# tag (space- or comma-separated), mirroring @cw-trace's multi-id capture, for
# a single emit site that fires more than one binding (e.g. a span that also
# bumps a counter).
_BINDING_TOKEN = r"[A-Za-z0-9_][A-Za-z0-9_./:-]*"
EMITS_TAG_RE = re.compile(
    rf"@cw-emits\s+(?P<names>{_BINDING_TOKEN}(?:[\s,]+{_BINDING_TOKEN})*)",
    re.IGNORECASE,
)


def split_binding_names(raw: str) -> list[str]:
    """Split an ``EMITS_TAG_RE`` ``names`` capture into individual binding names."""
    return [n for n in re.split(r"[\s,]+", raw.strip()) if n]
