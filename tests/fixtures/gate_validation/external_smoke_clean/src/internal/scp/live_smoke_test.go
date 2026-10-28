package scp

import "testing"

// One real round-trip against SCP. Pinned by case= so the results match is
// exact rather than file-shaped.
//
// @cw-smoke SCP case=TestSCPLiveVenueInfo
func TestSCPLiveVenueInfo(t *testing.T) {
	resp, err := liveClient(t).VenueInfo(t.Context())
	if err != nil {
		t.Fatalf("venue-info: %v", err)
	}
	if resp.Opens == "" {
		t.Fatal("venue-info returned no opening time")
	}
}
