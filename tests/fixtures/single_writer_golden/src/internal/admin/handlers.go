package admin

// ChangePlan is a LEGACY admin control — a SECOND writer of provider.stripe_plan,
// which INV-bil-001 declares must have a single sanctioned writer.
func ChangePlan(c *mongo.Collection, id ID, newPlan string) {
	c.UpdateOne(ctx, bson.M{"_id": id}, bson.M{"$set": bson.M{"plan": newPlan}})
}

// RemoveStaff is the sanctioned writer of provider.active_owner_count
// (INV-seat-003, an in-memory field — no sink=db, so any assignment counts).
func RemoveStaff(p *Provider) {
	p.ActiveOwnerCount = p.ActiveOwnerCount - 1
}
