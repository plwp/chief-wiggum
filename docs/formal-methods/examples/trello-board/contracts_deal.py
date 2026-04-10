"""
Auto-generated Design-by-Contract decorators from formal contracts.
Generated from formal model. Do not edit by hand.
"""

import deal

# === Board ===

# POST /1/boards
@deal.pre(lambda: # TODO: user is authenticated, message="user is authenticated")
@deal.pre(lambda: # TODO: name is non-empty string, message="name is non-empty string")
@deal.pre(lambda: request.body.name is not None, message="name is provided")
@deal.post(lambda result: # TODO: board created with status active, message="board created with status active")
@deal.post(lambda result: # TODO: board.id returned, message="board.id returned")
@deal.post(lambda result: # TODO: user added as admin member, message="user added as admin member")
@deal.post(lambda result: # TODO: default lists created if defaultLists=true, message="default lists created if defaultLists=true")
def create_board(request):
    """Create Board"""
    raise NotImplementedError


# GET /1/boards/{id}
@deal.pre(lambda: # TODO: user is authenticated, message="user is authenticated")
@deal.pre(lambda: # TODO: user has access to board, message="user has access to board")
@deal.post(lambda result: # TODO: board object returned with requested fields, message="board object returned with requested fields")
def read_board(request):
    """Read Board"""
    raise NotImplementedError


# PUT /1/boards/{id}
@deal.pre(lambda: # TODO: user is authenticated, message="user is authenticated")
@deal.pre(lambda: # TODO: user has write access to board, message="user has write access to board")
@deal.pre(lambda: # TODO: board is not deleted, message="board is not deleted")
@deal.post(lambda result: # TODO: board updated with new values, message="board updated with new values")
@deal.post(lambda result: # TODO: dateLastActivity updated, message="dateLastActivity updated")
def update_board(request):
    """Update Board"""
    raise NotImplementedError


# DELETE /1/boards/{id}
@deal.pre(lambda: # TODO: user is authenticated, message="user is authenticated")
@deal.pre(lambda: # TODO: user is board admin, message="user is board admin")
@deal.post(lambda result: # TODO: board marked as deleted, message="board marked as deleted")
@deal.post(lambda result: # TODO: board no longer appears in member board lists, message="board no longer appears in member board lists")
@deal.post(lambda result: # TODO: all lists and cards become inaccessible, message="all lists and cards become inaccessible")
def delete_board(request):
    """Delete Board"""
    raise NotImplementedError


# PUT /1/boards/{id}
@deal.pre(lambda: # TODO: user is authenticated, message="user is authenticated")
@deal.pre(lambda: # TODO: user has write access, message="user has write access")
@deal.pre(lambda: # TODO: board is active, message="board is active")
@deal.post(lambda result: # TODO: board.closed set to true, message="board.closed set to true")
@deal.post(lambda result: # TODO: board moves to closed boards view, message="board moves to closed boards view")
def close_board(request):
    """Close Board"""
    raise NotImplementedError


# PUT /1/boards/{id}
@deal.pre(lambda: # TODO: user is authenticated, message="user is authenticated")
@deal.pre(lambda: # TODO: user has write access, message="user has write access")
@deal.pre(lambda: # TODO: board is closed, message="board is closed")
@deal.post(lambda result: # TODO: board.closed set to false, message="board.closed set to false")
@deal.post(lambda result: # TODO: board reappears in active boards view, message="board reappears in active boards view")
def reopen_board(request):
    """Reopen Board"""
    raise NotImplementedError


# GET /1/members/me/boards
@deal.pre(lambda: # TODO: user is authenticated, message="user is authenticated")
@deal.post(lambda result: # TODO: array of board objects returned, message="array of board objects returned")
@deal.post(lambda result: # TODO: only boards user has access to, message="only boards user has access to")
def list_board(request):
    """List Board"""
    raise NotImplementedError


# PUT /1/boards/{id}
@deal.pre(lambda: # TODO: user is authenticated, message="user is authenticated")
@deal.pre(lambda: # TODO: user is board member, message="user is board member")
@deal.post(lambda result: # TODO: board.starred set to true for this user, message="board.starred set to true for this user")
@deal.post(lambda result: # TODO: board appears in starred section, message="board appears in starred section")
def star_board(request):
    """Star Board"""
    raise NotImplementedError

