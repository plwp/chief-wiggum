## Entity: Order

Core business entity — tracks service orders from creation through completion

### Canonical Fields
| Field | Type | Required | Source of Truth | Notes |
|-------|------|----------|-----------------|-------|
| id | ObjectID | always | MongoDB _id | immutable |
| customer_id | ObjectID | after status >= pending | customers collection | MUST be set during the submit transition |
| status | enum | always | — | Valid values: draft, pending, confirmed, in_progress, completed, cancelled |
| item_ids | []ObjectID | after status >= pending | items collection | NEVER store item names as strings — always reference by ID |
| resource_id | ObjectID | after status == in_progress | — | — |
| start_date | string | after status >= pending | — | ISO 8601 date format |
| end_date | string | after status >= pending | — | ISO 8601 date format. Must be after start_date. |

### POST /api/v1/orders
Create a new order in draft status

- **REQUIRES**: Authenticated staff OR valid public submission token; At least one item_id provided; Valid date range (end_date > start_date)
- **ENSURES**: Order created with status 'draft'; Order ID returned in response; Order visible on admin list within 1 second
- **ERROR CASES**: 400 if dates invalid (end_date <= start_date); 401 if no authentication; 409 if capacity exceeded for requested dates

### POST /api/v1/orders/:id/confirm
Confirm a pending order

- **REQUIRES**: Order exists; Order status is 'pending'; customer_id is set; At least one item_id exists
- **ENSURES**: Status transitions to 'confirmed'; Confirmation notification sent (or error surfaced if service unavailable)
- **ERROR CASES**: 404 if order does not exist; 409 if order is not in 'pending' status; 422 if customer_id or item_ids missing
- **STATE TRANSITION**: pending → confirmed

- **INVARIANT**: After confirmation, order.customer_id is NEVER null
