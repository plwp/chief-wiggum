"""Regression fixture for the leading relation-tier rank key (CTR-fh-052/053,
INV-fh-007/012): this file carries BOTH a direct @cw-trace annotation AND an
artifact-derived (inferred) match on the SAME 'Confirm Order' operation (its
path words {orders, confirm, direct} cover all of
/api/v1/orders/:id/confirm's literal words {orders, confirm}). `orient` must
rank the direct fact ahead of the inferred one for this file — this is the
same ordering IT-fh-03 case (d) requires of a direct-vs-measured tie, tested
here for the direct-vs-inferred tier boundary that ships in this ticket.

@cw-trace guards INV-checkout-001
"""


def handle_confirm_direct(request):
    ...
