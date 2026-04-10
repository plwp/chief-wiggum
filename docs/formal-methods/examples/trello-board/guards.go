// Auto-generated guard clauses from formal contracts.
// Generated from formal model. Do not edit by hand.

package handlers

import "fmt"

// POST /1/boards
func CreateBoard(w http.ResponseWriter, r *http.Request) {
	// REQUIRES: user is authenticated
	if !(# TODO: user is authenticated) {
		http.Error(w, "user is authenticated", 401)
		return
	}

	// REQUIRES: name is non-empty string
	if !(# TODO: name is non-empty string) {
		http.Error(w, "name is non-empty string", 400)
		return
	}

	// REQUIRES: name is provided
	if !(request.body.name is not None) {
		http.Error(w, "name is provided", 400)
		return
	}

	// --- implementation ---

	// ENSURES:
	// board created with status active
	// board.id returned
	// user added as admin member
	// default lists created if defaultLists=true
}


// GET /1/boards/{id}
func ReadBoard(w http.ResponseWriter, r *http.Request) {
	// REQUIRES: user is authenticated
	if !(# TODO: user is authenticated) {
		http.Error(w, "user is authenticated", 401)
		return
	}

	// REQUIRES: user has access to board
	if !(# TODO: user has access to board) {
		http.Error(w, "user has access to board", 404)
		return
	}

	// --- implementation ---

	// ENSURES:
	// board object returned with requested fields
}


// PUT /1/boards/{id}
func UpdateBoard(w http.ResponseWriter, r *http.Request) {
	// REQUIRES: user is authenticated
	if !(# TODO: user is authenticated) {
		http.Error(w, "user is authenticated", 401)
		return
	}

	// REQUIRES: user has write access to board
	if !(# TODO: user has write access to board) {
		http.Error(w, "user has write access to board", 401)
		return
	}

	// REQUIRES: board is not deleted
	if !(# TODO: board is not deleted) {
		http.Error(w, "board is not deleted", 401)
		return
	}

	// --- implementation ---

	// ENSURES:
	// board updated with new values
	// dateLastActivity updated
}


// DELETE /1/boards/{id}
func DeleteBoard(w http.ResponseWriter, r *http.Request) {
	// REQUIRES: user is authenticated
	if !(# TODO: user is authenticated) {
		http.Error(w, "user is authenticated", 401)
		return
	}

	// REQUIRES: user is board admin
	if !(# TODO: user is board admin) {
		http.Error(w, "user is board admin", 401)
		return
	}

	// --- implementation ---

	// ENSURES:
	// board marked as deleted
	// board no longer appears in member board lists
	// all lists and cards become inaccessible
}


// PUT /1/boards/{id}
func CloseBoard(w http.ResponseWriter, r *http.Request) {
	// REQUIRES: user is authenticated
	if !(# TODO: user is authenticated) {
		http.Error(w, "user is authenticated", 401)
		return
	}

	// REQUIRES: user has write access
	if !(# TODO: user has write access) {
		http.Error(w, "user has write access", 401)
		return
	}

	// REQUIRES: board is active
	if !(# TODO: board is active) {
		http.Error(w, "board is active", 400)
		return
	}

	// --- implementation ---

	// ENSURES:
	// board.closed set to true
	// board moves to closed boards view
}


// PUT /1/boards/{id}
func ReopenBoard(w http.ResponseWriter, r *http.Request) {
	// REQUIRES: user is authenticated
	if !(# TODO: user is authenticated) {
		http.Error(w, "user is authenticated", 401)
		return
	}

	// REQUIRES: user has write access
	if !(# TODO: user has write access) {
		http.Error(w, "user has write access", 401)
		return
	}

	// REQUIRES: board is closed
	if !(# TODO: board is closed) {
		http.Error(w, "board is closed", 400)
		return
	}

	// --- implementation ---

	// ENSURES:
	// board.closed set to false
	// board reappears in active boards view
}


// GET /1/members/me/boards
func ListBoard(w http.ResponseWriter, r *http.Request) {
	// REQUIRES: user is authenticated
	if !(# TODO: user is authenticated) {
		http.Error(w, "user is authenticated", 401)
		return
	}

	// --- implementation ---

	// ENSURES:
	// array of board objects returned
	// only boards user has access to
}


// PUT /1/boards/{id}
func StarBoard(w http.ResponseWriter, r *http.Request) {
	// REQUIRES: user is authenticated
	if !(# TODO: user is authenticated) {
		http.Error(w, "user is authenticated", 400)
		return
	}

	// REQUIRES: user is board member
	if !(# TODO: user is board member) {
		http.Error(w, "user is board member", 400)
		return
	}

	// --- implementation ---

	// ENSURES:
	// board.starred set to true for this user
	// board appears in starred section
}

