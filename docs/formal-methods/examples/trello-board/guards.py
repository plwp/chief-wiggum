"""
Auto-generated guard clauses from formal contracts.
Generated from formal model. Do not edit by hand.
"""

def create_board(request):
    """Create Board"""
    # REQUIRES: user is authenticated
    if not (# TODO: user is authenticated):
        raise HTTPError(401, "user is authenticated")

    # REQUIRES: name is non-empty string
    if not (# TODO: name is non-empty string):
        raise HTTPError(400, "name is non-empty string")

    # REQUIRES: name is provided
    if not (request.body.name is not None):
        raise HTTPError(400, "name is provided")

    # --- implementation ---

    # ENSURES:
    # board created with status active
    # board.id returned
    # user added as admin member
    # default lists created if defaultLists=true


def read_board(request):
    """Read Board"""
    # REQUIRES: user is authenticated
    if not (# TODO: user is authenticated):
        raise HTTPError(401, "user is authenticated")

    # REQUIRES: user has access to board
    if not (# TODO: user has access to board):
        raise HTTPError(404, "user has access to board")

    # --- implementation ---

    # ENSURES:
    # board object returned with requested fields


def update_board(request):
    """Update Board"""
    # REQUIRES: user is authenticated
    if not (# TODO: user is authenticated):
        raise HTTPError(401, "user is authenticated")

    # REQUIRES: user has write access to board
    if not (# TODO: user has write access to board):
        raise HTTPError(401, "user has write access to board")

    # REQUIRES: board is not deleted
    if not (# TODO: board is not deleted):
        raise HTTPError(401, "board is not deleted")

    # --- implementation ---

    # ENSURES:
    # board updated with new values
    # dateLastActivity updated


def delete_board(request):
    """Delete Board"""
    # REQUIRES: user is authenticated
    if not (# TODO: user is authenticated):
        raise HTTPError(401, "user is authenticated")

    # REQUIRES: user is board admin
    if not (# TODO: user is board admin):
        raise HTTPError(401, "user is board admin")

    # --- implementation ---

    # ENSURES:
    # board marked as deleted
    # board no longer appears in member board lists
    # all lists and cards become inaccessible


def close_board(request):
    """Close Board"""
    # REQUIRES: user is authenticated
    if not (# TODO: user is authenticated):
        raise HTTPError(401, "user is authenticated")

    # REQUIRES: user has write access
    if not (# TODO: user has write access):
        raise HTTPError(401, "user has write access")

    # REQUIRES: board is active
    if not (# TODO: board is active):
        raise HTTPError(400, "board is active")

    # --- implementation ---

    # ENSURES:
    # board.closed set to true
    # board moves to closed boards view


def reopen_board(request):
    """Reopen Board"""
    # REQUIRES: user is authenticated
    if not (# TODO: user is authenticated):
        raise HTTPError(401, "user is authenticated")

    # REQUIRES: user has write access
    if not (# TODO: user has write access):
        raise HTTPError(401, "user has write access")

    # REQUIRES: board is closed
    if not (# TODO: board is closed):
        raise HTTPError(400, "board is closed")

    # --- implementation ---

    # ENSURES:
    # board.closed set to false
    # board reappears in active boards view


def list_board(request):
    """List Board"""
    # REQUIRES: user is authenticated
    if not (# TODO: user is authenticated):
        raise HTTPError(401, "user is authenticated")

    # --- implementation ---

    # ENSURES:
    # array of board objects returned
    # only boards user has access to


def star_board(request):
    """Star Board"""
    # REQUIRES: user is authenticated
    if not (# TODO: user is authenticated):
        raise HTTPError(400, "user is authenticated")

    # REQUIRES: user is board member
    if not (# TODO: user is board member):
        raise HTTPError(400, "user is board member")

    # --- implementation ---

    # ENSURES:
    # board.starred set to true for this user
    # board appears in starred section

