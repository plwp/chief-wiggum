"""
Auto-generated guard clauses from formal contracts.
Generated from formal model. Do not edit by hand.
"""

def create_order(request):
    """Create a new order in draft status"""
    # REQUIRES: Authenticated staff OR valid public submission token
    if not (request.user.is_staff or request.token.is_valid_public):
        raise HTTPError(400, "Authenticated staff OR valid public submission token")

    # REQUIRES: At least one item_id provided
    if not (len(request.body.item_ids) > 0):
        raise HTTPError(400, "At least one item_id provided")

    # REQUIRES: Valid date range (end_date > start_date)
    if not (request.body.end_date > request.body.start_date):
        raise HTTPError(400, "Valid date range (end_date > start_date)")

    # --- implementation ---

    # ENSURES:
    # Order created with status 'draft'
    # Order ID returned in response
    # Order visible on admin list within 1 second


def confirm_order(request):
    """Confirm a pending order"""
    # REQUIRES: Order exists
    if not (order is not None):
        raise HTTPError(404, "Order exists")

    # REQUIRES: Order status is 'pending'
    if not (order.status == 'pending'):
        raise HTTPError(404, "Order status is 'pending'")

    # REQUIRES: customer_id is set
    if not (order.customer_id is not None):
        raise HTTPError(404, "customer_id is set")

    # REQUIRES: At least one item_id exists
    if not (len(order.item_ids) > 0):
        raise HTTPError(409, "At least one item_id exists")

    # --- implementation ---

    # ENSURES:
    # Status transitions to 'confirmed'
    # Confirmation notification sent (or error surfaced if service unavailable)

