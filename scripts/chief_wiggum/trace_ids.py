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

# A token in a DECLARATION position that looks like a stable ID but is NOT
# one — in practice the two-segment `INV-001` shape the /architect skill's
# own worked example used to model (chief-wiggum#281). This is the
# *complement* of the grammar, not a heuristic: it matches only in the same
# declaration positions DEFINE_RE uses (markdown heading, bold, JSON "id"),
# and near_miss_ids() then subtracts everything that IS a valid ID_RE match.
# A foreign prefix glued onto a real kind (e.g. ENT-INV-001) is excluded
# structurally: the declaration prefix must sit immediately before the KIND
# alternation, and "ENT-" occupies that position instead of "**"/heading/
# '"id": "'. Used by check_traceability to tell "artifacts present, nothing
# parseable" (an ERROR) apart from "nothing to measure" (inapplicable).
NEAR_MISS_DEFINE_RE = re.compile(
    rf"(?:^#{{1,6}}\s+|\*\*\s*|[\"']id[\"']\s*:\s*[\"'])"
    rf"((?:{_KINDS})-[A-Za-z0-9][A-Za-z0-9-]*)(?![A-Za-z0-9-])",
    re.MULTILINE,
)

# A valid ID *prefix* — the same body, anchored at the token start but with no
# end-lookahead, so it still matches when a suffix follows.
_ID_PREFIX_RE = re.compile(rf"^{ID_BODY}")


def near_miss_ids(text: str) -> list[str]:
    """Declaration-position tokens that ALMOST match the stable-ID grammar.

    Returns tokens in source order (duplicates kept — callers report
    file:line per occurrence). A token that fullmatches ID_RE is a real
    declaration, not a near miss, and is never returned.

    A token that merely *starts* with a valid ID and continues with a
    hyphenated suffix is a local sub-id, not a malformed stable ID —
    ``CTR-order-confirm-001-pre1`` names the ``pre1`` precondition OF the
    real contract ``CTR-order-confirm-001``. Flagging those was a false
    positive caught by the #281 precision dry-run against
    ``tests/fixtures/code_query_repo``; the whole point of this detector is
    to be a grammar complement, so it must not invent violations the grammar
    does not have.

    The suffix must begin with a hyphen for the exemption to apply, which
    keeps a genuine typo flagged: ``INV-order-0011`` has a valid prefix
    ``INV-order-001`` followed by ``1`` (not ``-``), so it is still a near
    miss.
    """
    out = []
    for m in NEAR_MISS_DEFINE_RE.finditer(text):
        token = m.group(1)
        if ID_RE.fullmatch(token):
            continue
        prefix = _ID_PREFIX_RE.match(token)
        if prefix and token[prefix.end():prefix.end() + 1] == "-":
            continue
        out.append(token)
    return out


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
