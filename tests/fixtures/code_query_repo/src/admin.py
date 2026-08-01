"""Admin override tooling — deliberately NOT a sanctioned writer of order.status,
for the golden-parity / orient violation fixtures."""

from src.order import Order


def admin_override_status(order: Order, new_status: str) -> Order:
    order.status = new_status
    return order
