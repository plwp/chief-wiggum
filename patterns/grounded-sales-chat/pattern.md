# Pattern: Grounded Sales Chat (public pre-signup assistant)

- **Category:** monetization
- **Trust class:** the system prompt and its factual lists are an eval-gated protected path, and the whole surface is unauthenticated public spend — limits, breaker, and kill switch are protected paths too
- **Status:** specified (spec complete; `scaffold/` not yet built)
- **Depends on:** [`provider-neutral-adapter`](../provider-neutral-adapter) — the inference completer is a PNA seam: single-file vendor confinement, neutral message DTOs, vendor error shapes classified at the boundary
- **Feeds:** [`frictionless-onboarding`](../frictionless-onboarding) — the chat's only CTA is the free-tier signup; [`platform-cost-observability`](../platform-cost-observability) — per-call token/cost metering is PCO ingest; [`improvement-loop`](../improvement-loop) — transcripts and eval scores are end-user-signal (admin-gated)

## What it is

An LLM sales assistant on the public landing page: anonymous visitors ask
pre-signup questions ("does it do X?", "what does it cost?") and get grounded
answers plus a signup CTA. Mined from a shipped production SaaS's landing-page
assistant — an eval-tuned prompt (19/20 on its probe corpus) over a cheap
model, behind a four-layer spend stack.

Why a pattern and not "just call the API from the landing page": this surface
is **an unauthenticated endpoint that spends money and speaks for the
company**. Every invariant exists because its absence is a real failure class:
an ungrounded prompt invents discounts and features (hallucinated
commitments), an unowned prompt gets extracted or replaced by injection, an
unmetered public endpoint is a wallet-draining target, and rendered model
output is an XSS/phishing vector. The mined discipline's headline technique is
the **closure clause**: the prompt's capability and pricing lists are declared
COMPLETE, so anything absent *does not exist* and is denied plainly — the eval
found that grounding a true fact beats prohibiting a topic.

## When to apply

- A self-serve product wants pre-signup questions answered 24/7 without a
  human sales touch (async-first, no demo-call dependency).
- The product's facts (tiers, limits, workflows) already have in-repo sources
  of truth the prompt can be derived from and checked against.
- The operator accepts bounded LLM spend on an anonymous public surface and
  wants it contained by construction, not by hope.

## Mechanism — generic components

- **Server-owned system prompt.** The prompt is a server-side constant,
  prepended in the handler into a fresh message slice; the client role
  allowlist excludes `system` (rejected before the model is ever called); the
  prompt is never echoed, never persisted, never client-supplied.
  *(INV-GSC-001 — mined.)*
- **Grounded closed-world knowledge.** Every fact in the prompt — pricing,
  limits, workflows — traces to a declared in-repo source of truth (the tier
  matrix, the workflow docs), and the prompt declares those lists COMPLETE:
  absent means non-existent, denied plainly and positively. No invented
  features, integrations, discounts, timelines, or UI mechanisms.
  *(INV-GSC-002 — mined.)*
- **Eval-gated prompt changes.** The prompt is a versioned artifact. The eval
  harness extracts it from the shipped source (never a copy — it hard-fails if
  the constant stops being parseable), replicates production model parameters
  exactly, hash-stamps every transcript, and replays a probe corpus spanning
  factual, hallucination-bait, discount-pressure, prompt-extraction, off-topic,
  and tone categories. A prompt change ships only with a re-validated score.
  *(INV-GSC-003 — mined.)*
- **Untrusted output rendering.** "A model is not a contract": assistant text
  is rendered as plain text; only allowlisted hosts become links, and their
  hrefs come from a hardcoded canonical map, never from model output; no raw
  HTML path exists. Applied to assistant bubbles only. *(INV-GSC-004 — mined.)*
- **Four-layer spend stack, gate-ordered.** (1) Structural off: no provider
  key ⇒ handler never constructed ⇒ routes never registered ⇒ 404. (2) Admin
  kill switch: TTL-cached runtime flag, fail-open on read failure, every real
  transition durably audited. (3) Credits circuit breaker: trips on the
  vendor's quota-exhausted signal, admits exactly one half-open probe per
  interval, and only the request admitted *as* the probe may reset it (verdict
  fixed atomically at admission). (4) Two-tier rate limiter: per-client and
  global windows, where a per-client denial never consumes global budget and a
  missing client identifier is limited, not exempt. Checks run kill-switch →
  breaker → rate-limit → body-bind, so a dead feature costs a map lookup.
  *(INV-GSC-005 — mined.)*
- **Server-enforced conversation caps.** Max messages per window, max
  per-message length counted on *untrimmed* content, request body cap derived
  arithmetically from those two, last message must be user-role. UI mirrors
  are affordances; the server is the enforcement. *(INV-GSC-006 — mined.)*
- **Anonymous write-only transcript.** The conversation window is client-held
  and replayed each turn; the server persists at most one transcript document
  per client-minted, regex-validated session id (unique index, insert/update
  upsert split), storing no IP, no user-agent, no account identity. Logging is
  best-effort — it can never fail the visitor's reply — and transcripts carry
  a declared retention period. *(INV-GSC-007 — mined; the retention clause is
  design-derived from the mined gap.)*
- **Key confinement and opaque errors.** The provider key travels secret-store
  → auth header only: never logged, never wrapped into an error chain, never
  in a client response. Upstream error bodies are drained unlogged (they can
  echo prompt or visitor text); clients get fixed error codes, never upstream
  prose. *(INV-GSC-008 — mined.)*
- **Zero cost until the visitor speaks.** The widget makes no network call on
  mount; availability probing is deferred to visibility/intent, fires at most
  once, and fails open; a stale in-flight reply cannot resurrect a reset
  transcript (generation-counter guard). *(INV-GSC-009 — mined.)*

## Grounding

All nine invariants are **mined** from a shipped production SaaS's
landing-page sales assistant (per this registry's provenance policy,
private-repo paths are held out of the public registry; the manifest describes
the realized mechanisms). The one design-derived clause is INV-GSC-007's
declared retention period: the mined system persists visitor transcripts
indefinitely despite an otherwise identity-free PII posture — the gap is
promoted to a requirement rather than copied. Known v1 boundaries, stated not
hidden: no lead capture or human handoff (the only conversion path is the
signup CTA), no third-party embed story (first-party page section only), and
the global rate window is per-instance — the true ceiling is
`window × max_instances` until a shared store binds it. All three are
parameters or stated limits, not invariants. Per-call token/cost metering
(absence ≠ zero, vendor-reported cost, detached best-effort writes) is
[`platform-cost-observability`](../platform-cost-observability) discipline and
is not re-derived here.

## Parameters

| Parameter | Required | Meaning |
|--|--|--|
| `model` | yes | Inference model slug, config-overridable so a vendor rename is not a deploy (mined: a low-cost flash-tier model, max ~400 completion tokens, temperature 0.3). |
| `knowledge_sources` | yes | The in-repo sources of truth the prompt's facts must trace to (tier matrix, workflow docs) — INV-GSC-002's authority. |
| `probe_corpus` | yes | Eval probes with expected behaviors, covering at least the six mandatory categories (INV-GSC-003). |
| `link_allowlist` | yes | Canonical host→href map the renderer may linkify (INV-GSC-004). |
| `caps` | yes | `max_messages`, `max_message_len`, upstream timeout; body cap is derived (INV-GSC-006). |
| `rate_limits` | yes | Per-client and global windows (mined: 6/min per IP, 120/10min global per instance). |
| `transcript_retention` | yes | Declared transcript TTL (INV-GSC-007; the mined system's indefinite retention is the gap, not the default). |
| `regulated_advice_carveout` | no | Domain-specific "never advise on X, refer to a professional" rule (mined instance: clinical advice → "ask your vet"). |
| `kill_switch_cache_ttl` | no | Settings-cache TTL (mined: 30s, fail-open). |
| `breaker_probe_interval` | no | Half-open probe interval (mined: 15min). |

## Success metrics

`eval_pass_rate` ↑ (shipped-prompt score on the probe corpus),
`hallucination_probe_failures` = 0, `chat_to_signup_rate` ↑ (sessions reaching
the CTA that convert), `cost_per_session` ↓, `breaker_trips` = 0 (each one is
a spend-ceiling event worth a finding).

## Trust

Two protected paths. The prompt and its factual lists speak for the company —
a worker editing prompt copy, pricing facts, or the probe corpus without a
re-validated eval score is parked for human review. And the spend stack
(limits, breaker thresholds, kill switch, caps) bounds an unauthenticated
public endpoint's cost — widening any of it is a goalpost change, parked the
same way. Transcripts and eval scores are end-user signal: improvement-loop
proposals derived from them (prompt tuning, new grounded facts) are admin-gated.
