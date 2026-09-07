# CLAUDE.md — Business Context Layer Demo

## Purpose and scope

This is a runnable demonstration of the **Business Context Layer** from the Enterprise Agentic Architecture design. It shows one complete vertical slice: how raw data from heterogeneous Systems of Record is assembled, cached, governed, and served as clean canonical context to agents.

The demo covers the **customer product holdings** journey end-to-end: CRM identity + Billing subscriptions → CDC assembly → Context Cache → ACG retrieval → agent reads and governed writes via the Action Broker.

It is a **demonstration**, not a production system. Design gaps are documented explicitly in the `DIVERGENCES FROM DESIGN` section below and in `README.md`. Do not paper over those gaps — the goal is to evolve toward the design incrementally, with each gap clearly understood before closing it.

---

## Why key decisions were made

### The ontology is a Python module, not a service

`ontology/ontology.py` is imported directly by CDC, the Cache, and the Action Broker. It is not a REST service. This choice was deliberate:

- Field mapping, schema validation, and write route declarations are hot-path logic. A service call on every cache write would add latency and create a dependency that must be healthy before CDC can process any event.
- The ontology changes infrequently and is owned by a small team. Tight coupling to it is acceptable; loose coupling would add machinery without benefit.
- Testing ontology logic in isolation (unit tests against `commercial_rules`, `validate_cache_record`, etc.) is simpler when the module is importable without starting a service.

If the ontology needed to be independently versioned or consumed by teams that cannot import Python, a REST service would be appropriate. That threshold has not been reached.

### CDC uses a stateful in-process pending store, not a message bus

The pending join state (`_pending` dict in `cdc_app.py`) lives in process memory rather than a durable queue. Reasons:

- The demo has a single CDC instance. A distributed store would be needed only once CDC is horizontally scaled, which is not a demo requirement.
- Keeping the state in-process makes the join logic observable via `GET /pending` without any external dependencies.
- The design describes a Complex Event Processing join — the in-process implementation is the simplest faithful representation of that concept.

The consequence: if the CDC process dies, pending assemblies are lost and affected customers will have partial or missing cache records. This is acceptable for a demo; in production, the pending state would be in Redis or a durable event store.

### The Context Cache is an in-memory dict, not Redis + Cosmos DB

The two-tier structure (`_store` for hot cache, `_permanent` for the permanent index) is modelled structurally to match the design (Redis + Cosmos DB), but both tiers are plain Python dicts with no eviction, no TTL, and no persistence. Reasons:

- The demo runs on a single machine. Introducing a Redis dependency would gate the demo on external infrastructure and obscure the business logic.
- The structural distinction between `_store` and `_permanent` is preserved so that the ACG's two-tier read pattern (hot → permanent fallback) is demonstrable without Redis.
- Eviction and promotion policies are not the focus of this demo slice.

### Field groups carry `_meta` at write time rather than deriving freshness from the ontology at read time

Each field group written to the cache includes a `_meta` block with `assembled_at`, `ttl_seconds`, and `consistency_class`. These values are embedded by `assemble_domain()` at write time (sourced from `FIELD_GROUPS` in the ontology) rather than being looked up from the ontology at read time.

Reason: the ACG can evaluate freshness (`_with_freshness()`) without making a request to another service or re-importing the ontology. The freshness signal is self-contained in the cache record.

### The Action Broker governs all writes; direct SoR writes are not permitted in the architecture

Agents (and demo scripts) must submit write intents to the Action Broker's `POST /submit-intent` endpoint. The SoR endpoints exist and are reachable, but calling them directly bypasses the permission check, payload validation, and audit trail.

The teardown in the integration test `restore_state` fixture does call the Billing SoR's `PATCH /customers/{id}` directly — this is an intentional exception because test teardown needs to restore state reliably without triggering the full governance chain. This is documented in the test and should not be cited as a precedent for skipping the Action Broker in application code.

### JWT auth uses HS256 with a shared secret

`auth.py` uses HMAC-SHA256 with a hardcoded shared secret. This was chosen because:

- It requires no external infrastructure (no OIDC provider, no key management service).
- All services can verify tokens without a network call.

In production, this must be replaced with asymmetric keys (RS256 or ES256) issued by an authorisation server. The shared secret is not a secret — it is committed to source control and is intended only to demonstrate the JWT flow.

### The retrieval plan is implemented as a pydantic-graph

The ACG's retrieval plan uses `pydantic-graph` (`Graph`, `BaseNode`) rather than a plain chain of `if/else` statements. Reasons:

- The same graph object drives execution, Mermaid diagram generation (`GET /retrieval-plan/mermaid`), and REST introspection (`GET /retrieval-plan`). There is no separate description to maintain.
- Edges between nodes are derived from return type annotations — adding a new node and declaring its possible next steps in the return type is enough to register it in the graph.
- The MCP tool schema is generated from the same graph description.

This is not over-engineering for this use case: the retrieval plan is a genuine graph with branching (cache hit → skip permanent read), and pydantic-graph makes that structure inspectable without a separate diagram.

### Commercial rules are YAML-driven, not hardcoded

Discount policies (`rules/discounts.yaml`) and products (`product/catalogue.yaml`) are YAML files loaded at startup (and hot-reloadable via `POST /reloads` on the Offer Engine). Rules live in YAML rather than Python because:

- Business teams own the discount rules, not engineers. YAML is editable without code changes.
- The rule engine (`commercial_rules.py`) is intentionally policy-agnostic — it evaluates any policy matching the declared schema. Adding a new discount requires adding a YAML entry, not changing the engine.
- Hot-reload means rule changes can be tested against live context without restarting services.

---

## SDLC rules

### Test validation

**Always run the test suite after any change.** Do not consider a task complete until tests pass.

```bash
# From demo/ — runs all tests that don't require services
cd demo
pytest tests/rules/          # unit tests — no services needed

# Integration tests require all services to be running first
./start.sh
pytest tests/                # runs both unit and integration tests
./stop.sh
```

**Test categories:**

| Category | Location | Requires services | What it covers |
|----------|----------|-------------------|----------------|
| Unit | `tests/rules/` | No | Discount rule evaluation, policy stacking, edge cases |
| Integration | `tests/test_pricing_journey.py` | Yes (all) | End-to-end Action Broker flows, permission denials, audit log, CDC propagation |

**When to add tests:**

- Every new discount policy in `discounts.yaml` must have a corresponding test in `tests/rules/test_discounts.py`. The test must cover the positive case, the boundary case (value just below threshold), and any interaction with other policies.
- Every new write intent in `WRITE_ROUTES` must have integration test coverage for: the permitted case (correct caller, valid payload), a permission denial (wrong caller), and a schema violation (missing required field).
- Pure functions (e.g. `evaluate_discounts`, `validate_cache_record`, `assemble_domain`) must have unit tests that can run without any service running.

**Rules for integration tests:**

- All state mutations in integration tests must be fully restored in `teardown` / `autouse` fixtures. Tests must be idempotent — they must leave the system in exactly the state they found it.
- Use module-scoped fixtures (`scope="module"`) to snapshot state before the test suite runs. Do not snapshot inside individual tests — if a test fails, teardown still needs the original state.
- Skip gracefully when services are down: check service health at module collection time and use `pytestmark = pytest.mark.skipif(...)`. Do not let integration tests fail with connection errors when services are simply not running.
- Tests that depend on CDC propagation settling (event → cache update) must sleep explicitly (`time.sleep(1.5)`) and include a comment explaining why. Do not remove these sleeps — they are not flakiness, they are propagation waits.

**Rules for unit tests:**

- Unit tests must not import `httpx`, make network calls, or depend on any service being up.
- Fixtures in `conftest.py` that represent customer context must mirror the post-assembly canonical format produced by CDC (fields named `product_id`, `status`, `list_price_gbp`, `monthly_charge_gbp`, `contract_term_months`). If the canonical format changes in the ontology, update the fixtures to match.
- Each fixture docstring must state the customer, products, status, list price, contracted price, term, and the active list total. This is the primary documentation of what the fixture represents.

---

### Separation of concerns

#### The ontology is the authority — do not duplicate it elsewhere

`ontology/ontology.py` is the sole place for:
- Field mapping declarations (`FieldMapping`, `_CRM_MAPPINGS`, `_BILLING_LINE_MAPPINGS`, `_CATALOGUE_MAPPINGS`)
- Field group declarations (`FIELD_GROUPS`)
- Assembly rules (`ASSEMBLY_SPEC`)
- Valid assembly states (`VALID_ASSEMBLY_STATES`)
- Write route declarations (`WRITE_ROUTES`)
- Agent permission declarations (`AGENT_PERMISSIONS`)
- Cache schema validation (`validate_cache_record`)

Do not add field names, status codes, or domain logic to `cdc_app.py`, `acg_app.py`, or `action_broker_app.py`. If a new SoR field needs mapping, add a `FieldMapping` to the appropriate list in `ontology.py`. If a new write intent is needed, add it to `WRITE_ROUTES` and `AGENT_PERMISSIONS`.

The `STATUS_CODES` dict (`A → active`, `S → suspended`, `C → cancelled`) is declared in `ontology.py`. `billing_app.py` has its own `_STAT_CODES` copy because the Billing SoR is in the Systems of Record layer and cannot import the ontology (it owns the raw codes, not the canonical values). This is an acceptable exception — it is not a precedent for duplicating ontology knowledge in Business Context or Agentic layer services.

#### The CDC assembly service is the only writer to the Context Cache

Only `cdc_app.py` (and its enrichment helpers) should call `PUT /records/{id}` on the Context Cache. The ACG is a pure reader — it never writes to the cache or publishes events to CDC. If you find yourself adding a cache write to `acg_app.py`, stop and reconsider the design.

The exception is the ACG's legacy cache-miss assembly path (which was removed in the current version). The current ACG design is: cache miss → read permanent store → return partial or 404. The ACG does not trigger on-demand assembly.

#### Each service has one responsibility

| Service | Responsibility | Not its responsibility |
|---------|---------------|----------------------|
| CRM SoR | Owns customer identity data; emits `crm.customer.updated` events | Assembling canonical records |
| Billing SoR | Owns subscription state; emits `billing.subscription.updated`; evaluates discount rules at point of sale | Caching, entity resolution |
| Product Catalogue SoR | Owns product definitions and prices; emits `product.catalogue.updated` | Billing calculations |
| CDC Assembly | Receives events, applies ontology mappings, manages join state, writes to cache | Serving context to agents; making business decisions |
| Context Cache | Stores and retrieves canonical records | Assembling records; evaluating freshness |
| ACG | Reads context and serves it to agents; evaluates freshness at read time | Writing to cache; publishing events |
| Offer Engine | Evaluates discount policies; computes offer proposals | Storing results; reading customer state directly |
| Action Broker | Governs write intents: auth, permissions, routing, validation, audit | Executing business logic; making write decisions |
| Logging Service | Stores structured log entries; provides trace reconstruction | Reacting to log content |

#### The Offer Engine does not read customer state directly

The Offer Engine is stateless — it accepts context packets in its request body rather than fetching customer data itself. This is deliberate: the Offer Engine can evaluate any hypothetical portfolio, not just live customer state. The ACG passes the assembled context to the Offer Engine; the Offer Engine never calls the ACG or the Cache.

#### Logging is fire-and-forget — it must never interrupt the assembly path

Every `await log_http.post(...)` call is wrapped in `try/except Exception: pass`. This pattern must be preserved. If the Logging Service is down, events must still be assembled and written to the cache. Never remove the `try/except` from a log call.

---

### Documentation accuracy

**When code changes, documentation must change in the same commit.** The following documents must be kept in sync with the code:

| Document | What it must reflect |
|----------|---------------------|
| `README.md` (this folder) | Service ports, file paths, example journeys, sequence diagrams, design gap table |
| `CLAUDE.md` (this file) | Architectural decisions, SDLC rules, design divergences |
| `rules/discounts.yaml` comments | Rule scope semantics if the engine gains new scope types |
| `product/catalogue.yaml` comments | Relationship types if new types are added |

**Specific sync rules:**

- If a new service is added, update the service table in `README.md` and `start.sh`; add a port comment to `start.sh`.
- If a write intent is added to `WRITE_ROUTES`, update the write route table in `README.md` and add integration test coverage.
- If a new rule scope is added to `commercial_rules.py`, document it in `rules/discounts.yaml`'s comment header.
- If a design gap is closed, remove it from the divergences section of `CLAUDE.md` and the gap table in `README.md`.
- If a new design gap is introduced intentionally (i.e. the code diverges further from the design), add it to both documents with an explanation of why.
- If `ASSEMBLY_SPEC` changes (new event type, new field group), update the sequence diagrams in `README.md`.

---

### Port and service conventions

Ports are assigned by architectural layer:

| Range | Layer |
|-------|-------|
| 8010–8014 | Systems of Record |
| 8015 | Cross-cutting (logging) |
| 8016–8019 | Business Context + Agentic |

When adding a new service:
1. Assign the next free port in the appropriate range.
2. Add a startup block to `start.sh` in dependency order with a `--log-level warning` flag.
3. Add a PID file (`echo $! > "$PID_DIR/.pid_<name>"`).
4. Add the PID file to `stop.sh`.
5. Add the service to the `seed.py` health-check loop if it must be ready before seeding.
6. Update the service table in `README.md`.

---

### Import rules

All services run in their own subdirectory (`cd "$SCRIPT_DIR/service_name"`) with `PYTHONPATH` set to include the demo root, `ontology/`, and `rules/`. Shared modules are:

| Module | Location | Who imports it |
|--------|----------|----------------|
| `ontology` | `ontology/ontology.py` | CDC, Cache, ACG, Action Broker |
| `commercial_rules` | `rules/commercial_rules.py` | Billing SoR, Offer Engine |
| `auth` | `auth.py` | ACG, Action Broker |

Do not create circular imports. The dependency direction is:

```
SoR layer → CDC → Cache
                ↗
commercial_rules (Billing SoR, Offer Engine)
auth (ACG, Action Broker)
ontology (CDC, Cache, ACG, Action Broker)
```

---

## Invariants — never break these

1. **`ontology.py` is the authority.** Field mappings, assembly rules, write routes, and permissions belong there. `cdc_app.py` and `action_broker_app.py` contain no domain knowledge.

2. **Only CDC writes to the cache.** `cache_app.py` is the only service that may write to `_store` or `_permanent`. Nothing calls `PUT /records/{id}` except `cdc_app.py` and its enrichment helpers.

3. **The `_merge_domain` sentinel is stripped before storing.** `cache_app.py` calls `body.pop("_merge_domain", None)` before any storage. This sentinel must never appear in a stored record or in an API response.

4. **Assembly states are a closed set derived from `ASSEMBLY_SPEC`.** `VALID_ASSEMBLY_STATES` in `ontology.py` is computed from `REQUIRED_DOMAINS` — currently `complete`, `awaiting_billing`, `awaiting_customer`, `partial_timed_out`. Adding a new `EventType` to `ASSEMBLY_SPEC` automatically registers its `awaiting_<domain>` state. The UI badge renderer in `acg/ui/index.html` must be updated manually when new domains are added.

5. **Logging failures are silent.** Every log call is wrapped in `try/except`. This is not laziness — logging failure must never interrupt the assembly or retrieval path.

6. **The Offer Engine is stateless.** It accepts context in the request body. It never calls the ACG or the Cache to fetch customer state.

7. **The ACG is a pure reader.** It never calls `PUT /records/{id}` on the cache and never publishes events to CDC.

8. **The Action Broker derives `caller_id` from the verified JWT, not from the request body.** The request body's `intent`, `customer_id`, and `payload` fields are trusted; the caller's identity is not. This is asserted in `submit_intent` and must not be changed.

9. **Tests are idempotent.** Integration tests must restore all mutated state in teardown. A test run must leave the system in the same state it was in before the run.

10. **New discount policies require new tests.** A policy entry in `discounts.yaml` without a corresponding test is incomplete work.

---

## Divergences from the design

These are **known and intentional** scope limitations. When a gap is closed, remove its entry here and from `README.md`. When new work introduces a new gap, add it here with a clear explanation.

### 1. Entity resolution is absent — canonical ID equals SoR key

**Why the demo is simpler:** Both mocked SoRs (CRM and Billing) share the same key space (`C001`–`C005`). There is no need to resolve SoR-local identifiers to canonical IDs because there is no identifier conflict.

**What the design requires:** A resolution service rewrites SoR-local identifiers (e.g. SAP `BP-4500067890`, Salesforce `ACC-00123`) to canonical entity IDs using a Golden Record Table before events reach the event hub. The resolution service is independent of CDC.

**Impact of this gap:** Any third SoR using a different key scheme cannot join correctly without this layer. Adding ITSM (incident IDs), for example, requires entity resolution before this can work.

**Recommended next step:** Add a `resolve_identity(sor_key, source_system) → canonical_id` stub to `ontology.py` that initially returns the input unchanged. This creates the seam for a real implementation without breaking current behaviour.

---

### 2. Cache documents are flat — field groups exist but version vector is a plain integer

**Why the demo is simpler:** The cache uses a plain integer `version` counter incremented on each write. There are no ETags and no per-SoR version tracking.

**What the design requires:** Every cache document carries a `versionVector` dict mapping each SoR's contribution to its ETag at last assembly. Agents carry the version vector forward in write intents. The Action Broker submits it as an `If-Match` conditional write header to the SoR API, which returns 412 Precondition Failed on a conflict.

**Impact of this gap:** The concurrency protection described in the design (optimistic locking on SoR writes) cannot be implemented. Any write intent submitted via the Action Broker is unconditional.

---

### 3. No write path at the ACG — `submitIntent` is absent

**What the design requires:** The ACG exposes two operations: `getContext` (reads) and `submitIntent` (writes). Write intents pass through the ACG before reaching the Action Broker.

**Why the demo is different:** The demo has agents call the Action Broker's `POST /submit-intent` directly. The ACG's role in the write path (intent routing, context enrichment before write) is not implemented.

**Impact of this gap:** Agents that call `submitIntent` via MCP would expect to call the ACG, not the Action Broker. In the demo, MCP clients that need to write must call the Action Broker directly.

---

### 4. Context Cache has no tier policy — hot and permanent tiers always in sync

**What the design requires:** A Tier Policy Engine with four components (Tier Policy Rules, CEP Evaluator, Tier Demotion Handler, Evaluation Queue Writer) governs promotion from the permanent store to the hot cache and demotion when customers are inactive. Tier 2/3 has a 72-hour sliding TTL.

**Why the demo is simpler:** Both tiers are populated on every CDC write. The hot cache never evicts. The distinction between tiers is structural — it demonstrates the two-tier read pattern in the ACG without requiring a tier policy.

---

### 5. Intelligence layer has one read surface, not three

**What the design requires:** The ACG fires three stores in parallel: Context Cache (structured state), Vector Stores (semantic indexes for policy, product, and interaction history), and Graph Stores (cross-domain semantic relationship edges). Results are streamed as progressive context packets in arrival order.

**Why the demo is simpler:** The ACG reads only the Context Cache. There are no Vector Stores, no Graph Stores, no embedding pipelines, and no streaming. The retrieval plan is synchronous.

---

### 6. `timeout_action = "discard"` is dead code

`EventType` in `ontology.py` declares a `timeout_action` field with two possible values: `"write_partial"` and `"discard"`. All event types in `ASSEMBLY_SPEC` declare `"write_partial"`. The discard branch in `_timeout_checker` (`cdc_app.py`) is never exercised.

**What to do:** Either add an event type with `timeout_action = "discard"` and write a test for it, or remove the field from `EventType` if the discard behaviour is not needed in this demo scope.

---

### 7. No Model Gateway or guardrails

**What the design requires:** All model invocations pass through a Model Gateway with runtime Guardrails enforcing content policy on inputs and filtering outputs.

**Why absent:** The demo does not invoke any LLM. The ACG returns structured JSON context packets; agents consume them without model inference in the demo.

---

### 8. Multi-SoR join is hardcoded to two domains

The join logic in `cdc_app.py` checks `required = {"customer", "billing"}`. This is hardcoded — extending to a third SoR requires changing both `ASSEMBLY_SPEC` in `ontology.py` and the `required` set in `cdc_app.py`.

**What the design requires:** The required domain set should be derived from `ASSEMBLY_SPEC` for the event type, making the join logic data-driven. This is a straightforward refactor but has not been done because the demo only needs the two-domain join.

---

## Recommended evolution order

When evolving this demo toward the full design, prioritise in this order:

1. **Make the required domain set data-driven in CDC** — low-risk refactor that closes gap 8 and enables a third SoR to be added without touching the join logic.
2. **ETag-based version vector** — adds a version string to every cache record; Action Broker forwards it as `If-Match` on SoR writes. Closes gap 2.
3. **Entity resolution stub** — adds `resolve_identity()` to the ontology with a passthrough implementation. Creates the seam for gap 1 without breaking anything.
4. **`submitIntent` on the ACG** — routes write intents through the ACG before the Action Broker, closing gap 3 and enabling MCP-based write governance.
5. **Tier policy with TTL eviction** — adds TTL eviction to the hot cache and a simple promotion rule. Closes gap 4.
6. **Retrieval plans declared in the ontology** — moves the ACG's hardcoded `RETRIEVAL_PLAN` into `ontology.py`, making the retrieval strategy data-driven. Enables per-task-type retrieval strategies.
