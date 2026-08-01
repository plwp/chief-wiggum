package billing

import "testing"

// A test fixture writing stripe_plan directly (via the same $set shape as
// production) — must be surfaced as a writer but never a violation (test
// files are fixtures, not production write paths).
func TestReconcileSetsPlan(t *testing.T) {
	fake := &fakeCollection{}
	fake.UpdateOne(ctx, bson.M{"_id": "1"}, bson.M{"$set": bson.M{"stripe_plan": "pro"}})
}
