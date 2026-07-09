# SaaS Gate - Non-Functional Requirements Validation

Validates common SaaS non-functional requirements (security, tenant isolation, performance, data integrity, observability) against a running app, and reports actionable pass/fail per category. Run standalone or as an extra gate in `/close-epic`.

The build-side complement to a SaaS baseline: it proves a chief-wiggum-built (or cloned) SaaS app meets production standards before shipping.

For products that hold **regulated or sensitive data** (health, financial, biometric, children's, government-classified, or PII at scale), the runtime checks are necessary but not sufficient — there is also a **design-time compliance posture** to verify (data classification, privacy/cross-border law, retention + legal hold, de-identification, the AI/LLM data-path gate). That posture is captured in `docs/compliance-requirements.md` (filled from `templates/compliance-requirements.md`), and this gate checks it exists and its controls are evidenced. See Step 5.

## Usage
```
/saas-gate <owner/repo> --base-url <url> [--auth-mode cookie|bearer]
```

## Statuses

The gate reports five statuses so it never claims more than it proved: `pass`, `fail`, `warn`, `skipped`, `not_applicable`. As a `/close-epic` gate it fails the epic **only on a real `fail`** (real evidence or an explicit contract), not on `warn`/`skipped`.

## Workflow

### Step 1: Resolve paths and detect the stack

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
TARGET_REPO=$(python3 "$CW_HOME/scripts/repo.py" resolve "$owner_repo")
```

`saas_gate.py` auto-detects the stack (Go/Node/Python) from the repo. Confirm the dependency profile if needed: `python3 "$CW_HOME/scripts/check_deps.py" --for core`.

### Step 2: Get a running app

The runtime checks need a live base URL. If the app isn't already running, **start it** (don't punt): `docker compose up -d` (wait for healthy), or the repo's run command. Use the local URL (e.g. `http://localhost:8080`). For an already-deployed environment, pass its URL.

If you genuinely cannot start the app, run the gate without `--base-url` — it still reports the static/structured-log checks and marks the runtime checks `skipped` (honest, not a false pass).

### Step 3: Run the gate

```bash
python3 "$CW_HOME/scripts/saas_gate.py" --repo "$TARGET_REPO" \
  --base-url "$BASE_URL" --auth-mode cookie \
  --rate-limit-path /login --markdown
```

Options: `--require-https` (require HSTS), `--rate-limit-path` / `--rate-limit-required` (fail if no limiter on a known endpoint), `--auth-mode bearer` (CSRF marked not-applicable for API/token auth), `--log-sample <file>` (validate structured logging), `--gate` (exit non-zero on any `fail`), `--json`.

### Step 4: The live-app checks

`saas_gate.py` covers what it can hermetically (security headers, CSRF cookie posture, rate-limit probe, health, structured-log format). Some checks genuinely need a live multi-user app — run them as part of this step and record the result:

- **Tenant isolation**: create two users, have user A create a resource, confirm user B gets `401/403/404` fetching it (a `200` is a data-leak `fail`). The helper exposes `check_tenant_isolation(make_user, create_resource, fetch_resource)` for this.
- **Performance**: sample representative endpoints under a realistic load and compare against the target SLOs.
- **Data integrity**: confirm an audit trail is written and soft-delete is honored (verify in the datastore / via the API).

### Step 5: The regulated-data compliance dimension (design-time)

Run this dimension **only if the product holds regulated/sensitive data** (otherwise mark the whole dimension `not_applicable`). It is a design-time checklist, not a runtime probe — it verifies the compliance posture is defined and evidenced, drawing its criteria from the product's `docs/compliance-requirements.md`.

- **Requirements doc present** — `docs/compliance-requirements.md` exists and is filled (not the raw template). Absent for a regulated-data product is a `fail`.
- **Data classification** — a classification is stated and drives handling.
- **AI/LLM data-path GATE** — if regulated data is sent to any third-party model, the doc records a resolved residency / no-training / no-retention posture (in-region model, signed DPA, reasonable-steps/transfer-impact file). Unresolved is a `fail` — this gate is load-bearing.
- **Encryption + immutable audit** — customer-managed keys at rest and a tamper-evident audit trail of regulated-data access are evidenced in the running app (extends the Step 4 data-integrity check).
- **Retention + legal hold** — a retention schedule exists and a `legal_hold` hard-block on deletion is implemented.
- **De-identification** — analytics/benchmarking run on de-identified data with small-cell suppression; identified data is access-walled.
- **Individual rights** — access/correction (and erasure where required) workflows exist with the right SLAs.
- **Unresolved `TBD:` legal items** — surface any `TBD:` in the requirements doc; a `TBD:` blocking a shipped data-touching feature is a `fail` (a guessed lawful basis / retention period must not ship). Reuse `scripts/check_unresolved.py` against the doc.

### Step 6: Report

Present the per-category pass/fail, including the regulated-data compliance dimension (Step 5) where applicable. Flag every `fail` with the actionable fix. As a `/close-epic` gate, a `fail` blocks the epic close; `warn`/`skipped` are surfaced but don't block.
