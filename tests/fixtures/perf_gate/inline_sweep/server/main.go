package main

// Planted defect for the perf gate's self-test (chief-wiggum#524): the shape
// of booking-forms #434 before its fix. GET /api/staff/{site}/bookings runs a
// write-side sweep inline, one store write per item, under a 20 s deadline.

import "net/http"

func main() {
	srv := &server{}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/staff/", srv.handleStaff)
	mux.HandleFunc("/api/client", srv.handleClient)
	mux.HandleFunc("POST /api/import", srv.handleImport)
	http.ListenAndServe(":8080", mux)
}
