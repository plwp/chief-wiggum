package scp

// Not a smoke — present so the corpus has an un-annotated sibling and the
// scanner's file count is not trivially 1.

type Client struct{ base string }

func (c *Client) VenueInfo() error { return nil }
