# Binding: `engagement-instrumentation` → DIY Firestore analytics

- **Realizes:** [`engagement-instrumentation`](../../../engagement-instrumentation) (vendor-neutral spec)
- **Tier:** T0+ · **Vendor:** Firestore REST (no SDK, no vendor) · **Source:** `plwp.net`

The **zero-cost** end of the instrumentation seam: capture how far users get
through content with no analytics vendor, no Cloud Function, no server — a
cookieless beacon straight to Firestore, read offline by a CLI. This is the T0
building block that makes *every* other pattern's success metrics measurable for
$0.

## Write path (client → Firestore, direct)

A tiny IIFE exposes `track(event, detail)` that does a raw `fetch()` **POST to the
Firestore REST API** —
`https://firestore.googleapis.com/v1/projects/<project>/databases/(default)/documents/<collection>`
— with `keepalive: true` (survives page unload) and a hand-built Firestore REST
`fields` payload. Guards that matter:

- **Origin guard** — skip the write unless `location.hostname` matches the
  production domain, so local dev never pollutes the data.
- **Never break the page** — wrapped in `try/catch` + `.catch(()=>{})`; analytics
  failure is silent by construction.
- **No PII, cookieless** — capture `event`, `detail`, `path`, `referrer`, `ua`,
  `lang`, `tz` (rough geo), `screen`, `ts`. No user id, no cookie, no stored IP.

Instrument both navigation (`pageview`, `panel`) and in-content engagement
(`game_start`, `game_over`) — i.e. the "how far did they get" signal the pattern is
about.

## Security: public-write, server-only-read (the whole trick)

One collection, **append-only**, enforced by security rules:

```
match /hits/{id} {
  allow create: if request.resource.data.keys().hasOnly([...whitelist...])
                && <each field type/size validated>
                && request.resource.data.ts is timestamp;
  allow read, update, delete: if false;
}
match /{document=**} { allow read, write: if false; }   // deny-all catch-all
```

The public can **append well-formed beacons** but can never read or tamper. Reads
happen server-side only.

**Be honest about what this is not.** The endpoint is **unauthenticated and
abuseable**: the origin guard is **client-side only** (it's just an `if` in JS an
attacker skips by POSTing straight to the Firestore REST URL), and `ts` is
**client-supplied** so it's untrusted for ordering/geo. A hostile actor can
**poison your metrics** (inflate events), **exhaust the ~20k/day free write quota**,
and — **if billing is enabled — run up billable writes**. The append-only `hasOnly`
schema bounds the *shape* of abuse (small, typed, whitelisted fields only), not its
*volume*. Mitigate per how much the numbers matter: **validate every field**
(`hasAll` + a type/size check on each, not just some), put a **project budget
alert** + a **hard write quota** on the database, and when it graduates past "vanity
metrics" move the write behind **App Check** or a **rate-limited Cloud Run endpoint**
with a **server-set timestamp**.

## Read / aggregate / display

A stdlib-only CLI (`stats.py`): authenticate with `gcloud auth print-access-token`,
POST a Firestore `runQuery` (`structuredQuery` over the collection, filtered
`ts >= now - N days`), aggregate in-memory with `collections.Counter` (pageviews/day
ASCII chart, event counts, referrers, timezones, UA-derived browser/device), with a
**bot filter** (`bot`/`crawl`/`headless`/`lighthouse` UA markers). Dashboard = the
terminal; no stored aggregates, no scheduled rollups.

## Trust (this is the important bit for the loop)

These writes are **public and unauthenticated → `untrusted` signal**. Per the
[improvement-loop trust model](../../../improvement-loop/pattern.md#trust-model),
any finding a loop derives from this data inherits `untrusted`, so a change it
proposes routes to **quarantine → blocking admin approval**. The append-only
`hasOnly` schema also *bounds* the injection surface — an attacker can only write
whitelisted, size-capped fields, not arbitrary payloads. Bind this source
`untrusted` at apply time.

## Cost

Genuinely **~$0**: 1 write per event sits well under the Firestore free tier
(~20k writes/day); reads happen only when the owner runs the CLI. No always-on
server, no vendor SaaS. This is the cost floor of the whole stack.

## Graduating up

When you outgrow it: keep the append-only + trust-tag discipline, move the write
behind a Cloud Run endpoint (so you can enrich server-side: real geo from IP, a
trusted server timestamp, rate limiting), and add scheduled rollups. The *shape*
(append-only, server-derived trust fields, one-path aggregation) is what carries
forward — see the vendor-neutral spec.

## Stand it up

See [`skills/firestore-diy-analytics-setup.md`](../skills/firestore-diy-analytics-setup.md).
