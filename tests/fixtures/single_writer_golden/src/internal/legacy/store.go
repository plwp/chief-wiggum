package legacy

// UpdateQuotaOverride is the sanctioned (by file path) single writer of
// provider.quota_minutes (INV-leg-002, sink=db — SQL UPDATE ... SET).
func UpdateQuotaOverride(db *sql.DB, id string, minutes int) error {
	_, err := db.Exec("UPDATE providers SET quota_minutes = $1 WHERE id = $2", minutes, id)
	return err
}
