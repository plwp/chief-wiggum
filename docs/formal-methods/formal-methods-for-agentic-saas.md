# Formal methods for agentic SaaS reverse-engineering

**The most practical formal methods pipeline for an LLM-powered agent to reverse-engineer SaaS applications combines three layers: XState statecharts for observable UI behavior, Quint/TLA+ for deeper behavioral invariants, and Alloy for structural constraints—all connected by automata learning (AALpy), property-based conformance testing (Hypothesis/Schemathesis), and a verifier-in-the-loop code generation pattern.** This architecture works because LLM-friendliness is the decisive constraint: formalisms must be generatable by LLMs at high accuracy, and the 2024–2026 research explosion in LLM+formal methods has identified exactly which tools clear that bar. The key insight from both industrial practice (AWS, Amazon's decade of TLA+) and recent benchmarks (SysMoBench, FM-BENCH) is that LLMs alone produce unreliable formal specs, but LLMs paired with model checker feedback loops and grammar-constrained generation achieve **68–96% verification success rates** depending on the formalism.

---

## The three-layer specification architecture

A SaaS application's behavior operates at distinct levels of abstraction, and no single formalism captures all of them well. The research points to a layered approach where each formalism handles what it does best.

**Layer 1: XState/Statecharts for observable workflows.** XState (v5.30, actively maintained by Stately.ai) is the clear winner for modeling UI state machines, multi-step forms, authentication flows, and CRUD lifecycle patterns. Its JSON/TypeScript syntax is **maximally aligned with LLM training data**—LLMs generate XState machines with high reliability because the npm ecosystem contains over 1,356 dependent packages and massive documentation. XState's built-in model-based testing module (`@xstate/graph`) generates all possible paths through a state machine via Dijkstra's algorithm, producing test plans with full state and transition coverage. Crucially, XState machines are directly executable—the specification *is* the code—eliminating the spec-to-code gap entirely. Hierarchical states, parallel regions, guards, and the actor model handle the complexity of real SaaS workflows. The limitation is formal rigor: XState cannot prove temporal properties or complex invariants.

**Layer 2: Quint/TLA+ for behavioral invariants and temporal properties.** For properties like "an order, once confirmed, eventually transitions to shipped or cancelled" or "concurrent edits never lose data," you need temporal logic. **Quint** (developed by Informal Systems) provides a TypeScript-inspired syntax for TLA+ logic with type checking, a REPL, and VS Code integration—making it far more LLM-friendly than raw TLA+. The **Quint LLM Kit** exists specifically for AI-assisted spec generation. Quint specs are checked by **Apalache** (symbolic model checker backed by Z3), which in 2025 added a **JSON-RPC server API** enabling external tools to drive symbolic execution programmatically—exactly what an agent needs. Apalache's interactive symbolic testing capability has been demonstrated on TFTP and blockchain protocols for conformance checking. The TLA+ ecosystem is the most actively researched for LLM integration: the **TLAi+ Challenge 2025** (TLA+ Foundation + NVIDIA) produced Specula, an open-source framework that derives TLA+ specs from source code, and the **SysMoBench** and **TLAiBench** benchmarks are driving rapid progress. However, an EPITA technical report found that open-source LLMs still produce unparsable TLA+ code, so grammar-constrained generation and verifier feedback loops are essential.

**Layer 3: Alloy 6 for structural and relational constraints.** Alloy excels at modeling entity relationships, data schema invariants, and authorization rules—"every order references a valid product," "no user is simultaneously admin and restricted." Alloy 6 (v6.2.0, January 2025) adds temporal operators (`always`, `eventually`, `after`) via the `var` keyword, enabling behavioral modeling over relational structures. The SAT-solver-backed analyzer finds satisfying instances (test data) and counterexamples (violation scenarios) automatically. Recent research shows LLMs are **surprisingly effective at Alloy**: experiments with OpenAI o3-mini and DeepSeek R1 found they could generate ≥10 correct unique formulas for 10 out of 11 relational properties, and GPT-4-family models successfully repair buggy Alloy specs when given analyzer feedback.

---

## Automata learning extracts models from running systems

The most direct path from "observed SaaS behavior" to "formal model" runs through **active automata learning**—algorithms that build state machine models by systematically querying a running system.

**Angluin's L* algorithm** and its successors (TTT, ADT, L#) learn Mealy machine models by issuing **membership queries** (executing action sequences against the SaaS and observing responses) and **equivalence queries** (checking whether the current hypothesis model matches the real system). The running application serves as the "teacher." For SaaS applications, membership queries map to HTTP request sequences executed via browser automation or API calls; equivalence queries are approximated through conformance testing techniques (random walks, W-method). **AALpy** (v1.5.3, December 2025, Python, MIT license) from TU Graz is the recommended library for Python-based agentic systems. It supports both active learning (L*, KV algorithms for DFA, Mealy, and Moore machines) and passive learning (RPNI from traces), plus non-deterministic and stochastic model learning—critical because real web apps exhibit non-deterministic behavior from network latency, caching, and concurrent users. The **System Under Learning interface** requires implementing just `reset()` and `step()` methods, mapping naturally to browser session management and HTTP requests.

**LearnLib** (Java, Apache 2.0, CAV 2025 paper: "LearnLib: 10 years later") remains the gold standard with the broadest algorithm selection, including **RALib** for register automata that handle data parameters—essential because SaaS APIs involve user IDs, session tokens, and other values that basic Mealy learning treats as separate symbols, causing alphabet explosion. LearnLib's companion project **ALEX** is a web application specifically designed for learning models of web applications via browser automation and REST APIs, evaluated with 140 students. For a Python-based agent, AALpy is more practical, with LearnLib/RALib available via Java interop for advanced register automata needs.

The recommended strategy is **hybrid passive-then-active learning**. First, use RPNI (passive, polynomial time) on traces captured from API documentation examples, recorded browser sessions, and HTTP traffic logs to build an initial model quickly. Then switch to active learning (L* or KV for Mealy machines) to refine the model by querying the live system. Recent work by Kruger et al. (TACAS 2024) shows that L# with "state matching" against a reference model achieves **two orders of magnitude improvement** when leveraging partial prior knowledge—exactly the scenario where an LLM has already extracted a rough model from documentation.

**LLMs dramatically accelerate the abstraction layer.** The hardest part of automata learning for SaaS is defining the input alphabet—mapping concrete HTTP requests to abstract symbols. **RESTSpecIT** (2024–2025) demonstrates that LLMs can infer **88.62% of documented API routes** and **89.25% of parameters** from just an API name, using their pre-trained knowledge of common API patterns. **ProtocolGPT** achieves >90% accuracy on inferring protocol state machines from implementation code using retrieval-augmented generation. **PROSPER** (HotNets 2023) and **Hermes** (USENIX Security 2024) extract FSMs from natural language specifications with 81–87% accuracy. An LLM agent can thus parse API docs and help center articles to define the alphabet and provide skeleton state machines that active learning then refines.

---

## Specification mining complements automata learning

Beyond learning complete state machines, specification mining extracts individual properties and invariants from traces.

**Daikon** (v5.8.23, June 2025, actively maintained) performs dynamic invariant detection by checking ~75 invariant templates against observed execution traces—constants, ranges, linear relationships, ordering, containment, conditional implications. Adapted for HTTP traffic, Daikon could discover constraints like "field X is always present when status=200" or "array length ≤ 100" from API response data, requiring a custom front-end to convert request/response pairs into Daikon's trace format.

**Synoptic** mines temporal properties from system logs in three forms: "x AlwaysFollowedBy y," "x NeverFollowedBy y," and "x AlwaysPrecedes y." Applied to HTTP request logs, it discovers ordering constraints like "login AlwaysPrecedes dashboard_access." **Texada** generalizes this to arbitrary **Linear Temporal Logic (LTL)** formulas of any length, discovering complex patterns like "Globally, whenever POST /orders succeeds, eventually GET /orders/{id} returns 200." The **nl2spec** framework (CAV 2023, 86.1% accuracy) can translate natural language documentation directly into LTL formulas using LLM-powered sub-translation decomposition.

These mined properties serve dual purposes: they enrich the formal model learned by automata learning, and they become **test oracles**—automatically generated assertions that the clone must satisfy.

---

## Model-based and property-based testing close the conformance loop

Once you have a formal model, generating tests and checking conformance uses two complementary approaches.

**ioco theory** (input-output conformance, Jan Tretmans, University of Twente) provides the theoretical foundation. An implementation I conforms to specification S (`I ioco S`) if, after any suspension trace of S, every output and quiescence that I produces is permitted by S. This precisely defines what "behavioral conformance" means for the SaaS clone: the clone may implement a subset of behaviors, but it must never produce outputs the specification doesn't foresee. The **TorX/JTorX** tools implement ioco-based on-the-fly testing, and Apalache's symbolic testing capability performs conformance checking for Quint/TLA+ specs.

For practical test execution, three tools form the recommended stack:

- **Schemathesis** (Python, built on Hypothesis, used by Spotify, Red Hat, JetBrains) auto-generates property-based tests from OpenAPI schemas with **stateful testing mode** that tests multi-step workflows (create→get→update→delete). It is the **most production-ready tool for API conformance testing** and requires zero per-endpoint configuration.
- **Hypothesis `RuleBasedStateMachine`** (Python, v6.151+) enables custom stateful property tests where rules correspond to API operations, bundles carry resource IDs between operations, and invariants verify clone behavior matches the model. The **icontract-hypothesis** bridge automatically generates Hypothesis tests from Design-by-Contract annotations.
- **AltWalker** (Python/.NET wrapper for GraphWalker) performs graph-based model-based testing for UI workflows, walking state machine models with configurable coverage criteria (100% edge coverage, random walk, A* paths) while executing Playwright actions at each step.

The **original SaaS application itself is the strongest test oracle** (differential/back-to-back testing). Running identical input sequences against both original and clone and comparing outputs provides ground truth that no formal model can fully replace. The formal model serves as a structured intermediary that enables systematic exploration of the input space rather than ad-hoc manual testing.

---

## Code generation follows a constrain-then-generate pattern

The most promising approach for going from formal model to working code is not traditional code generation (which targets embedded/safety-critical systems) but rather **using formal specs to constrain LLM code generation**.

**The TSL+LLM pattern** (Santolucito et al., 2024) demonstrates the principle: Temporal Stream Logic provides a correct-by-construction control structure, then LLMs fill in data transformation "holes." Applied to SaaS reverse-engineering, XState machines provide the control flow skeleton (correct state transitions, guard conditions, action sequences), while LLMs generate the implementation logic for each action. This separation means the structural correctness is guaranteed by the formal model while the LLM handles the creative parts.

**Dafny as a verification intermediate language** (POPL 2025) offers another powerful pattern. The LLM generates code in Dafny (Microsoft's verification-aware language) rather than the target language directly. Dafny's verifier checks pre/postconditions and loop invariants automatically, then compiles verified code to Python, Rust, Java, C#, JavaScript, or Go. This achieves **~77% pass@1 on HumanEval**, and success rates jumped from 68% to **96% in one year** (2024→2025) as LLMs improved. The user (or agent) never needs to read Dafny—it serves purely as a verification checkpoint.

**VeCoGen** (2024) automates this further: LLMs generate C programs from ACSL specifications, then Frama-C with SMT solvers (Z3, CVC4, Alt-Ergo) verifies correctness, and failures are fed back iteratively until the code passes. **Astrogator** (Berkeley, 2025) applies formal verification to LLM-generated Ansible code, proving correctness in 83% of cases. The **Property-Generated Solver (PGS)** dual-agent pattern (2025) uses a Generator agent and a Tester agent with property-based testing as the core validation engine, achieving **23–37% improvement** over test-driven development baselines.

For the practical code generation stack, the recommended path is:

- **OpenAPI spec → server/client stubs** via openapi-generator (supports 40+ languages), providing the API surface skeleton
- **XState machines → executable TypeScript** directly (the spec is the code for UI state management)
- **itemis CREATE → TypeScript/Python/Java** for complex statechart-based workflow logic (actively maintained, used by MAN, BSH, and automotive OEMs)
- **Design-by-Contract decorators** (Python `deal` or `icontract`) on generated business logic, enabling runtime verification and automatic test generation
- **LLM-generated implementation** constrained by all of the above, with Dafny or direct verification as a quality gate

---

## Lightweight formal methods provide practical guardrails

Full mathematical proof is unnecessary for SaaS cloning. Several lightweight approaches provide significant formal rigor with minimal overhead.

**Design by Contract** bridges formal specifications and running code. Python's `deal` library (production-ready since 2018) provides `@deal.pre`, `@deal.post`, `@deal.pure`, and `@deal.inv` decorators with zero runtime dependencies, plus Z3-based formal verification via deal-solver and automatic Hypothesis test generation. `icontract` (Parquery) adds correct inheritance semantics and informative violation messages. An LLM agent can generate contracts directly from observed API behavior: "this endpoint requires authentication (precondition), always returns a JSON object with an 'id' field (postcondition), and the total count never decreases after a create operation (invariant)."

**Session types and the Scribble protocol language** model multi-party interaction protocols. A Scribble global protocol describes the complete interaction pattern between participants (client, server, third-party service), which is then projected to local protocols (per-role finite state machines). **StMungo** translates Scribble protocols into typestate specifications and Java API skeletons that are statically verified. Research from 2019 demonstrates session-type-safe web development with WebSockets and TypeScript. For SaaS reverse-engineering, discovered API interaction patterns (e.g., OAuth flows, payment processing sequences) could be modeled as Scribble protocols and projected to conformant client code. The tooling remains primarily academic, but the FSM representation maps directly to XState machines.

**OpenAPI/AsyncAPI specifications function as partial formal models.** They capture data types, interface signatures, parameter constraints, and authentication schemes with formal precision (via JSON Schema), but miss behavioral semantics, temporal ordering, and complex invariants. The **IDL extension** (Martin-Lopez et al., 2020) adds inter-parameter dependency constraints ("if parameter A is set, parameter B must also be present"). Enriching OpenAPI specs with pre/postconditions, state machine annotations, and temporal constraints transforms them into the API-layer equivalent of a formal specification—and the ecosystem of code generators, validators, and test tools is vastly larger than any academic formalism.

---

## LLMs and formal methods are converging rapidly

The 2024–2026 period represents an inflection point. Multiple benchmarks now track LLM performance on formal methods tasks, and the trajectory is steep.

**FM-BENCH** (ACL 2025) provides 4,000 test pairs across Coq, Lean 4, Dafny, ACSL, and TLA+ covering six formal verification sub-tasks. Fine-tuning on the companion **FM-ALPACA** dataset (18,000 instruction-response pairs) yielded **up to ~3x performance improvement** and—surprisingly—also enhanced math, reasoning, and general coding skills, suggesting formal methods training provides transferable benefits. The **Vericoding benchmark** tracked Dafny verification success rising from 68% (Claude 3 Opus, June 2024) to 96% (frontier models, 2025) in just one year.

The most reliable pattern across all successful systems is the **verifier-in-the-loop**: generate a candidate specification or code with the LLM, validate with a model checker or verifier, feed errors back, and iterate. Grammar-constrained generation (using GBNF syntax or the Guidance framework to enforce valid formal language at the token level) eliminates syntactic errors. RAG with databases of verified specifications guides the LLM toward correct patterns. The TLAi+ Challenge's second-place entry used exactly this approach: grammar-constrained local LLMs that could only produce syntactically valid TLA+.

Where LLMs currently fail is instructive: they struggle with **complex distributed system modeling** (SysMoBench shows poor performance on Etcd Raft and Redis), **compositional reasoning** across module boundaries (DafnyComp benchmark), and **TLA+-specific idioms** like 1-based indexing. The practical implication is that LLM-generated formal specs must always be validated mechanically—which is exactly what model checkers provide.

---

## A realistic end-to-end methodology

Combining all the research, the following pipeline represents the most viable approach for an LLM-powered agent to observe → model → test → generate → verify a SaaS application.

**Phase 1: Intelligence gathering and alphabet construction.** The agent uses browser automation (Playwright) and HTTP interception (mitmproxy) to capture interaction traces. Simultaneously, it processes API documentation, help center articles, and training video transcripts. The LLM applies RESTSpecIT-style inference to construct an initial OpenAPI specification (~89% route accuracy) and nl2spec-style extraction to identify temporal behavioral properties from natural language (~86% accuracy). This phase produces: a draft OpenAPI spec, a set of LTL properties, and a corpus of interaction traces.

**Phase 2: Model construction with hybrid learning.** RPNI (passive, via AALpy) builds initial Mealy machine models from captured traces. The LLM generates XState statechart definitions for UI workflows and Quint specifications for behavioral invariants, using grammar-constrained generation and Apalache verification in a feedback loop. Active automata learning (L*/KV via AALpy) refines the passive models by querying the live system, using the LLM-generated alphabet abstraction. Alloy models capture structural data constraints discovered through Daikon-style invariant detection on API responses. Each model is validated against the live system before proceeding.

**Phase 3: Test generation and code synthesis.** Schemathesis generates API conformance tests from the OpenAPI spec. Hypothesis `RuleBasedStateMachine` classes encode the learned Mealy machines as executable test generators. AltWalker walks XState-derived graph models for UI coverage testing. Simultaneously, the LLM generates implementation code constrained by: OpenAPI schemas (data types), XState machines (control flow), Quint invariants (behavioral properties), and `deal`/`icontract` decorators (runtime contracts). Grammar-constrained generation and optional Dafny verification provide quality gates.

**Phase 4: Conformance checking loop.** The agent runs the full test suite against the clone, using **three oracle strategies** in parallel: differential testing against the original SaaS (strongest), model-based testing against the formal specification (ioco-style), and schema conformance against the OpenAPI spec. Failures are shrunk to minimal counterexamples (Hypothesis's built-in shrinking), analyzed by the LLM to identify implementation gaps, and fed back into code generation. The loop continues until the test suite passes—which, given the formal model's coverage guarantees, approximates feature parity.

**The right level of formalism** is deliberately heterogeneous. XState provides immediate executability with moderate formal rigor. Quint/TLA+ provides strong temporal guarantees where they matter (concurrency, consistency, ordering). Alloy catches structural violations. Design by Contract provides runtime guardrails throughout. No single formalism handles everything, but the combination covers SaaS behavior comprehensively while staying within what LLMs can reliably generate.

---

## Tool readiness and recommended stack

| Tool | Role | Language | Status | LLM-Friendliness |
|------|------|----------|--------|-------------------|
| **XState v5** | UI workflow modeling + MBT | TypeScript | Very active (v5.30) | Excellent |
| **Quint** | Temporal invariants, model checking | TypeScript-like | Active (Informal Systems) | High (LLM Kit exists) |
| **Apalache** | Symbolic model checking, conformance testing | Scala/Java | Active (JSON-RPC API, 2025) | N/A (tool, not authored) |
| **Alloy 6** | Structural constraints, test data generation | Alloy | Active (v6.2.0, Jan 2025) | Moderate-High |
| **AALpy** | Automata learning (active + passive) | Python | Active (v1.5.3, Dec 2025) | N/A (library) |
| **LearnLib/ALEX** | Advanced automata learning, web app learning | Java | Active (CAV 2025) | N/A (library) |
| **Schemathesis** | API conformance testing from OpenAPI | Python | Very active | N/A (tool) |
| **Hypothesis** | Property-based + stateful testing | Python | Very active (v6.151+) | N/A (library) |
| **AltWalker** | Graph-based UI MBT (wraps GraphWalker) | Python | Active | N/A (tool) |
| **deal** | Design by Contract for Python | Python | Stable (since 2018) | High |
| **icontract-hypothesis** | Contracts → PBT bridge | Python | Active | High |
| **Daikon** | Dynamic invariant detection | Java | Active (v5.8.23, June 2025) | N/A (tool) |
| **nl2spec** | NL → temporal logic | Python | Research (CAV 2023) | N/A (tool) |
| **itemis CREATE** | Statechart → code generation | Multi | Active (commercial+OSS) | N/A (tool) |
| **Dafny** | Verification-aware code generation | Dafny | Active (Microsoft) | Moderate-High |

## Conclusion

The convergence of three trends makes this methodology viable now in a way it wasn't even two years ago. First, **LLM+formal methods integration** has crossed a usability threshold: grammar-constrained generation eliminates syntactic errors, verifier-in-the-loop patterns catch semantic errors, and fine-tuning on formal data (FM-ALPACA) provides ~3x performance gains. Second, **automata learning tools** have matured into production-quality Python libraries (AALpy v1.5.3) with web-application-specific tooling (ALEX). Third, **property-based conformance testing** (Schemathesis, Hypothesis stateful) has achieved mainstream adoption with zero-configuration API testing from inferred schemas.

The critical architectural decision is rejecting any single "best" formalism in favor of a layered stack matched to abstraction levels. The agent should not try to write a complete TLA+ specification of a SaaS application—that exceeds both LLM capability and practical necessity. Instead, it should generate XState machines for what's directly observable, Quint specs for the handful of critical temporal invariants, Alloy models for structural constraints, and Design-by-Contract decorators everywhere else. The formal models collectively define "ground truth" for ioco-style conformance testing, while the original application provides differential oracle backup. This is not full formal verification—it is *lightweight formal methods applied at scale by an autonomous agent*, which is a fundamentally new capability enabled by the LLM+formal methods convergence of 2024–2026.