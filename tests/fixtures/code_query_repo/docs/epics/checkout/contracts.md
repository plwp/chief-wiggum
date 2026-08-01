# Checkout — Contracts

### BR-order-001

Orders must be confirmed atomically — a confirmed order must never revert to
pending, and no code path may confirm an order twice.

### CTR-order-confirm-001

`POST /api/v1/orders/:id/confirm`

- REQUIRES: order status is `pending`
- ENSURES: order status becomes `confirmed`
- Error: 409 if the order is already confirmed

<!-- @cw-trace realizes BR-order-001 -->
