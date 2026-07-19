package tier

// SetPlanTier is the sanctioned single writer of the hyphenated Mongo key
// provider.plan-tier (INV-tier-004, sink=db).
func SetPlanTier(c *mongo.Collection, id ID, v string) {
	c.UpdateOne(ctx, bson.M{"_id": id}, bson.M{"$set": bson.M{"plan-tier": v}})
}

// LegacyTier is an UNSANCTIONED second writer of the same hyphenated key —
// the emission layer must capture non-\w quoted field names or this real
// violation would be invisible to a full scan.
func LegacyTier(c *mongo.Collection, id ID, v string) {
	c.UpdateOne(ctx, bson.M{"_id": id}, bson.M{"$set": bson.M{"plan-tier": v}})
}
