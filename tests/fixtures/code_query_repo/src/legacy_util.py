"""A plain helper with no @cw-trace annotation and no artifact binding.

Fixture-only (#187 IT-fh-03 case (a)): this file carries NO direct/inferred
governance of its own — its only fact should be the `measured` hotspot fact
from `docs/quality/hotspots.json` exact membership.
"""


def normalize(value: str) -> str:
    return value.strip().lower()
