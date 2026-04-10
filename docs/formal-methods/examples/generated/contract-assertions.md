# Contract Assertion Templates

Generated from formal contracts. Each operation has precondition and postcondition checks.

## Create Order (POST /api/v1/orders)

### Precondition Tests
- [ ] **PRE-001**: Verify Authenticated staff OR valid public submission token
  - Call WITHOUT this condition → expect error
- [ ] **PRE-002**: Verify At least one item_id provided
  - Call WITHOUT this condition → expect error
- [ ] **PRE-003**: Verify Valid date range (end_date > start_date)
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **POST-001**: Verify Order created with status 'draft'
  - Call correctly → assert postcondition holds
- [ ] **POST-002**: Verify Order ID returned in response
  - Call correctly → assert postcondition holds
- [ ] **POST-003**: Verify Order visible on admin list within 1 second
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 400: dates invalid (end_date <= start_date)
- [ ] Status 401: no authentication
- [ ] Status 409: capacity exceeded for requested dates

## Confirm Order (POST /api/v1/orders/:id/confirm)

### Precondition Tests
- [ ] **PRE-010**: Verify Order exists
  - Call WITHOUT this condition → expect error
- [ ] **PRE-011**: Verify Order status is 'pending'
  - Call WITHOUT this condition → expect error
- [ ] **PRE-012**: Verify customer_id is set
  - Call WITHOUT this condition → expect error
- [ ] **PRE-013**: Verify At least one item_id exists
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **POST-010**: Verify Status transitions to 'confirmed'
  - Call correctly → assert postcondition holds
- [ ] **POST-011**: Verify Confirmation notification sent (or error surfaced if service unavailable)
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 404: order does not exist
- [ ] Status 409: order is not in 'pending' status
- [ ] Status 422: customer_id or item_ids missing
