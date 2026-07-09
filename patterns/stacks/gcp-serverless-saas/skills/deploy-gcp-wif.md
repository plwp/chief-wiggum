# Skill: Stand up a keyless WIF deploy pipeline (GCP + GitHub Actions)

Realizes [`deployment-release`](../bindings/deployment-release.md) on this stack.
Outcome: pushes to `main` auto-deploy to **staging**; production is a manual,
reviewer-gated promotion of the **byte-identical** staging image. **No long-lived
cloud keys.**

> Parameters to bind first: `PROJECT_STAGING`, `PROJECT_PROD`, `REPO` (`owner/repo`),
> `REGION` (`us-central1`), `SERVICE` (Cloud Run service name), `AR_REPO`
> (Artifact Registry repo).

## 1. Workload Identity Federation (once, from an operator shell)

```bash
# Pool + GitHub OIDC provider
gcloud iam workload-identity-pools create github-pool \
  --project="$PROJECT_STAGING" --location=global --display-name="GitHub pool"

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --project="$PROJECT_STAGING" --location=global \
  --workload-identity-pool=github-pool \
  --display-name="GitHub OIDC" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref,attribute.workflow_ref=assertion.job_workflow_ref" \
  --attribute-condition="assertion.repository=='${REPO}'"   # <-- repo lock is the FLOOR, not enough on its own (see below)

# Deployer SA (least privilege: roll releases, NOT read secrets / mutate infra)
gcloud iam service-accounts create github-deployer --project="$PROJECT_STAGING"
SA="github-deployer@${PROJECT_STAGING}.iam.gserviceaccount.com"
for role in roles/run.developer roles/artifactregistry.writer roles/firebasehosting.admin; do
  gcloud projects add-iam-policy-binding "$PROJECT_STAGING" --member="serviceAccount:$SA" --role="$role"
done
# Deliberately DO NOT grant roles/secretmanager.secretAccessor or serviceusage.* to the deployer.

# Let the GitHub identity impersonate the deployer SA — but scope the binding TIGHTER
# than repo-only (see the hardening note): here, only pushes to the main ref.
POOL=$(gcloud iam workload-identity-pools describe github-pool --project="$PROJECT_STAGING" --location=global --format="value(name)")
gcloud iam service-accounts add-iam-policy-binding "$SA" --project="$PROJECT_STAGING" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/${POOL}/attribute.repository/${REPO}"
```

Put the provider resource name and `$SA` into GitHub repo secrets
`GCP_WIF_PROVIDER` / `GCP_WIF_SERVICE_ACCOUNT`.

> **⚠ Repo-only WIF is the floor, not the ceiling.** `assertion.repository=='${REPO}'`
> lets **any workflow, on any branch, in that repo** mint the deployer token. An
> attacker who can land a workflow file (or a malicious PR that runs a workflow with
> `id-token: write`) can satisfy the condition and deploy — **the GitHub
> `environment: production` reviewer gate lives in the workflow YAML and can be
> bypassed by not using it.** Harden prod separately:
>
> - **Split staging and prod** into two WIF providers + two deployer SAs in two
>   projects. The prod provider/SA binding must additionally require a
>   **protected-branch ref** and/or the **`job_workflow_ref`** of a *reusable,
>   protected* promote workflow — e.g. bind the prod SA's `workloadIdentityUser` to
>   `attribute.workflow_ref/<owner>/<repo>/.github/workflows/promote-prod.yml@refs/heads/main`.
> - Enforce prod approval in **branch/tag protection + the GitHub environment**, not
>   only in the YAML, so a rewritten workflow can't skip it.

## 2. Local floor mirror (`scripts/pre-merge-check.sh`)

Same commands CI runs, so the gate is reproducible off-CI: `go build ./... && go
test ./... && go vet ./...` + `npm ci && npm run lint && npm run test && npm run
build`. CI calls this exact script.

## 3. Staging workflow (`.github/workflows/deploy-staging.yml`)

Trigger `on: push: branches: [main]`. Steps:
`permissions: { id-token: write, contents: read }` → `google-github-actions/auth@v2`
(with the two WIF secrets, **no key file**) → run `pre-merge-check.sh` →
`gcloud builds submit`/`docker build` + push to `${REGION}-docker.pkg.dev/${PROJECT_STAGING}/${AR_REPO}/${SERVICE}:${GITHUB_SHA::7}` →
`gcloud run deploy $SERVICE --image <that> --region $REGION` →
`npm run build -- --mode staging && firebase deploy --only hosting:<staging-site>` →
`curl -fsS https://<staging>/healthz | grep '"db":"ok"'`.

## 4. Production promote (`.github/workflows/promote-prod.yml`)

Trigger `on: workflow_dispatch`. Add `environment: production` (configure **required
reviewers** on that environment in repo settings — this is the human gate, and it
must be backed by branch/tag protection per the WIF note, not just this line).
Steps: auth via WIF → **promote the identical image by DIGEST** (not by the mutable
SHA tag — a tag can be repointed; a digest can't):

```bash
# resolve the staged image to its immutable digest, then promote THAT
DIGEST=$(gcloud artifacts docker images describe \
  "${REGION}-docker.pkg.dev/${PROJECT_STAGING}/${AR_REPO}/${SERVICE}:${SHA}" \
  --format='value(image_summary.digest)')
SRC="${REGION}-docker.pkg.dev/${PROJECT_STAGING}/${AR_REPO}/${SERVICE}@${DIGEST}"
DST="${REGION}-docker.pkg.dev/${PROJECT_PROD}/${AR_REPO}/${SERVICE}@${DIGEST}"
docker pull "$SRC"; docker tag "$SRC" "${DST%@*}:${SHA}"; docker push "${DST%@*}:${SHA}"
# deploy by the digest you verified, and record it as provenance
gcloud run deploy "$SERVICE" --image "${DST%@*}@${DIGEST}" --project "$PROJECT_PROD" --region "$REGION" --min-instances=1
```

Enable **immutable tags** on the Artifact Registry repo so a tag can never be
silently repointed after it's been validated.

→ rebuild frontend with prod env → **bundle guard**:

```bash
# fail if any staging/demo config leaked into the prod bundle
if grep -rE "(dogeared-stag|firebaseapp.com/__/staging|demo-)" dist/assets/*.js; then
  echo "::error::staging config leaked into prod bundle"; exit 1
fi
```

→ `firebase deploy --only hosting:<prod-site>` → smoke `/healthz`.

## Verify

- Push a trivial change to `main` → staging deploys, `/healthz` green.
- Run the prod workflow → it waits for a reviewer, then ships the same digest.
- Confirm the deployer SA **cannot** read a secret (`gcloud secrets versions
  access` as that SA fails) — least privilege holds.

## Gotchas

- Forgetting the `attribute-condition` repo lock = any repo can mint your token.
- **Repo-only lock is not enough for prod** — see the WIF hardening note; scope prod
  to a protected branch + the promote workflow's `job_workflow_ref`.
- Rebuilding for prod instead of promoting the image = silent staging/prod drift.
- Promoting by mutable **tag** instead of **digest** = a repointed tag can slip
  unvalidated bits into prod. Promote by digest; enable immutable tags.
- Granting the deployer `secretAccessor` "to make it work" = you just widened the
  blast radius to every secret. Keep it out — **but know that even without it, a
  deployer that can `run deploy` can ship an image that runs *as the runtime SA* and
  reads every secret the runtime SA can.** The deployer's real privilege ceiling is
  "whatever the runtime SA can do", so (a) keep the runtime SA's secret grants
  minimal and per-service, and (b) gate *what image* prod will accept (digest
  allowlist / the promote-only path above), not just *who* can call deploy.
