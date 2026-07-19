package billing

// ReconcileStripe is the sanctioned single writer of provider.stripe_plan.
func ReconcileStripe(c *mongo.Collection, id ID, plan string) {
	c.UpdateOne(ctx, bson.M{"_id": id}, bson.M{"$set": bson.M{"stripe_plan": plan}})
}
