"""Tests for the checkout epic's order-confirmation contract.

@cw-trace verifies CTR-order-confirm-001 INV-checkout-001
"""

# NOTE: fixture content for scripts/code_query.py + check_traceability.py's
# regex-based scan — these are never imported/executed (the outer test suite
# would fail to resolve `src.order` as a top-level package), only read as text.
# Mirrors tests/fixtures/traceability_golden/src/test_order.py's convention.


def test_confirm_order_transitions_pending_to_confirmed():
    ...


def test_confirm_order_rejects_already_confirmed():
    ...
