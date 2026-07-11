# Pattern: Revocable Elevated-Access Session (support impersonation)

- **Category:** saas-infra (operator plane)
- **Trust class:** the session mint + guard + audit are all protected paths
- **Status:** specified (spec complete; `scaffold/` not yet built)

## What it is

The safe shape for **operator act-as**: a support admin temporarily acting *as* a
tenant user to reproduce a problem, with the whole session time-boxed, revocable,
audited under a dual identity, and unable to touch the things impersonation must
never touch. It is the machinery that lets support be effective without becoming a
standing backdoor.

The reason this is worth a pattern rather than "mint a token and go": the safety
is a **cluster of invariants that only work together**. Ship the mint without the
independent TTL and a leaked session lives as long as the identity token. Ship the
TTL without the deny-list and an impersonated session can delete the account it's
debugging. Ship both without dual-identity audit and you can't prove *who* did
what afterwards. Miss any one and "support convenience" becomes "audited backdoor
you didn't know you built."

## When to apply

Any multi-tenant product where support/ops genuinely need to see what a user sees
(SaaS with per-tenant data, anything with a "reproduce the customer's bug"
workflow). **Do not** stamp it if you don't need act-as — it is real attack
surface, only worth it when the support workflow demands it.

## Mechanism — generic components

Vendor-neutral: the "signer" is whatever mints your session token (a JWT signer, a
managed-auth custom token); the "identity provider" is your auth vendor.

- **Session token minted by the trusted signer, subject = the target.** The signer
  issues a token whose subject is the impersonated user, carrying an `imp` claim =
  `{actor (admin id), target, session_id, iat}`. The actor claim is set by the
  server at mint; a caller can never self-assert it, so the session is
  **un-forgeable at the signing root**.
- **Server-enforced TTL measured from the session's own `iat`, independent of the
  identity-provider token life.** The elevated window (e.g. 15 min) is enforced
  against `imp.iat`, *not* the IdP's token expiry (which may be an hour). A request
  past the window is rejected even with a still-valid identity token.
- **Future-skew guard.** An `imp.iat` more than a small tolerance (e.g. 30s) in the
  future is rejected — defends against clock drift / a forward-dated mint.
- **Revocable per session, fail-closed.** Ending a session writes a `revoked_at`;
  the guard does a **revocation lookup that fails *closed*** — if it can't confirm
  the session is live, it denies. Revocation safety outranks audit completeness.
- **No nested impersonation.** An already-impersonated session cannot mint another.
- **Deny-list with boundary-checked prefix matching.** An impersonated session is
  blocked from: the **operator plane** (`/admin/*`), **auth-factor changes**
  (email/password/MFA/uid — prefix-matched so future endpoints in the family are
  pre-blocked), and **destructive/GDPR** actions (data export + delete). Prefix
  matches carry a boundary check (`/`, `?`, `#`, or end) so `…/data-exported`
  doesn't match `…/data-export`. The deny rules are **exported** so the UI banner
  can enumerate exactly what's blocked.
- **Dual-identity audit, fail-closed, before every impersonated mutation.** Every
  mutating request under impersonation writes an audit row carrying *both*
  identities (actor admin + target) **before** the mutation proceeds; if the audit
  write fails, the mutation is refused.
- **Fixed middleware order.** Resolve session → guard (TTL/skew/revocation) →
  deny-list → audit → handler. The order is load-bearing and asserted.
- **A visible, non-dismissible banner** while a session is active (the human-facing
  half: the operator always knows they're acting as someone else).

## Invariant cluster

Realized 1:1 in the provenance app; the invariant ids are stamped directly in the
guard's code comments there.

| Generic ID | Invariant | Realized as (provenance) |
|--|--|--|
| `INV-EAS-001` | TTL enforced against the session's own `iat`, independent of the IdP token life. | dogeared-coach `INV-ADM-003`; `middleware/impersonation_guard.go:31-34,87-89` |
| `INV-EAS-002` | Revocable per-session; revocation lookup is fail-closed. | `INV-ADM-004`; `middleware/impersonation_guard.go:140-182` |
| `INV-EAS-003` | No nested impersonation. | `INV-ADM-005`; `middleware/impersonation_guard.go` |
| `INV-EAS-004` | Future-skew guard: `imp.iat` more than tolerance ahead is rejected. | `INV-ADM-009` (code); `middleware/impersonation_guard.go:36-38,98-108` |
| `INV-EAS-005` | Deny-listed from operator plane + auth-factor + destructive/GDPR, boundary-checked prefix; rules exported for the banner. | `INV-ADM-006/007`; `middleware/impersonation_deny_list.go:32-156` |
| `INV-EAS-006` | Every impersonated write is dual-identity audited, fail-closed, before the mutation. | `INV-ADM-008`; `middleware/impersonation_audit.go:82-110` |
| `INV-EAS-007` | Un-forgeable at the signing root: the session is minted by the trusted signer with an actor claim the caller cannot self-assert. | `INV-ADM-009`; `handlers/admin_impersonate.go:146-214` |

## Parameters

| Parameter | Required | Description |
|--|--|--|
| `ttl` | no (default `15m`) | Server-enforced elevated window, measured from `imp.iat`. |
| `future_skew_tolerance` | no (default `30s`) | Max tolerated forward clock drift on `imp.iat`. |
| `signer` | yes | What mints the session token (behind `provider-neutral-adapter`). |
| `deny_prefixes` | yes | The route families an impersonated session may never reach (operator plane, auth-factor, destructive). |
| `audit_sink` | yes | Where dual-identity audit rows are written (must fail closed). |

## Success metrics

| ID | Goal | Description |
|--|--|--|
| `unaudited_impersonated_mutations` | down | impersonated writes with no dual-identity audit row (target 0; gate-asserted). |
| `expired_session_acceptances` | down | requests accepted past TTL or after revocation (target 0). |
| `deny_list_escapes` | down | impersonated requests that reached a denied family (target 0). |

## Relationship to other patterns

- **`multi-tenant-isolation`** — the operator plane (`/admin/*`) this session is
  denied from is that pattern's out-of-band admin plane; the two share the
  "operator identity is orthogonal to the tenant graph" seam.
- **`provider-neutral-adapter`** — the signer + identity provider sit behind the
  neutral seam so the auth vendor is swappable.
- **`engagement-instrumentation` / audit** — the dual-identity audit stream is
  trust-tagged signal (operator actions are `trusted` origin).
