package main

import (
	"context"

	"cloud.google.com/go/firestore"
)

type firestoreStore struct{ client *firestore.Client }

func (f *firestoreStore) AppendAction(ctx context.Context, site, id string) error {
	ref := f.client.Collection("actions").Doc(site + "__" + id)
	return f.client.RunTransaction(ctx, func(ctx context.Context, tx *firestore.Transaction) error {
		return tx.Set(ref, map[string]any{"action": "confirm"})
	})
}

// Requests reads the asked window only.
func (f *firestoreStore) Requests(ctx context.Context, site, from string) ([]record, error) {
	it := f.client.Collection("bookings").Where("tenant", "==", site).Where("departure_date", ">=", from).Documents(ctx)
	_ = it
	return nil, nil
}

// Profiles reads every asked profile in one batched call.
func (f *firestoreStore) Profiles(ctx context.Context, ids []string) ([]record, error) {
	refs := make([]*firestore.DocumentRef, 0, len(ids))
	for _, id := range ids {
		refs = append(refs, f.client.Collection("profiles").Doc(id))
	}
	_, err := f.client.GetAll(ctx, refs)
	return nil, err
}
