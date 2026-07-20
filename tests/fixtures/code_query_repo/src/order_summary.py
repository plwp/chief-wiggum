"""A plain file that lexically resembles a hotspot path (shares the word
"order" with src/order.py) but is NOT itself listed in
docs/quality/hotspots.json.

Fixture-only (#187 IT-fh-03 case (c), the negative): orient must NOT surface a
hotspot fact here — measured facts come ONLY from exact path membership,
never from lexical resemblance (INV-fh-007/012).
"""


def summarize(order) -> str:
    return f"order {getattr(order, 'customer_id', '?')}: {getattr(order, 'status', '?')}"
