# Skill: Secret Manager containers + least-privilege runtime access

The secrets discipline every T1+ binding on this stack depends on. Outcome: IaC
creates secret **containers** and grants the **runtime** SA read on exactly those;
**values are added out-of-band** and never touch tfstate, `.tfvars`, env files, or
the repo. Mirrors [chief-wiggum's own rule](../../../../CLAUDE.md): secrets never
become environment variables in your tooling either.

> Bind first: `PROJECT`, the runtime service account `RUNTIME_SA`, and the logical
> secret → env-var map (e.g. `resend-api-key → RESEND_API_KEY`).

## 1. Create containers via IaC (Terraform), env-suffixed

```hcl
variable "secret_ids" { default = ["resend-api-key", "mongodb-atlas-uri", "stripe-secret-key", "stripe-webhook-secrets"] }

resource "google_secret_manager_secret" "s" {
  for_each  = toset(var.secret_ids)
  secret_id = "${each.value}-${var.env}"     # e.g. resend-api-key-prod
  replication { auto {} }
}

# Grant ONLY the runtime SA read on ONLY these secrets (least privilege)
resource "google_secret_manager_secret_iam_member" "runtime_read" {
  for_each  = google_secret_manager_secret.s
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.runtime_sa}"
}
```

Terraform creates **containers only**. It never sees a secret value.

## 2. Add values out-of-band (operator shell, not CI)

```bash
printf %s "$THE_ACTUAL_KEY" | gcloud secrets versions add resend-api-key-prod \
  --project="$PROJECT" --data-file=-
```

Rotate by adding a new version; consumers reading `latest` pick it up on next boot.
For rotation-list secrets (`stripe-webhook-secrets`) store a comma-separated value
and let the app accept any entry.

## 3. Inject into Cloud Run at runtime (not build time)

```bash
gcloud run deploy "$SERVICE" --project="$PROJECT" --region="$REGION" \
  --set-secrets="RESEND_API_KEY=resend-api-key-${ENV}:latest,\
STRIPE_SECRET_KEY=stripe-secret-key-${ENV}:latest,\
STRIPE_WEBHOOK_SECRETS=stripe-webhook-secrets-${ENV}:latest"
```

Cloud Run mounts the value as an env var **in the running container only** — it is
not in the image, the build logs, or the deploy config.

## 4. Keep the deployer out

The **deploy** SA (`github-deployer`) must **not** have `secretAccessor` — only the
**runtime** SA does. This is the split that bounds blast radius: a compromised deploy
job can roll an image but cannot read a single secret. Verify:

```bash
gcloud secrets versions access latest --secret=resend-api-key-prod \
  --impersonate-service-account="$DEPLOYER_SA"   # must FAIL
```

## Non-secret config

Public/non-secret config (API URLs, `NEXT_PUBLIC_*`, feature flags) goes in
`--env-vars-file=.env.yaml` or build env — **not** Secret Manager. Only true secrets
get a container.

## Verify

- `terraform plan` shows secret **containers**, zero secret **values**.
- The running service reads the key; the repo and tfstate contain none.
- The deployer SA access check above **fails**; the runtime SA succeeds.
