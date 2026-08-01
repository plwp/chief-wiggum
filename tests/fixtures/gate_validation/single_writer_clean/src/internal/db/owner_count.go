package db

// RemoveStaff is the sanctioned single writer of provider.active_owner_count
// (INV-seat-003, an in-memory field — no sink=db, so any assignment counts).
func RemoveStaff(p *Provider) {
	p.ActiveOwnerCount = p.ActiveOwnerCount - 1
}
