"""Grandfather-waiver reading for the blocking gates + rendering overlay (#215).

``adopt.py grandfather`` writes ``<meta root>/adoption/grandfathered.json``
(schema ``grandfather/1``): one entry per pre-adoption baseline finding, in the
JUSTIFIED-waiver shape (``reason``/``owner``/``expiry``). This module is the
single reader the blocking gates share (F5 on chief-wiggum#215): a finding
whose key matches a NON-EXPIRED entry is reported under a ``grandfathered``
section and does NOT count toward the blocking exit; an EXPIRED entry does NOT
waive — the finding blocks again, labeled "EXPIRED grandfather" (expiry is
visible pressure, not amnesty).

Key formats (exactly what ``adopt.py`` writes — documented here so the writers
and readers cannot drift):

- ``DEBT-<10 hex>`` — debt-inventory items (read by ``debt_inventory.py``).
- ``check_traceability:uncovered:<STABLE-ID>`` /
  ``check_traceability:untested:<STABLE-ID>`` — traceability coverage gaps,
  canonical stable ID (``CTR-``/``INV-``).
- ``check_single_writer:<INV-id>:<field>:<file>`` — single-writer violations
  (canonical invariant id, controlled-field path, repo-relative file).

Expiry mirrors ``chief_wiggum.trace_links.Justification.is_expired``: an
unparseable or missing expiry counts as EXPIRED, never a silent pass.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

# Relative to the resolver meta root (`artifacts.Resolver.meta_root`).
GRANDFATHER_RELPATH = Path("adoption") / "grandfathered.json"


def load_entries(path: str | Path) -> tuple[dict[str, dict], str | None]:
    """(finding-key -> entry, warning) from a grandfathered.json.

    A missing file is graceful absence -> ``({}, None)``. An unparsable or
    mis-shaped file grants NOTHING and returns a warning — an unreadable
    amnesty file must never silently waive findings.
    """
    p = Path(path)
    if not p.is_file():
        return {}, None
    try:
        doc = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"grandfather file {p} unparsable ({exc}) — no findings waived"
    if not isinstance(doc, dict):
        return {}, f"grandfather file {p} is not a JSON object — no findings waived"
    out: dict[str, dict] = {}
    for entry in doc.get("entries") or []:
        if isinstance(entry, dict) and entry.get("id"):
            out[entry["id"]] = entry
    return out, None


def is_expired(entry: dict, today: date | None = None) -> bool:
    """JUSTIFIED-waiver expiry posture: unparseable/missing expiry = expired."""
    today = today or date.today()
    expiry = entry.get("expiry")
    if not isinstance(expiry, str):
        return True
    try:
        return date.fromisoformat(expiry) < today
    except ValueError:
        return True


def expired_live(item: dict, today: date | None = None) -> bool:
    """LIVE expired-ness for RENDERERS (F8 on chief-wiggum#215).

    ``debt_inventory`` stores ``grandfather_expired`` as a build-time snapshot;
    a surface rendering the stored inventory later (slop-gate debt block,
    quality-report debt section, ``code_query orient`` facts) must overlay the
    stored flag with a live compare of ``grandfather_expiry`` vs today — an
    inventory built before the expiry date must still render EXPIRED after it
    passes. The stored True is trusted (never un-expires an item).
    """
    if not item.get("grandfathered"):
        return False
    if item.get("grandfather_expired"):
        return True
    return is_expired({"expiry": item.get("grandfather_expiry")}, today)
