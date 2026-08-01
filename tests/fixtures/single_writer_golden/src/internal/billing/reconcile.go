package billing

// ReconcileStripe is the sanctioned single writer of provider.stripe_plan.
func ReconcileStripe(c *mongo.Collection, id ID, plan string) {
	c.UpdateOne(ctx, bson.M{"_id": id}, bson.M{"$set": bson.M{"stripe_plan": plan}})
}

// EffectiveLimits builds an in-memory struct — not a persistence write, and
// Plan here is a different (unrelated) in-memory field, exercising the
// sink=db filter that only counts DB sinks for INV-bil-001.
func EffectiveLimits(p *Provider) Limits {
	out := Limits{}
	out.Plan = "computed"
	return out
}
