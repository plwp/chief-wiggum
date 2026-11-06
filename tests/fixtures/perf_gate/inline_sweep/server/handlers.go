package main

import (
	"context"
	"net/http"
	"time"
)

type server struct {
	store actionStore
}

type actionStore interface {
	// A declaration, not a write: the scanner must not count it.
	AppendAction(ctx context.Context, site, id string) error
	Requests(ctx context.Context, site string) ([]record, error)
	Profile(ctx context.Context, id string) (*record, error)
}

type record struct {
	ID string
	// Field types named like store verbs are declarations, not calls.
	Set    func(ctx context.Context) error
	Update int
}

func (s *server) handleStaff(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	s.handleBookings(w, r)
}

func (s *server) handleBookings(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	site := r.URL.Query().Get("site")
	recs, err := s.store.Requests(ctx, site)
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	s.applySweep(ctx, site, recs)
	for _, rec := range recs {
		p, _ := s.store.Profile(ctx, rec.ID)
		_ = p
	}
	w.Header().Set("Content-Type", "application/json")
}

// applySweep confirms each matched request: one store write per item.
func (s *server) applySweep(ctx context.Context, site string, recs []record) {
	for _, rec := range recs {
		if ctx.Err() != nil {
			return
		}
		s.store.AppendAction(ctx, site, rec.ID)
	}
}

// handleClient serves GET and POST; only the POST branch writes.
func (s *server) handleClient(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		w.WriteHeader(http.StatusOK)
	case http.MethodPost:
		s.store.AppendAction(r.Context(), "site", "id")
	}
}

// handleImport is registered POST-only, so its write is not a read path.
func (s *server) handleImport(w http.ResponseWriter, r *http.Request) {
	s.store.AppendAction(r.Context(), "site", "import")
}
