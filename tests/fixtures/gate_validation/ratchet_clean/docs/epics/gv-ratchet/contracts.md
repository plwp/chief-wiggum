# GV Ratchet Fixture — Contracts

A minimal epic doc so ratchet's contract-definition hashing has real
markdown-declared stable-ID blocks to hash.

## CTR-rt-001 — create_widget validates its name

REQUIRES: the request carries a non-empty `name` no longer than 64 characters.
ENSURES: exactly one widget row is created with a server-assigned id and the
caller's name; no other field is written by this path.
