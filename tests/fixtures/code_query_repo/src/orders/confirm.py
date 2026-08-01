"""Un-annotated confirm-endpoint handler.

Deliberately carries NO @cw-trace annotation and is NOT listed in
transition-map.json — `orient` must bind it to the `POST
/api/v1/orders/:id/confirm` operation purely via artifact-derived (inferred)
path matching: its path words {orders, confirm} cover ALL the operation
path's literal words.
"""


def handle_confirm(request):
    ...
