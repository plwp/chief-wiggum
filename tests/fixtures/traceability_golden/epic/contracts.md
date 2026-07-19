### CTR-order-001 — valid date range
<!-- @cw-trace realizes BR-order-001 -->

REQUIRES: start_date <= end_date
ENSURES: order.total > 0

### CTR-order-002 — idempotent creation

No tests written yet for this one — should surface as untested (and uncovered).
