package main

import (
	"context"
	"net/http"
	"sync"
	"time"
)

type server struct {
	store   actionStore
	mu      sync.Mutex
	running map[string]bool
}

type actionStore interface {
	AppendAction(ctx context.Context, site, id string) error
	Requests(ctx context.Context, site, from string) ([]record, error)
	Profiles(ctx context.Context, ids []string) ([]record, error)
}

type record struct{ ID string }

func (s *server) handleStaff(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	site := r.URL.Query().Get("site")
	from := r.URL.Query().Get("from")
	// Two independent reads in parallel, joined before the response.
	var recs []record
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		recs, _ = s.store.Requests(ctx, site, from)
	}()
	wg.Wait()
	ids := make([]string, 0, len(recs))
	for _, rec := range recs {
		ids = append(ids, rec.ID)
	}
	s.store.Profiles(ctx, ids)
	s.kickSweep(site, recs)
	w.Header().Set("Content-Type", "application/json")
}

// kickSweep starts the sweep in the background, one per site; it never waits.
func (s *server) kickSweep(site string, recs []record) {
	s.mu.Lock()
	if s.running[site] {
		s.mu.Unlock()
		return
	}
	s.running[site] = true
	s.mu.Unlock()
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
		defer cancel()
		for _, rec := range recs {
			s.store.AppendAction(ctx, site, rec.ID)
		}
	}()
}
