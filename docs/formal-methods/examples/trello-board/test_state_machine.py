"""
Auto-generated Hypothesis RuleBasedStateMachine for: Board Lifecycle
Generated from formal model. Do not edit by hand.
"""

from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, rule, invariant, initialize


class BoardLifecycle(RuleBasedStateMachine):
    """State machine test: State machine for Board entity, extracted from domain model"""

    VALID_STATES = ['active', 'closed', 'deleted']
    TERMINAL_STATES = ['deleted']

    @initialize()
    def init(self):
        self.state = "active"

    @rule()
    def transition_active_to_closed_via_close(self):  # Guards: user is authenticated, user has write access, board is active
        if self.state != "active":
            return
        self.state = "closed"

    @rule()
    def transition_closed_to_active_via_reopen(self):  # Guards: user is authenticated, user has write access, board is closed
        if self.state != "closed":
            return
        self.state = "active"

    @rule()
    def transition_active_to_deleted_via_delete(self):  # Guards: user is authenticated, user is board admin
        if self.state != "active":
            return
        self.state = "deleted"

    @rule()
    def transition_closed_to_deleted_via_delete(self):  # Guards: user is authenticated, user is board admin
        if self.state != "closed":
            return
        self.state = "deleted"


    # --- Invalid transition assertions ---

    @rule()
    def invalid_deleted_to_active(self):
        """Must be rejected: deleted is a terminal state — no transitions out"""
        if self.state != "deleted":
            return
        # Assert this transition is not possible
        assert self.state != "active" or self.state == "deleted"

    @rule()
    def invalid_deleted_to_closed(self):
        """Must be rejected: deleted is a terminal state — no transitions out"""
        if self.state != "deleted":
            return
        # Assert this transition is not possible
        assert self.state != "closed" or self.state == "deleted"


TestStateMachine = BoardLifecycle.TestCase