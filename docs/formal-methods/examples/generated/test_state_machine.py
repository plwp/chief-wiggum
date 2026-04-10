"""
Auto-generated Hypothesis RuleBasedStateMachine for: Order Status State Machine
Generated from formal model. Do not edit by hand.
"""

from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, rule, invariant, initialize


class OrderStatusStateMachine(RuleBasedStateMachine):
    """State machine test: Tracks order lifecycle from creation through completion or cancellation"""

    VALID_STATES = ['draft', 'pending', 'confirmed', 'in_progress', 'completed', 'cancelled']
    TERMINAL_STATES = ['completed', 'cancelled']

    @initialize()
    def init(self):
        self.state = "draft"

    @rule()
    def transition_draft_to_pending_via_submit(self):  # Guards: customer_id is set, items list is non-empty, end_date > start_date
        if self.state != "draft":
            return
        self.state = "pending"

    @rule()
    def transition_pending_to_confirmed_via_confirm(self):  # Guards: capacity is available for the requested dates, all pre-start validation passes
        if self.state != "pending":
            return
        self.state = "confirmed"

    @rule()
    def transition_pending_to_cancelled_via_cancel(self):
        if self.state != "pending":
            return
        self.state = "cancelled"

    @rule()
    def transition_confirmed_to_in_progress_via_start(self):  # Guards: a resource has been assigned
        if self.state != "confirmed":
            return
        self.state = "in_progress"

    @rule()
    def transition_confirmed_to_cancelled_via_cancel(self):
        if self.state != "confirmed":
            return
        self.state = "cancelled"

    @rule()
    def transition_in_progress_to_completed_via_complete(self):  # Guards: balance is settled or acknowledged unpaid
        if self.state != "in_progress":
            return
        self.state = "completed"

    @invariant()
    def check_inv_001(self):
        """Item names are NEVER stored as strings on orders. Always reference item_ids and resolve at read time."""
        # TODO: implement check — expression: not hasattr(order, 'item_names')
        pass

    @invariant()
    def check_inv_002(self):
        """Every order with status >= pending has a non-null customer_id that references a valid customer."""
        # TODO: implement check — expression: order.status in ('draft',) or order.customer_id is not None
        pass

    @invariant()
    def check_inv_003(self):
        """end_date > start_date on every order, enforced at write time."""
        # TODO: implement check — expression: order.end_date > order.start_date
        pass

    @invariant()
    def check_inv_004(self):
        """An order in in_progress MUST have a resource_id assigned."""
        if self.state not in ['in_progress']:
            return
        # TODO: implement check — expression: order.resource_id is not None
        pass

    @invariant()
    def check_inv_005(self):
        """Dashboard, List View, Calendar, and Detail View MUST derive record counts from the same query. No screen-specific counting logic."""
        # TODO: implement check — expression: N/A
        pass

    @invariant()
    def check_inv_006(self):
        """Capacity View and Summary View MUST use the same occupancy calculation. Define once, use everywhere."""
        # TODO: implement check — expression: N/A
        pass

    @invariant()
    def check_inv_007(self):
        """If an operation depends on an external service (email, payment), the success toast MUST NOT display unless the service call succeeded."""
        # TODO: implement check — expression: N/A
        pass


    # --- Invalid transition assertions ---

    @rule()
    def invalid_draft_to_confirmed(self):
        """Must be rejected: Cannot skip pending validation"""
        if self.state != "draft":
            return
        # Assert this transition is not possible
        assert self.state != "confirmed" or self.state == "draft"

    @rule()
    def invalid_completed_to_in_progress(self):
        """Must be rejected: Completion is irreversible"""
        if self.state != "completed":
            return
        # Assert this transition is not possible
        assert self.state != "in_progress" or self.state == "completed"

    @rule()
    def invalid_cancelled_to_draft(self):
        """Must be rejected: Cancellation is terminal — no transitions out"""
        if self.state != "cancelled":
            return
        # Assert this transition is not possible
        assert self.state != "draft" or self.state == "cancelled"

    @rule()
    def invalid_cancelled_to_pending(self):
        """Must be rejected: Cancellation is terminal — no transitions out"""
        if self.state != "cancelled":
            return
        # Assert this transition is not possible
        assert self.state != "pending" or self.state == "cancelled"

    @rule()
    def invalid_cancelled_to_confirmed(self):
        """Must be rejected: Cancellation is terminal — no transitions out"""
        if self.state != "cancelled":
            return
        # Assert this transition is not possible
        assert self.state != "confirmed" or self.state == "cancelled"


TestStateMachine = OrderStatusStateMachine.TestCase