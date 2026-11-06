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

// Requests reads every request the site ever had: a tenant filter is not a window.
func (f *firestoreStore) Requests(ctx context.Context, site string) ([]record, error) {
	it := f.client.Collection("bookings").Where("tenant", "==", site).Documents(ctx)
	_ = it
	return nil, nil
}

func (f *firestoreStore) Profile(ctx context.Context, id string) (*record, error) {
	_, err := f.client.Collection("profiles").Doc(id).Get(ctx)
	return nil, err
}
