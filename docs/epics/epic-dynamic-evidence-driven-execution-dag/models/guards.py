"""
Auto-generated guard clauses from formal contracts.
Generated from formal model. Do not edit by hand.
"""

def commit_graph_event(request):
    """Commit Graph Event"""
    # REQUIRES: The caller holds the exclusive SQLite write transaction and the envelope base revision equals the current graph revision.
    if not (tx.exclusive and envelope.base_revision == graph.graph_revision):
        raise HTTPError(409, "The caller holds the exclusive SQLite write transaction and the envelope base revision equals the current graph revision.")

    # REQUIRES: The envelope is bounded, version-recognized and safely decodable into either a mutation candidate or a typed rejection envelope before the transaction commits.
    if not (bounded(envelope_bytes) and known_major_version and decodable_to_candidate_or_rejection(envelope)):
        raise HTTPError(409, "The envelope is bounded, version-recognized and safely decodable into either a mutation candidate or a typed rejection envelope before the transaction commits.")

    # --- implementation ---

    # ENSURES:
    # Exactly one hash-chained event commits atomically and accepted graph effects become visible together.
    # Rejected decisions advance journal_seq but not graph_revision; accepted state changes advance both exactly once.


def import_tracker_intent(request):
    """Import Tracker Intent"""
    # REQUIRES: The tracker adapter reports capability and scan status and supplies a source revision/hash; malformed or unavailable input is not interpreted as an empty graph.
    if not (source.status in ('observed','degraded','unavailable','malformed') and source.digest != null):
        raise HTTPError(422, "The tracker adapter reports capability and scan status and supplies a source revision/hash; malformed or unavailable input is not interpreted as an empty graph.")

    # --- implementation ---

    # ENSURES:
    # Human intent remains tracker/ratchet authoritative and is imported one-way as immutable version-bound facts.


def project_static_waves(request):
    """Project Static Waves"""
    # REQUIRES: Intent compilation has a verified source status and ticket-level projection is possible.
    if not (intent.scan_status == 'observed' and ticket_projection_defined(graph)):
        raise HTTPError(503, "Intent compilation has a verified source status and ticket-level projection is possible.")

    # --- implementation ---

    # ENSURES:
    # The projection is byte-equivalent to plan_waves.py for all six fields waves, gated, skipped, warnings, integration_risks and gate_reasons, with exit 0 success, 1 invalid input and 2 cycle.


def derive_ready_set(request):
    """Derive Ready Set"""
    # REQUIRES: All decision inputs are from the pinned journal projection, including logical ticks, provider health, budgets, policy, leases and resources.
    if not (all(inputs in journal_projection for inputs in readiness_inputs)):
        raise HTTPError(400, "All decision inputs are from the pinned journal projection, including logical ticks, provider health, budgets, policy, leases and resources.")

    # --- implementation ---

    # ENSURES:
    # A node is ready iff admitted, nonterminal, all schedulable predecessors succeeded, no active block/control constraint exists, resources and provider capabilities are claimable, and retry/budget limits allow dispatch.


def acquire_fenced_lease_and_dispatch(request):
    """Acquire Fenced Lease and Dispatch"""
    # REQUIRES: The node is currently ready, the next lease epoch is atomically allocated, and all shared mutable resources are claimable.
    if not (ready(node) and epoch == prior_epoch + 1 and resources_claimable(node)):
        raise HTTPError(409, "The node is currently ready, the next lease epoch is atomically allocated, and all shared mutable resources are claimable.")

    # --- implementation ---

    # ENSURES:
    # Claim acquisition and dispatch-request outbox event commit atomically before process launch; every result and Git action must present the current fencing token.


def promote_candidate(request):
    """Promote Candidate"""
    # REQUIRES: All candidates have terminal attempt outcomes, the chosen candidate succeeded and passed the frozen independent verifier, isolation is proven, and the group has no winner.
    if not (all_terminal(group) and chosen.outcome == 'succeeded' and verifier_passed(chosen, group.verifier_hash) and isolation_proven(group) and group.winner == null):
        raise HTTPError(409, "All candidates have terminal attempt outcomes, the chosen candidate succeeded and passed the frozen independent verifier, isolation is proven, and the group has no winner.")

    # --- implementation ---

    # ENSURES:
    # One atomic event marks exactly one candidate promoted and all other eligible candidates superseded without changing immutable attempt outcomes.


def apply_operator_control(request):
    """Apply Operator Control"""
    # REQUIRES: An authenticated runtime operator supplies identity, exact action, non-empty reason and the current base revision.
    if not (operator.authenticated and action in ('pause','resume','cancel') and reason != '' and base_revision == current_revision):
        raise HTTPError(400, "An authenticated runtime operator supplies identity, exact action, non-empty reason and the current base revision.")

    # --- implementation ---

    # ENSURES:
    # The action is journaled; a running worker pauses or cancels only after a safe commit boundary and lease release/revocation.


def route_execution(request):
    """Route Execution"""
    # REQUIRES: Required capabilities, budget, health observations, objective escalation evidence and provider policy are pinned by digest.
    if not (all(hash_pinned(x) for x in (requirements, budget, health, evidence, policy))):
        raise HTTPError(422, "Required capabilities, budget, health observations, objective escalation evidence and provider policy are pinned by digest.")

    # --- implementation ---

    # ENSURES:
    # Selection is deterministic, cheapest capable first, escalation requires an objective trigger, and the author and verifier independence groups differ.


def run_agentic_openrouter_worker(request):
    """Run Agentic OpenRouter Worker"""
    # REQUIRES: Execution is in a verified non-main worktree with workspace-write sandbox; a trusted orchestrator resolves the key through a command-backed helper outside the model-controlled workspace.
    if not (assert_worktree() and not_main_checkout and sandbox == 'workspace-write' and credential_helper_not_invokable_by_model):
        raise HTTPError(401, "Execution is in a verified non-main worktree with workspace-write sandbox; a trusted orchestrator resolves the key through a command-backed helper outside the model-controlled workspace.")

    # --- implementation ---

    # ENSURES:
    # Success atomically publishes schema-valid metadata, result, log and DONE; failure publishes ERROR with one stable reason; DONE and ERROR are mutually exclusive.


def execute_registered_arm(request):
    """Execute Registered Arm"""
    # REQUIRES: Pre-registration predates all runs and fixes corpus strata, factorial cells, deterministic blocked-randomization/interleaving algorithm, assignment unit/log, budgets, tools, sandbox, verifier, seeds, stopping rules, non-inferiority margin, gap formula and degenerate cases.
    if not (preregistered_at < first_run_at and arm_policy_frozen and assignment_matches_preregistered_randomizer):
        raise HTTPError(412, "Pre-registration predates all runs and fixes corpus strata, factorial cells, deterministic blocked-randomization/interleaving algorithm, assignment unit/log, budgets, tools, sandbox, verifier, seeds, stopping rules, non-inferiority margin, gap formula and degenerate cases.")

    # --- implementation ---

    # ENSURES:
    # Every run publishes an immutable manifest and raw result bound to the ratchet journal; headline slices include N and confidence intervals and retain protocol violations and negative results.

