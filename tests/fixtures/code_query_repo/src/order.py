"""Order confirmation handler for the checkout epic."""


class Order:
    def __init__(self, status, customer_id):
        self.status = status
        self.customer_id = customer_id


def confirm_order(order: Order) -> Order:
    """Confirm a pending order.

    @cw-trace guards CTR-order-confirm-001 INV-checkout-001
    """
    if order.status != "pending":
        raise ValueError("order is already confirmed")
    order.status = "confirmed"
    return order


def ship_order(order: Order) -> Order:
    """Mark a confirmed order as shipped — not modeled in state-machines.json
    (deliberately undocumented, for transition-map drift coverage)."""
    order.status = "shipped"
    return order
