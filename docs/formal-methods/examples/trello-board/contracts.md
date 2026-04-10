## Entity: Board

Reverse-engineered entity from domain model

### Canonical Fields
| Field | Type | Required | Source of Truth | Notes |
|-------|------|----------|-----------------|-------|
| id | string | always | — | immutable |
| name | string | always | — | — |
| desc | string | optional | — | — |
| closed | boolean | optional | — | — |
| starred | boolean | optional | — | — |
| idOrganization | ObjectID | optional | organizations collection | — |
| prefs | string | optional | — | — |
| url | string | always | — | — |
| shortUrl | string | always | — | — |
| dateLastActivity | string | optional | — | — |
| memberships | string | optional | — | — |

### POST /1/boards

- **REQUIRES**: user is authenticated; name is non-empty string; name is provided
- **ENSURES**: board created with status active; board.id returned; user added as admin member; default lists created if defaultLists=true
- **ERROR CASES**: 401 if not authenticated; 400 if name is empty or missing

### GET /1/boards/{id}

- **REQUIRES**: user is authenticated; user has access to board
- **ENSURES**: board object returned with requested fields
- **ERROR CASES**: 401 if not authenticated; 404 if board does not exist; 401 if user does not have access

### PUT /1/boards/{id}

- **REQUIRES**: user is authenticated; user has write access to board; board is not deleted
- **ENSURES**: board updated with new values; dateLastActivity updated
- **ERROR CASES**: 401 if not authenticated or no write access; 404 if board does not exist

### DELETE /1/boards/{id}

- **REQUIRES**: user is authenticated; user is board admin
- **ENSURES**: board marked as deleted; board no longer appears in member board lists; all lists and cards become inaccessible
- **ERROR CASES**: 401 if not authenticated or not admin; 404 if board does not exist

### PUT /1/boards/{id}

- **REQUIRES**: user is authenticated; user has write access; board is active
- **ENSURES**: board.closed set to true; board moves to closed boards view
- **ERROR CASES**: 401 if not authenticated or no write access

### PUT /1/boards/{id}

- **REQUIRES**: user is authenticated; user has write access; board is closed
- **ENSURES**: board.closed set to false; board reappears in active boards view
- **ERROR CASES**: 401 if not authenticated or no write access

### GET /1/members/me/boards

- **REQUIRES**: user is authenticated
- **ENSURES**: array of board objects returned; only boards user has access to
- **ERROR CASES**: 401 if not authenticated

### PUT /1/boards/{id}

- **REQUIRES**: user is authenticated; user is board member
- **ENSURES**: board.starred set to true for this user; board appears in starred section
