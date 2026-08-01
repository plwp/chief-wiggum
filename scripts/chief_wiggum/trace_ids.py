"""Single source of truth for the stable-ID grammar and trace verbs (#166).

The TIM schema (``templates/formal-models/tim-schema.json``), the traceability
scanner (``scripts/check_traceability.py``), and the ratchet's definition
hashing (``scripts/ratchet.py``) must agree on what a stable ID looks like — a
kind added in one place but not the others is *silently dropped* by the
scanners, which is exactly the failure this module removes. All three now
build from these constants; ``tests/test_trace_ids.py`` cross-checks that no
copy can drift.
"""

from __future__ import annotations

import re

# Stable-ID kinds. BR/CTR/INV are the epic layer. The rest are the system
# layer (#166), reserved now so scanners never silently drop them:
# ARC (component/deployable), EDG (edge contract), SLO (objective),
# BUD (budget tree), ASM (external/vendor assumption), PRC (process),
# MIG (migration).
ID_KINDS = ("BR", "CTR", "INV", "ARC", "EDG", "SLO", "BUD", "ASM", "PRC", "MIG")

# Trace verbs: the original four plus the two SysML-derived structural verbs —
# allocate (component -> repo/deployable) and derive (child budget/requirement
# -> parent). allocate/derive are validated as links but do not feed
# orphan/coverage math.
VERBS = ("realizes", "guards", "ensures", "verifies", "allocate", "derive")

_KINDS = "|".join(ID_KINDS)

# An ID ends at the 3-digit suffix and must not run into more id chars
# (so CTR-order-001oops is NOT a valid CTR-order-001). The slug segment is
# case-insensitive; consumers canonicalise at ingestion.
ID_BODY = rf"(?:{_KINDS})-[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{{3}}"
ID_RE = re.compile(rf"\b{ID_BODY}(?![A-Za-z0-9-])")

# Where a defined ID is *declared*: a markdown heading `### CTR-...`, a bold
# label `**CTR-...**`, or a JSON `"id": "CTR-..."` field.
DEFINE_RE = re.compile(
    rf"(?:^#{{1,6}}\s+|\*\*\s*|[\"']id[\"']\s*:\s*[\"'])({ID_BODY})(?![A-Za-z0-9-])",
    re.MULTILINE,
)

# Ratchet's declaration grammar is markdown-only: JSON "id" nodes are hashed
# structurally by ratchet._walk_json_ids, so its DEFINE_RE must not match the
# JSON field form.
MD_DEFINE_RE = re.compile(
    rf"(?:^#{{1,6}}\s+|\*\*\s*)({ID_BODY})(?![A-Za-z0-9-])"
)

# The @cw-trace annotation grammar (LOBSTER-style namespaced tag).
TRACE_RE = re.compile(
    rf"@cw-trace\s+(?P<verb>{'|'.join(VERBS)})\s+"
    rf"(?P<ids>(?:{ID_BODY}(?![A-Za-z0-9-])[\s,]*)+)",
    re.IGNORECASE,
)


def canonical_id(node_id: str) -> str:
    """Canonical form: uppercase kind prefix, lowercase remainder.

    IDs are matched case-insensitively (CTR-order-001 == CTR-ORDER-001); this
    keeps the familiar display shape while making links immune to case drift
    between epic docs and code annotations. EVERY consumer that keys a map by
    a stable ID (the traceability scanner's annotations, the definition-hash
    maps in ``chief_wiggum.hashing``, the ratchet's contract-hash
    comparisons) must key by THIS form — a raw-cased key on one side of a
    join silently drops the match (PR #181 review: an uppercase-slug ID like
    ``CTR-BIL-001`` recorded no sidecar link and could never go suspect).
    """
    kind, _, rest = node_id.partition("-")
    return f"{kind.upper()}-{rest.lower()}"
