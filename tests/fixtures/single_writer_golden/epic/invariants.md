# Invariants

**INV-bil-001**: single atomic Stripe→plan write / single write path
<!-- @cw-writes INV-bil-001 controls_field=provider.plan,provider.stripe_plan sanctioned_writers=ReconcileStripe,internal/billing/reconcile.go sink=db -->

**INV-leg-002**: single write path for the legacy quota override
<!-- @cw-writes INV-leg-002 controls_field=provider.quota_minutes sanctioned_writers=internal/legacy/store.go sink=db -->

**INV-tier-004**: single write path for the hyphenated Mongo key plan-tier
<!-- @cw-writes INV-tier-004 controls_field=provider.plan-tier sanctioned_writers=SetPlanTier sink=db -->
