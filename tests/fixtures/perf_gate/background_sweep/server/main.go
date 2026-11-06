package main

// The #434 fix's shape, for the perf gate's clean trial (chief-wiggum#524):
// the read starts the sweep in the background and returns at once; reads are
// windowed and batched.

import "net/http"

func main() {
	srv := &server{}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/staff/", srv.handleStaff)
	http.ListenAndServe(":8080", mux)
}
