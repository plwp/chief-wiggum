# Checkout — Invariants

**INV-checkout-001**: single write path for order status — only `ConfirmOrder`
(or a call inside `src/order.py`) may set `order.status`.
