# Skill: Stand up zero-cost DIY Firestore analytics

Realizes [`engagement-instrumentation`](../bindings/engagement-instrumentation.md)
at T0. Outcome: a cookieless beacon writing straight to Firestore, read offline by a
CLI. **No SDK, no Cloud Function, no vendor, ~$0.**

> Bind first: `PROJECT`, `COLLECTION` (e.g. `hits`), `PROD_HOST` (origin guard),
> the field whitelist.

## 1. Firestore + append-only rules (the security core)

Create a Firestore database, then deploy rules that allow **public append of
well-formed docs only** — no read, no update, no delete:

```
rules_version = '2';
service cloud.firestore {
  match /databases/{db}/documents {
    match /hits/{id} {
      allow create: if
        request.resource.data.keys().hasOnly(['event','detail','path','referrer','ua','lang','tz','screen','ts'])
        && request.resource.data.keys().hasAll(['event','ts'])
        // validate EVERY field's type + size (not just some) — an unvalidated field is an abuse vector
        && request.resource.data.event    is string && request.resource.data.event.size()    < 64
        && request.resource.data.detail   is string && request.resource.data.detail.size()   < 256
        && request.resource.data.path     is string && request.resource.data.path.size()     < 512
        && request.resource.data.referrer is string && request.resource.data.referrer.size() < 512
        && request.resource.data.ua       is string && request.resource.data.ua.size()       < 512
        && request.resource.data.lang     is string && request.resource.data.lang.size()     < 32
        && request.resource.data.tz       is string && request.resource.data.tz.size()       < 64
        && request.resource.data.screen   is string && request.resource.data.screen.size()   < 16
        && request.resource.data.ts       is timestamp;   // NB: client-supplied, untrusted for ordering
      allow read, update, delete: if false;
    }
    match /{document=**} { allow read, write: if false; }   // deny-all catch-all
  }
}
```

```bash
firebase deploy --only firestore:rules
```

The `hasOnly`/`hasAll` key set + a per-field type/size check **bound the injection
surface** — a public writer can only ever append small, typed, whitelisted fields.
They do **not** bound *volume*.

> **⚠ This endpoint is unauthenticated and abuseable.** The origin guard in the
> beacon (below) is **client-side only** — an attacker POSTs straight to the
> Firestore REST URL and skips it. They can inflate your metrics and burn the
> ~20k/day free write quota (or your money if billing is on). Put a **project budget
> alert** + a **hard database write quota** in place, treat `ts` as untrusted, and
> when the numbers start to matter front the write with **App Check** or a
> **rate-limited Cloud Run endpoint that sets a server timestamp**. This T0 recipe
> is for vanity / product-signal metrics — not billing-grade or adversarial counting.

## 2. The beacon (inline, no build)

```html
<script>
(function(){
  var P="PROJECT", C="COLLECTION";
  window.track=function(event,detail){
    if(!/(^|\.)PROD_HOST$/.test(location.hostname)) return;      // anti-noise only — client-side, NOT security
    try{
      fetch("https://firestore.googleapis.com/v1/projects/"+P+"/databases/(default)/documents/"+C,{
        method:"POST", keepalive:true, headers:{"Content-Type":"application/json"},
        body:JSON.stringify({fields:{
          event:{stringValue:event||"pageview"}, detail:{stringValue:detail||""},
          path:{stringValue:location.pathname}, referrer:{stringValue:document.referrer},
          ua:{stringValue:navigator.userAgent}, lang:{stringValue:navigator.language},
          tz:{stringValue:Intl.DateTimeFormat().resolvedOptions().timeZone},
          screen:{stringValue:screen.width+"x"+screen.height},
          ts:{timestampValue:new Date().toISOString()}
        }})
      }).catch(function(){});                                    // never break the page
    }catch(e){}
  };
  track("pageview");
})();
</script>
```

Call `track('panel','pricing')`, `track('game_start')`, etc. at engagement points.

## 3. Read it (offline CLI, `stats.py`)

Stdlib only — auth with a gcloud token, `runQuery` filtered to the last N days,
aggregate with `collections.Counter`:

```python
# python3 stats.py --days 7   (needs: gcloud auth login)
import json,subprocess,urllib.request,collections,sys
PROJECT="PROJECT"; days=7
tok=subprocess.check_output(["gcloud","auth","print-access-token"]).decode().strip()
q={"structuredQuery":{"from":[{"collectionId":"hits"}],
   "where":{"fieldFilter":{"field":{"fieldPath":"ts"},"op":"GREATER_THAN",
     "value":{"timestampValue":__import__("datetime").datetime.utcnow().isoformat()+"Z"}}}}}
# (swap the filter value for now-minus-N-days in real use)
req=urllib.request.Request(
  f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents:runQuery",
  data=json.dumps(q).encode(), headers={"Authorization":f"Bearer {tok}","Content-Type":"application/json"})
rows=[r["document"]["fields"] for r in json.load(urllib.request.urlopen(req)) if "document" in r]
c=collections.Counter(f["event"]["stringValue"] for f in rows)
print(c.most_common())
```

Add a **bot filter** (drop UAs matching `bot|crawl|headless|lighthouse`) and a
per-day pageview bar chart as needed.

## 4. Bind the trust tag

When wiring this into an improvement loop, declare this source **`untrusted`**
(public, unauthenticated). Findings derived from it inherit `untrusted` → quarantine
→ blocking admin approval. See the binding's trust section.

## Verify

- Load the prod page → a `hits` doc appears. Load from `localhost` → **no** doc
  (origin guard).
- Try to `read` the collection from a browser console → **denied** (rules hold).
- Try to append a doc with an extra field → **denied** (`hasOnly` holds).
- `python3 stats.py --days 7` prints counts.

## Cost

Free: ~1 write/event under the ~20k/day Firestore free tier; reads only when you run
the CLI. No server, no vendor.
