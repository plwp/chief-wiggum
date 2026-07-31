# Pattern: Referral / Invite Growth Loop

- **Category:** process-loop
- **Trust class:** reward economics + invite copy are end-user-signal-driven, so they are a protected path (admin-gated)
- **Status:** specified (spec complete; `scaffold/` not yet built)
- **Depends on:** [`elevated-access-session`](../elevated-access-session) — reuses its signed-token discipline
- **Feeds:** [`improvement-loop`](../improvement-loop) — attribution is trust-tagged signal the loop can optimize against

## What it is

A self-serve growth loop: an existing user **invites** a new one, the invite is a
**signed, single-use, expiring token**, accepting it **attributes** the new account
to the referrer server-side, and both sides get a **reward** — once, idempotently.
The whole thing is trackable (k-factor, accept rate) and the reward/copy are tunable
*under admin gating*, so the improvement loop can propose better economics without a
public-facing paywall change shipping unreviewed.

Why a pattern and not "just add an invite link": the value is the **invariants that
keep it un-gameable**. An invite link that carries a client-editable `?ref=` is a
free-money bug; a reward that isn't idempotent double-pays on a retry; attribution
that trusts a client field lets anyone credit themselves. The token discipline that
makes this safe is **the same one `elevated-access-session` already establishes** —
un-forgeable at the signing root, TTL against the token's own issue time, fail-closed
on the consume/revoke lookup — so this pattern **reuses** it rather than re-deriving.

## When to apply

- You want existing users to bring new ones through a trackable, self-serve loop.
- An invite must be **attributable server-side** and **rewardable on both sides**.
- Reward economics or invite copy will be tuned over time and must stay admin-gated.

## Mechanism — generic components

- **Signed invite token (un-forgeable).** The invite is a token minted by the trusted
  signer carrying the referrer as an actor claim the accepting client cannot
  self-assert — so the invitee can never forge who referred them. *(Reuses the
  `elevated-access-session` signing root; INV-RIL-001 ← INV-EAS-007.)*
- **TTL against the token's own iat.** The token expires on its own issue time,
  independent of any session/IdP lifetime. *(INV-RIL-002 ← INV-EAS-001.)*
- **Consume-once, fail-closed.** Accepting an invite consumes the token exactly once;
  a replay or retry-after-accept is rejected fail-closed. *(INV-RIL-003, the sibling
  of `elevated-access-session`'s fail-closed revocation lookup, INV-EAS-002.)*
- **Server-trusted attribution.** Who referred whom is derived server-side from the
  *consumed token*, never from a client-supplied `referrer` field, and emitted as a
  trust-tagged signal. *(INV-RIL-004 — design-derived.)*
- **Grant-only, idempotent two-sided reward.** Each accepted invite rewards each side
  at most once, keyed on the consumed token; issuance is idempotent under retries and
  grant-only (a replay never re-grants, never revokes). *(INV-RIL-005 — design-derived.)*
- **Fail-closed fraud guard + protected economics.** Self-referral (referrer ==
  invitee) and re-attribution of an already-attributed account are rejected; reward
  size and invite copy are end-user-signal-driven and therefore a protected path —
  the improvement loop may *propose* tuning, but changes are admin-gated, never
  auto-applied. *(INV-RIL-006 — design-derived.)*

## Grounding

The token-discipline invariants (INV-RIL-001/002/003) are **not re-derived** — they
cite the in-repo [`elevated-access-session`](../elevated-access-session) cluster
(INV-EAS-007 un-forgeability, INV-EAS-001 TTL-against-own-iat, INV-EAS-002 fail-closed
lookup), which is the same signed, single-use, time-boxed, un-forgeable-token shape.
The attribution/reward/fraud invariants (INV-RIL-004/005/006) are **design-derived**
and marked as such: this loop has not yet been mined from a shipped app, so they
capture the standard, well-understood referral-integrity discipline (server-trusted
attribution, idempotent grant-only rewards, self-referral rejection) rather than a
per-app realization. They ground fully when a product first builds the loop.

## Parameters

| Parameter | Required | Meaning |
|--|--|--|
| `signer` | yes | The trusted signing root that mints invite tokens (same root as `elevated-access-session` if adopted). |
| `token_ttl` | yes | Invite lifetime, enforced against the token's own iat. |
| `reward_spec` | yes | The two-sided reward on a successful, attributed accept. |
| `attribution_sink` | yes | Where the server-trusted attribution signal is recorded. |
| `consume_store` | yes | The consume-once store keyed on the token id. |
| `already_attributed_check` | no | How an account is judged already-attributed (re-attribution guard). |

## Success metrics

`invites_sent` ↑, `invite_accept_rate` ↑, `k_factor` ↑, `reward_fraud_rate` ↓,
`reward_cost_per_attributed_signup` ↓. Reward/copy are protected-path — the
improvement loop proposes; a human approves (INV-RIL-006).

## Trust

The signer/mint path, the consume-once accept handler, the reward-grant path, and
`reward_spec` + invite copy are all protected paths. A worker touching them is parked
for human review, exactly as with any goalpost.
