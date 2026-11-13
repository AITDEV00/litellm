# MCP Source Broker: design logic map and implementation plan

Status: proposed, no implementation or release gate has run
Baseline: upstream LiteLLM **v1.108.5 LTS**, release commit `2a61f21`
Pattern: **modular vertical slice**, as required by [the OICM slice pattern](../oicm-slices.md)
Method: [Trace-Through-Before-You-Build](../techniques/logic_mapping_technique.md)
Source flow: the Nginx reverse-tunnel pilot `LiteLLM-Reverse-Tunnel-Nginx-Pilot.md`, committed alongside this proposal in the same directory

## 1. Goal and design decisions

A source host should call one gateway endpoint, receive an installer, run it as a one-shot privileged enrollment action, and have its connector return at every boot. The gateway should expose the resulting private HTTP, OpenAPI, or MCP services as one stable namespace per logical data source. Independent source hosts for that same data source should provide failover and request distribution rather than a pile of duplicate MCP tool names.

The feature combines four existing mechanisms:

1. Chisel `1.12.0`, the client already proven by the pilot, makes the outbound TLS WebSocket through the existing HTTPS ingress. The pinned official release supplies Linux AMD64 and ARM64 `.gz` binaries. macOS ARM64 and Windows AMD64 are deferred targets, not first-class server targets in this proposal
2. The gateway's existing `/install` artifact slice and RustFS mirror serve versioned archives and SHA-256 hashes, so no bootstrap binary needs to live in Postgres
3. Database-backed MCP registration in LiteLLM `1.108.5` persists configuration across a gateway restart. Existing `MCPServerManager` code owns MCP authorization, admission, guardrails, namespace prefixing, and dispatch
4. A broker-owned private HTTP reverse proxy chooses among healthy pool members. It is the **upstream endpoint router and capacity pool** for one source, positioned before the public MCP aggregator. The standard HTTP proxy pattern is **reverse proxy**, not an in-protocol load-balancing change to LiteLLM's MCP manager

The scope is explicit:

- The first supported source host is Linux AMD64 or ARM64 with systemd, `curl`, a trusted CA store, and `sha256sum`. The service script runs under a normal user, never root
- A pod-hosted **Chisel relay**, generated credential store, and resource metadata tables make enrollment and gateway restarts durable
- **Do not let each Pod choose its own listeners.** Allocate from the reviewed `127.0.0.20` through `127.0.0.254` range and ports `18080` through `18120`, persist the member's exact `(relay_host, relay_port)` lease, and restore that lease before the relay becomes ready. Replicas do not invent local allocations or per-source URLs
- One data source is registered once in LiteLLM. Clients use the existing `https://<gateway>/mcp` aggregate endpoint, or `https://<gateway>/<source-alias>/mcp` for a specific data source, not a Chisel address
- **A pooled endpoint is not proof of sameness.** Group by a logical data-source identity declared at enrollment, then verify a compatible catalog. Hostnames, IP addresses, `available_on_public_internet`, shared MCP aliases, `mcp_access_groups`, and access to "the internet" are not data-source IDs
- **Capacity means multiple independent working paths**, such as Server A in an AD site, Server B on another network boundary, and Server C through an egress proxy. For one-host capacity, `pool.py` limits task concurrency for each fixed upstream endpoint and its HTTP client reuses bounded backend connections. This is separate from source-member pooling. The broker dials its relay's loopback listener directly, not through the Kubernetes service proxy. The first product target is redundant source hosts; prove per-upstream connection behavior under concurrent load in Phase 3

The pilot's OpenAPI `1.108.6` verification is useful implementation evidence, not a release claim for this proposal. All baseline MCP behavior and anchors below use upstream `1.108.5` at `2a61f21`. The pilot must be repeated on `1.108.5` before a production rollout.

## 2. Trace: existing request and data flows

### 2.1 What already works

The pilot already proves these boundary crossings with the stock Chisel client:

```text
Gateway or broker probe                         Source host A
        |                                              |
        |  TCP to broker-private listener:17001         | user-installed Nginx on 18080
        |------------------- Chisel relay --------------| chisel client --fingerprint ...
        |<========= outbound TLS WebSocket over =========|
                      existing ingress /mcp-worker
```

No new public inbound connection is opened to A. The relay listens only in its own Pod. A's outbound HTTPS reaches the ingress; the reverse listener carries gateway-side traffic over A's WebSocket and then to A's loopback HTTP service.

### 2.2 MCP registration flow in v1.108.5

YAML startup goes through [`_init_non_llm_configs()`](https://github.com/BerriAI/litellm/blob/v1.108.5/litellm/proxy/proxy_server.py#L7215-L7225) into [`MCPServerManager.load_servers_from_config()`](https://github.com/BerriAI/litellm/blob/v1.108.5/litellm/proxy/_experimental/mcp_server/mcp_server_manager.py#L2523-L2864). Database startup uses [`reload_servers_from_database()`](https://github.com/BerriAI/litellm/blob/v1.108.5/litellm/proxy/_experimental/mcp_server/mcp_server_manager.py#L7161-L7165), whose catalog reload builds changed records and publishes the registry. Config and DB records are distinct registry sources.

Admin creation is `POST /v1/mcp/server` -> [`NewMCPServerRequest`](https://github.com/BerriAI/litellm/blob/v1.108.5/litellm/proxy/_types.py#L1367-L1415) -> `create_mcp_server_if_identifier_free()` -> [`add_server()`](https://github.com/BerriAI/litellm/blob/v1.108.5/litellm/proxy/_experimental/mcp_server/mcp_server_manager.py#L3435-L3475) -> `reload_servers_from_database()`.

```text
Admin or broker service
       |
       v
POST /v1/mcp/server, stable server_id + alias + private router URL
       |                                    |--> Postgres --> gateway restart reload
       v                                    |
NewMCPServerRequest -> create DB row        |
       |                                    |
       +--> add_server() --> build_mcp_server_from_table()
                            |--> registry[server_id]
                            |--> optional _register_openapi_tools()
```

The broker should persist one such record through a small shared adapter, not put a fake HTTP request through the admin management route. The adapter borrows the existing DB create/update functions and the runtime `add_server()` / `update_server()` methods so error handling and registry refreshes are not duplicated.

### 2.3 Native MCP and OpenAPI execution differ

```text
User's MCP client                         User's HTTP/LLM client
        |                                         |
        | /mcp or /source-alias/mcp                | existing LiteLLM tool use
        v                                         v
named handlers / REST tools/list or tools/call  -> normal gateway authentication and grants
        |                                         |
        +------> existing MCPServerManager -------+
                          | one logical server, stable namespace
                          |
                 private broker source router
                          |
                  choose healthy member*
                          |
                 Chisel relay sidecar, reverse listener
                          |
               source-host user service: local HTTP or MCP
```

For a native remote MCP server, [`create_mcp_client()`](https://github.com/BerriAI/litellm/blob/v1.108.5/litellm/proxy/_experimental/mcp_server/mcp_server_manager.py#L4325-L4369) delegates to `prepare_upstream_client()`, which constructs the HTTP or SSE MCP transport. That traffic can point at the private broker URL as an ordinary HTTP upstream.

For OpenAPI tools, [`_register_openapi_tools()`](https://github.com/BerriAI/litellm/blob/v1.108.5/litellm/proxy/_experimental/mcp_server/mcp_server_manager.py#L2878-L3019) parses the specification and stores direct HTTP handlers. [`create_tool_function()`](https://github.com/BerriAI/litellm/blob/v1.108.5/litellm/proxy/_experimental/mcp_server/openapi_to_mcp_generator.py#L502-L628) binds its `base_url` **at registration time**. On `1.108.5`, these handlers do not later consult a dynamic per-request member selector. Setting the registered URL to the broker's stable private address solves this without modifying upstream's OpenAPI closure semantics.

`tools/list` and `tools/call` end in the manager's native or OpenAPI branches. `access_groups` grants access. Duplicate alias/access-group entries can widen name resolution under some paths but do not independently create a load balancer. `_resolve_mcp_server_for_tool_call()` resolves one server before calling, so cloning three server rows with the same alias would still produce namespace and routing collisions

### 2.4 Client setup is separate from the source-host connector

| Host role | Installed client | What it does |
|---|---|---|
| Data-source host | Official Chisel `v1.12.0` as a system service | Owns the outbound reverse forward for a fixed local listener |
| Source host that runs no service application | Chisel plus the lightweight loopback HTTP/MCP adapter explicitly enrolled by an admin | Bridges to a reachable local or remote target without forwarding the whole host's network |
| Developer laptop or HTTP client | No Chisel | Calls the gateway's `/mcp` or inference API with the user's normal virtual key |
| Developer laptop using the existing coding-agent installer | Existing cc-switch-cli `jya0-5.11.1`, with the small noninteractive MCP addition below | Configures a supported harness to call the gateway instead of a remote MCP URL plus a separately copied header |

The user never needs root to call the gateway's MCP endpoint. Persistent connector installation requires root only once, to write the dedicated service account and enable `oicm-mcp-connector.service`. A `curl | sh` bootstrap is convenient but **cannot finish installing an unprivileged connector when run as an unprivileged user**. That limitation should be explicit in output rather than a failed background root service

## 3. Build: exact implementation slices

Keep the feature in one feature slice plus a tiny upstream delegation seam. Do not scatter enrollment logic through `proxy_server.py`, `mcp_management_endpoints.py`, or `mcp_server_manager.py`.

### 3.1 NEW: gateway feature slice

Create `litellm/proxy/mcp_source_broker/`:

| Exact file | Responsibility and concrete implementation |
|---|---|
| `__init__.py` | Export the control `AdminRouter` and artifact-install renderer for gateway mounting. Do not initialize the worker, relay, or network activity on import |
| `schema.py` | Frozen Pydantic request/response models for `SourceEnrollment`, `ConnectorBundle`, `PoolMember`, `SourceCatalog`, `InventoryEntry`, `HealthVerdict`, and the split credential views. Bounded strings, typed ports, fixed transport enums, and source allowlist policies. Secret value returned only by the mint action's response |
| `store.py` | A `SourceStore` protocol plus PostgreSQL implementation using Prisma's `execute_raw`, `query_raw`, and typed row adapters. Inserts sources only after probe success; handles optimistic `version` updates and `If-Match`; returns typed conflict/not-found/discriminators instead of scattered HTTP exceptions |
| `enrollment.py` | Internal operator identity and gateway-client dependency injection for create, update, delete, and secret rotation. Normalizes input, classifies candidates, reserves allocations, reconciles records, and applies optimistic concurrency |
| `pool.py` | Immutable `DataSource` -> frozen `PoolSnapshot` snapshots. Filters disconnected members and selects by least observed in-flight requests with deterministic member-id tie-breaking. It never randomizes for its own sake and never selects a disabled member |
| `router.py` | Private ASGI/HTTP reverse proxy, bounded bidirectional streaming and headers, session/member affinity, per-member semaphores, and source-path resolution. It removes the gateway/broker hop credential, applies the separately reviewed source-auth mode, and never invents a source credential |
| `health.py` | Real member probes, catalog fingerprint collection, collision/quarantine transitions, and in-flight/latency reporting. It distinguishes TCP tunnel liveness from MCP compatibility and from upstream response status |
| `secrets.py` | Hashes the generated operator enrollment token so the gateway can issue it without retaining or returning it again. The broker binds its hash to one minted inventory credential. Bearer-class token generation occurs downstream and plaintext is returned exactly once. Stored HMAC-SHA256 digests use versioned Secret material, not the DB's general salt key |
| `mcp_registration.py` | Adapter over `create_mcp_server_if_identifier_free()` / `update_mcp_server()`, `MCPServerManager.add_server()` / `update_server()`, and `reload_servers_from_database()`. Pins `server_id` and alias instead of relying on LiteLLM's content-derived ID. Embeds no connector identity in user-visible MCP names. Separate from `secrets.py` because these are existing service interface functions, not crypto responsibilities |
| `routes.py` | Thin `AdminRouter` on gateway `/v1/mcp/source-broker/**` and installer `/mcp-source-broker/**`; `BrokerControlRouter` only on private `8089` exposes `/mcp-worker/v1/**`; source dispatch uses a separate app on `9090`. Public Chisel WebSocket `/mcp-worker` accepts no broker REST features. Every handler validates its own scope and source policy |
| `__main__.py` | Dedicated broker entry point with distinct worker, internal-control, pool-ingress, and health listeners. Lifespan injects all repositories/auth dependencies explicitly. The public ingress accepts only WebSocket upgrade requests on `/mcp-worker`, never REST `/mcp-worker/v1/**`. Service `8089` serves control API only to the gateway control adapter and is never exposed through ingress |
| `tests/` is not inside the slice | The normal test paths mirror the source layout, as required by repository test conventions |

Use **one source of truth for snapshot state**. The DB is authoritative for durable versioned records. Connector heartbeat envelopes carry monotonic revisions and are written to Redis for fast liveness, with asynchronous DB publication for recovery. Read Redis first when it is at least as new as the DB snapshot. On cache loss, read the DB and accept the member only when `now - durable_heartbeat_at < heartbeat_ttl`, using an explicit interval rather than an unbounded trust in a previous connection. `SourcePool` publishes immutable versioned snapshots. Revoked tokens and missing catalog agreement prevent selection, even if a cache entry still claims a connection

`DB persistence` protects connector registration, allocation, version, and graceful reconciliation. A broker-local Prisma connection cannot instantiate the LiteLLM app registry. Where `mcp_registration.py` needs application-level `PrismaClient` and existing manager functions, inject them from the actual gateway application. Its direct shared-DB use for durable source records is fine; MCP registry refresh/serialization remains a gateway-owned service concern rather than a concurrent independent registry

Use a typed **service credentials** contract for encrypted in-cluster hop authentication between gateway and broker, provisioned with explicit admission. Provide `ServiceCredentialStore` through constructor injection alongside the connector/inventory stores. It supplies a short-lived K8s ServiceAccount token or signed mTLS/service identity rather than a customer LiteLLM key. Parse the peer's independent authentication scheme explicitly and remove its hop credential before proxying a request through Chisel

MCP caller authorization evaluated by LiteLLM must not automatically become a source-application bearer. Retain it in server-side request context. For native gateway-to-broker MCP transport, use the platform's `MCPAuth.none` with its credential resolver/materializer injecting the gateway/broker **service** token only at egress. Confirm the exact path through the `1.108.5` `_get_auth_headers()` implementation before choosing its source-model field: `authentication_token` for a client-credentials hop, not an unverified empty-key fallback. Capture the resulting `Authorization: Bearer <service-token>` at the broker once for native and once for OpenAPI. Ordinary user virtual keys never cross this hop

Source-application auth is a different credential store and contract. Do **not** also write it into `MCPServer.authentication_token`, caller `Authorization`, or the source-side Chisel credential. `MCPAuth.none` naturally means no own source bearer once the hop credential is removed; this is the initially supported unauthenticated loopback source after admin target and catalog validation. For a source requiring a static bearer or basic auth, add a typed broker-local `SourceCredentialStore` with an exact `source_id` membership binding, inject the expected static source credential only after removing the hop bearer, and test that a client-supplied header cannot override it. Public `passthrough` of original user Authorization and upstream `client_credentials` OAuth as a second independent source credential are deferred, not implied by the same MCP `authentication_token` slot

If the `1.108.5` OpenAPI generator cannot emit `MCPAuth.none` plus the service hop without an upstream seam, add one narrow auth/header resolver delegation there, mirroring native `create_mcp_client()` rather than creating gateway `MCPServer.auth_type` metadata that behaves only in one protocol path. This seam is a gate to be implemented and tested, not silently marked as already working

A source-root attacker can change its stock client `REMOTE`, even if a systemd unit is immutable to the service account. A valid stock-Chisel listener ACL is therefore a narrow listener constraint plus a source-local bind policy, **not proof of the ultimate TCP target**. The implementation must not silently treat the legacy regex as end-to-end attestation. The first supported target is a reviewed loopback service identified by a fixed service-version contract and real catalog. Root on that host is within the service-operator trust boundary. Optional non-loopback TCP targets need a separately reviewed management-agent design, not a loose relay regex. Callers cannot request `R:socks`, interface wildcards, arbitrary destination hosts, or `--allow-deny` exemptions

The broker-facing traffic policy is explicit: `RelayAccessPolicy {listener: 127.0.0.0/8:18080-18120, source_local_addr: loopback-only, source_port: approved-port}` is persisted alongside each grant. Its immutable source-host manifest contains normal non-secret URL/fingerprint/source-port settings. The service account can only read that manifest and pass it to Chisel; it does not forge service parameters in the URL query. Admission tests at enrollment and heartbeat validate actual protocol and source application credentials, not mere internet reachability

See the isolated-service decision below for how code is packaged. The VSA still co-locates business modules in this directory, but only the thin control router is imported by `proxy_server.py`. Endpoint definition, worker loop, relay lifecycle, auth binding, and artifact orchestration are separate functions with testable inputs and outputs

### 3.2 NEW: dedicated broker deployment

The production gateway has two replicas and four Granian workers. Keeping one loopback listener per replica at an arbitrary fixed port would break MCP Sessions and would misroute ordinary traffic. Use an independent broker service instead.

**Decision gated by session behavior: isolated broker Service, one broker replica, and one worker for the MCP execution Deployment.** Split `mcp-runtime` from ordinary inference serving before enabling this feature. The former uses one LiteLLM process and handles public `/mcp` plus its MCP-specific REST routes. Ordinary inference stays on the existing replicated Deployment. Its existing HTTP-tool paths need distributed session migration or an explicit prefix routing gate, not a claim that this split solves them automatically. All `/mcp` and source-alias `/mcp` downstream traffic reaches one owning `mcp-runtime` Service; HTTP-only MCP REST calls must either go through that same Service/session-binding layer or exclude native stateful servers. This is a deployment condition, not a cosmetic affinity annotation.

The broker Pod has two runtime processes, Chisel relay and broker worker, sharing its network namespace. The ordinary LiteLLM services keep public MCP authorization and tool dispatch. The broker has no LiteLLM `MCPServerManager` registry. A thin gateway control adapter reaches the broker's authenticated internal control API, receives the approved logical source configuration, and persists it with `mcp_registration.py`. Each request still reaches the broker's pool after LiteLLM authentication, not through a parallel unauthenticated MCP dispatch path.

| Exact new file | Object and purpose |
|---|---|
| `oicm-litellm-layer/deploy/base/source-broker/deployment.yaml` | One broker Pod with `relay` and `broker` containers. Ports are separate and documented. One broker worker owns its member bindings. Replication before shared or routed state is implemented is forbidden |
| `oicm-litellm-layer/deploy/base/gateway/mcp-runtime-deployment.yaml` | One-worker `mcp-runtime` variant handling `/mcp` and source aliases. Its readiness, public MCP tooling cache tests, and configuration lifecycle use the unmodified `MCPServerManager`. Ordinary inference workers are not reduced by this feature |
| `oicm-litellm-layer/deploy/base/gateway/mcp-runtime-service.yaml` | Distinct service selector for the one-worker MCP execution lane. Ingress routes public MCP names here before gateway catch-all routing. Covers HTTP REST routes that can dispatch native named-session state, with explicit cascade checks |
| `oicm-litellm-layer/deploy/base/source-broker/service.yaml` | Splits public **worker port `8088`**, default-deny **control port `8089`**, **pool ingress port `9090`**, and **internal health/metrics port `9091`**. Only worker port `8088` is ingress visible. `8089`, `9090`, `9091` have explicit cluster NetworkPolicy, never public discovery URLs |
| `oicm-litellm-layer/deploy/base/source-broker/secrets.yaml` | Placeholder/template references for Chisel host-key, per-domain service-auth, and HMAC pepper Secret material. Real values are minted through the existing approved process. Mounted read-only, never returned in any API response |
| `oicm-litellm-layer/deploy/base/source-broker/networkpolicy.yaml` | Only the nginx ingress controller reaches `8088`. Only the gateway control adapter reaches `8089`. Only LiteLLM gateway workers reach `9090`. Only scrape endpoints reach `9091`. Reverse listener ports are bound loopback-only and are not in the Service |
| `oicm-litellm-layer/deploy/base/source-broker/kustomization.yaml` | Registers those objects in the isolated broker base |
| `oicm-litellm-layer/deploy/overlays/source-broker/kustomization.yaml` | Rendered dev configuration. Production has a corresponding explicit source-broker overlay entry; the plain `deploy/overlays/prod` payload does not silently create a tunnel deployment before the pilot is approved |

The gateway application simply has additional `MCPServer` records pointing at `http://mcp-source-broker.adeo-litellm.svc.cluster.local:9090/sources/<source-id>[/mcp]`. It persists and reloads these records with its existing registry. End-user authorization stays with the existing MCP auth and permission hooks. The broker pool ingress additionally validates a narrowly scoped inter-service credential so another workload in the cluster cannot call an arbitrary upstream without passing MCP admission first.

The existing `oicm-litellm-layer/docs/session-identity/IMPLEMENTATION-PLAN.md` notes that some `1.108.5` session-routing patch work was deferred. Do not rely on an intermediate proposal changing the baseline. The implementation gate is a real `Mcp-Session-Id` capture demonstrating the same owning MCP worker and member, or the explicit one-worker execution lane described above. An operator-configured `X-Litellm-Proxy-Session-Id` would be a separate trusted annotation by the router, not something exposed for clients to forge. Source UUID does not substitute for a downstream MCP session identity

**Pilot shortcut:** run Chisel and the broker in one network namespace beside one LiteLLM worker, just as the original pilot did. This validates the same protocols but does not claim a production multi-replica session design

### 3.3 EDIT: gateway application wiring

| Exact file | Change |
|---|---|
| `litellm/proxy/proxy_server.py` | Import and include `mcp_source_broker.routes.AdminRouter` once in the existing OICM block. Lifespan injects existing `global_mcp_server_manager`, `prisma_client`, and `proxy_logging_obj` into its gateway registration adapter. No second MCP manager, client_distribution router, or broker relay is started in this app |
| `litellm/proxy/management_endpoints/mcp_management_endpoints.py` | **No change needed.** `mcp_registration.py` runs in the gateway and receives the existing `global_mcp_server_manager`, LiteLLM `PrismaClient`, and `ProxyLogging` references through lifespan dependency injection, not import-time global construction. The broker internal API has no MCP registry and cannot initialize a second `MCPServerManager` |
| `litellm/proxy/_experimental/mcp_server/mcp_server_manager.py` | **No MCP load-balancing change.** The existing native `create_mcp_client()` path points to the broker URL. `1.108.5`'s behavior and tests remain authoritative |
| `litellm/proxy/_experimental/mcp_server/openapi_to_mcp_generator.py` | **No change needed for this design.** Its registration-time `base_url` closure remains the private broker endpoint. If an independently changing per-call route is wanted later, add one injection point in `_register_openapi_tools()`, not repeated spec parsing |
| `litellm/proxy/schema.prisma`, `litellm-proxy-extras/litellm_proxy_extras/schema.prisma`, and root `schema.prisma` | Add the same three OICM MCP-broker table definitions below. Keep them aligned mechanically |
| `litellm-proxy-extras/litellm_proxy_extras/migrations/<timestamp>_mcp_source_broker/migration.sql` | A schema-only migration that creates the three tables and indexes. No `UPDATE`, `DELETE`, `MERGE`, or `INSERT ... SELECT`. Review against the migration resolver's ordering rules |

`server_id` is a generated UUID fixed for a logical source. `alias`, DB grants, and a stable public MCP namespace are assigned only once. The connector/member `relay_port` is never used in public `MCPServer.url`, so changing a pod no longer means renaming a tool or invalidating its grant

### 3.4 EDIT: installation, artifact publication, and deployment manifests

| Exact file | Change |
|---|---|
| `litellm/proxy/client_distribution/routes.py` | Add only `GET /mcp-source-broker/install` ahead of the generic `/install/{path}` artifact proxy. It generates a **connector-host installer**, not the existing coding-agent provider script. Keep binary artifact reads at `/install/chisel/1.12.0/<target>.gz` |
| `litellm/proxy/client_distribution/generator.py` | Add a separate renderer for the connector installer and its bootstrap context. Do not mix Chisel installation into the interactive coding-agent template |
| `oicm-litellm-layer/examples/mcp-source-broker/install.sh.tmpl` | Reviewable source of truth for the rendered bootstrap: uname detection, CA/curl checks, enrollment handshake, artifact fetch, hash check, user/unit installation, and immediate health verification |
| `oicm-litellm-layer/examples/mcp-source-broker/oicm-mcp-connector.service` | Unprivileged unit template using `EnvironmentFile=<private-config-dir>/connector.env`, `Restart=always`, `RestartSec=5`, and no download path at boot. The service runs in the dedicated user's home, not `/tmp` |
| `oicm-litellm-layer/examples/client-distribution/sync-mirror.sh` | Extend the existing verified upstream download/ingest routine to pin Chisel `v1.12.0` assets and upstream checksums. Admin sync verifies upstream SHA-256 before upload, manifest only records verified hashes, and installer never downloads GitHub directly |
| `oicm-litellm-layer/deploy/base/gateway/kustomization.yaml` | Do not mount broker or MCP execution files here unconditionally. The source-broker overlay owns its service independently; the MCP execution lane is a separately reviewed opt-in manifest |
| `oicm-litellm-layer/deploy/base/gateway/config/litellm-config.yaml` | Do not seed private-source records in every replicated YAML. `mcp_registration.py` writes one DB record whose private URL is `http://mcp-source-broker.adeo-litellm.svc:9090/sources/<source-id>[/mcp]`. DB reload supplies every owning MCP runtime instance |
| `oicm-litellm-layer/deploy/prod/litellm-ingress.yaml` | Add an **Exact `/mcp-worker`** path forwarding only the WebSocket to isolated broker Service public port `8088`, with long timeouts. Prefix `/mcp-worker/` paths must not allow arbitrary broker admin API exposure. Only connector authentication, not public service registration or raw admin calls, crosses this boundary |
| `oicm-litellm-layer/deploy/overlays/prod/kustomization.yaml` | Add the isolated broker only as an explicitly reviewed feature overlay. Also capture the split MCP runtime routing requirement once this subsystem is deployed |

### 3.5 NEW: tests that must be written with implementation

| Exact file | Required behavior |
|---|---|
| `tests/unit/proxy/mcp_source_broker/test_pool.py` | One logical record contains all A/B/C members; healthy members can all receive a distribution of successful calls; configured limits hold; unhealthy/quarantined members are absent from selection; identical snapshot input is deterministic; revoked or removed members disappear |
| `tests/unit/proxy/mcp_source_broker/test_health.py` | Actual upstream probe results, not a hardcoded list, form the catalog fingerprint. Two different directories with the same name fail isolation instead of being pooled. Catalog changes invalidates the logical MCP snapshot. Internet-only access cannot cause two unrelated sources to merge |
| `tests/unit/proxy/mcp_source_broker/test_enrollment.py` | Admin-issued one-time enrollment token mints once; stored hash never equals or reveals the token; same `Idempotency-Key` cannot repurpose another enrollment; localhost targets and loopback ports must be generated; invalid transport, IP-literal target restrictions, oversized specs, and unsupported versions are rejected |
| `tests/unit/proxy/mcp_source_broker/test_router.py` | Pool choice affects forwarded responses; all unavailable members yield `503`; a non-transient domain `4xx` is not replayed elsewhere; streams and SSE are not buffered to EOF; JSON-RPC notifications have no retry side effect; `Mcp-Session-Id` pins to one member; hop credentials never appear at the source upstream |
| `tests/unit/proxy/mcp_source_broker/test_store.py` | Uses a test database to prove durable restarts, compare-and-swap handling, no SQL data-rewrite migration, session generation monotonicity, atomically pre-reserved relay `(loopback address, port)` uniqueness, and independence of connector and inventory keys |
| `tests/unit/proxy/client_distribution/test_routes.py` and `test_generator.py` | New installer stays outside the generic artifact path; different targets select the correct versioned asset; downloaded bytes are checked before running; no secret in script text, process arguments, or log |
| `tests/unit/proxy/test_oicm_drop_detection.py` | Pin only app mounting, not substantive business behavior. The behavior belongs in the new slice tests |
| `tests/e2e/mcp_source_broker/` | Add only per this directory's own harness conventions: real Chisel exchange, reboot persistent connector, non-root restart, outage recovery, pool balance, gateway deploy recovery, distinct-source isolation |

Use captured `initialize`, `tools/list`, and `tools/call` transcripts from a real source host during Phase 2 of the mapping technique, not an invented MCP schema. Save those under `docs/mcp-source-broker/live-data/` with a numbered README only when they are collected. Do not claim a verification step has run by adding placeholder captures

## 4. Deduplication and load-balancing algorithm

### 4.1 Source identity

`source_id` is a UUID. `source_key` is an admin-controlled lowercase DNS label in the source's ownership domain. A caller may join an existing source only with a fresh admin-issued enrollment bundle or the current inventory token; possession of a relay username or a shared public alias is not enough.

Tenant-scoped identity is mandatory:

```text
(tenant_id, source_key) -> data_source -> one persistent LiteLLM registration
                                             |
                                             +--> many connector bindings
                                             +--> independent requests/capacity memberships
```

This means A, B, C register **the same source** only when they are demonstrably serving the same material backend and an admin has approved that binding. A remote MCP server for production finance records and a public Internet egress proxy with identical paths are separate sources. A common egress proxy should be registered as one internet proxy capability, **not merged with every direct private database source**; requests to `search_internet` and `read_private` belong to different data-source namespaces even when the upstream host offers both

### 4.2 Verification before joining a member

For native MCP, collect a canonical **catalog identity** from an authenticated real `initialize` plus complete `tools/list` pagination, bounded to a declared tool-cap and byte-cap. Normalize names, descriptions, and input schemas after applying the existing tool normalization rules; content descriptions can differ semantically rather than textually. A tool with a side effect or an input that accepts a server selector is not equivalent to a catalog without those fields. Include resources and prompt catalogs in scope before advertising them.

For OpenAPI, hash a size-capped canonical specification with redacted secrets but injected no gateway URL. Shared private schema content does not prove shared source records; an admin-created binding plus read-only equivalence proof is required. For neither technology should the gateway issue a write request, destructive action, search with business data, or generic free-form PUT as an equivalence check. If a source cannot supply a read-only identity proof, an admin must explicitly approve a shared binding and keep it out of automatic joining

The default behavior on a different fingerprint is **409 conflict and isolation**, never "load balance both anyway" or "keep the old source and call it deduplicated". The fingerprint becomes the expected logical source catalog. Reconciliation after an allowed catalog change updates it once, as a versioned administrative operation, not as an uncontrolled background merge.

For the MCP catalog, do not assert that every replica has identical current data. Verify the same **contract** and the same declared backend ownership. Source drift is surfaced with metrics and per-member last-observed data only if the actual protocol exposes such data; no hallucinated data-version label is invented by the broker

### 4.3 Selection and failure handling

Use this per-request algorithm for stateless calls:

```text
entry: one public MCP request for logical source
     |
     v
validate caller grant using existing MCP auth -> choose source snapshot
     |
     v
eligible = active + connected + catalog compatible + not draining + not overloaded
     |
     | empty -> 503 source_unavailable, do not invent a secondary data source
     |
     v
sort eligible by (in_flight / max_concurrency, EWMA latency, stable member_id)
     |
     v
choose first -> forward once over corresponding private listener
     |
     +-- tunnel/connection failure before forwarding bytes --> next eligible member once
     |
     +-- accepted stream, timeout, HTTP 4xx/5xx, application error --> return that result
```

Multiple eligible members receive work through concurrent **request streams**, not through round-robin percentages or repeated identical calls. Admin-set `weight` and `max_concurrency` express available host capacity; `in_flight / max_concurrency` is the primary local decision. A small EWMA favors lower latency without sending everything to the first healthy entry. Capacity checks come before balancing; per-source and per-caller fairness is rate limiting, not an obscure scheduling parameter.

**A retry needs evidence of non-execution.** A timeout, partial JSON-RPC response, closed stream, write upstream, `5xx`, or OpenAPI `POST` is unsafe to repeat. Forward bytes once on the selected candidate where possible. To get failover on a transport connection error, only repeat when the broker failed before any upstream bytes were sent. For plain read-only requests it can use an explicit operator-approved `retry_policy`, not infer idempotence from an operation's `GET` alone. Clients cannot ask the router to replay arbitrary mutations

For native MCP, the `Mcp-Session-Id` binds **source ID, logical downstream session, auth context, catalog generation, and member ID** for every subsequent request: initialize, calls, prompts, resource reads, subscriptions, pagination, sampling/elicitation, notifications, and SSE. That state cannot be reconstructed on another proxy worker by copying only a `server_id`. Normalize only session-ID header casing; a forgeable `member_id` path argument is never allowed to bypass it. If the chosen member dies, return an explicit MCP session-expired response so the client must reinitialize. HTTP `404` is considered only after testing the SDK's `1.108.5` behavior at the transport edge. Silently retargeting a live stateful session risks cross-backend state and privacy leakage.

The one-worker MCP execution lane gives each public downstream MCP session one owning process. It must include the public `/mcp` and `/<alias>/mcp` route surface at the ingress, not only an internal annotation. Audit other `/v1/mcp/tools/call`-style execution paths before enabling the tunnel; if a different process can create a differently keyed upstream MCP session, pass it through the same owning lane or explicitly scope the first release to stateless/OpenAPI. Remaining replicated OpenAPI calls carry no native upstream MCP session. Broker affinity uses `(mcp_session_id, source_id, auth_binding_hash)` and member generation. HA still requires later protocol-aware session migration, not only a Redis `session_id` copied to a new worker

For **OpenAPI**, allow a second candidate only under the same no-bytes-sent connect-failure condition. A successful HTTP response, including a `4xx` or `5xx`, is returned unmodified with its body and headers. The original spec remains the source of tool definitions. If direct OpenAPI balancing is later moved into Python, inject selection into `create_tool_function(base_url_resolver=...)` from `_register_openapi_tools()` rather than reparsing on each call

## 5. One-call installation and restart persistence

### 5.1 Enrollment contract (admin first, then one user call)

For security, do not allow an unlabeled inbound machine to create arbitrary persisted MCP backends, choose any TCP target, or get admin authority using a tunnel key. The operator creates a one-time `enrollmentToken` for one source and one bootstrapping operation (initial source, join candidate, or connector replacement). It expires after a short operator-set TTL and has a single use tied to its `enrollment_id` and an optional expected host identity.

Admin `enrollments` returns an installer URL and one-time token, not a service credential. The `/install` route returns a template, not executable JSON. The installer exchanges the token once before systemd commit. Any approved remote target is stored with that enrollment contract; the installer cannot offer arbitrary egress redirection.

| Exact route | Receiver auth and body | Result |
|---|---|---|
| `POST /v1/mcp/source-broker/enrollments` on gateway | `PROXY_ADMIN` only, body `{tenant_id, source_key, kind, mode, target_policy, source_backend, initial_catalog_contract, expires_at}` | Mints one `enrollmentToken`, returns full download URL plus its enrollment id, retains HMAC digest. Does not allow query-supplied source IDs outside its own approved new/join binding |
| `GET /mcp-source-broker/install?token=...` on gateway | One-time installer token lookup, body never reaches Chisel | Generates a bounded reviewable shell template. Source enrollment is performed before the systemd commit. Invalid/expired input is non-2xx, never an HTML or JSON error body served as a 200 script |
| `GET /mcp-source-broker/enroll` on gateway | One-time installer token in `Authorization`; uses server-reserved target policy, not arbitrary user fields | Gateway validates token and reported source identity, then calls broker internal `POST /mcp-worker/v1/enroll` over `8089` with scoped service token. Returns connector credentials once. Retry idempotent by enrollment id |
| `POST /mcp-worker/v1/enroll` on private `8089` | Gateway-scoped service authentication plus one-time token digests. Requires complete typed grant policy and idempotency key. Never public ingress | Creates source/member and private listener from approved policy only. Returns connector SSH parameter binding and source-specific `inventoryToken` only when separately granted |
| `POST /mcp-worker/v1/connectors/<id>/heartbeat` private `8089` | Scoped gateway/broker management service credential, authenticated connector identity and fixed member generation. Body reports approved listener, catalog generation and version, not a new route | Persists liveness/check result, advances revision. Different expected catalog returns `409` and quarantines. Never arbitrary remote-host grants |
| `POST /mcp-worker/v1/connectors/<id>/revoke` private `8089` | Scoped internal management request, cause derived from admin-authorized inventory action. It is not an inventory token granting broker control-plane privilege | Evicts membership/creds, terminates registered session id or executes relay drain/restart, then invalidates caches. API outcome is typed and renewed through reconcile. Existing tunnels are not assumed closed by credential deletion |
| `GET /v1/mcp/source-broker/sources` | `PROXY_ADMIN` or scoped view | List source summary, not relay ports or bearer tokens, to unprivileged keys |
| `GET /v1/mcp/source-broker/catalog` | Operator-issued inventory token, scoped to trusted harness enrollment. It returns only the fixed aggregate entry and configured logical entries; it does not claim to have resolved the target user's grants. Normal user keys can instead discover through existing `/v1/mcp/tools` | Safe `{alias,url:"/source-alias/mcp"}` values and the one aggregate `{id:"adeo_litellm_mcp",url:"/mcp"}`. No private URL or connector token |

Implementation detail: `/mcp-worker` forwards Chisel's TLS WebSocket, authenticated by its SSH username/password, never a user LiteLLM key. In `v1.12.0`, `Remote.UserAddr()` emits the listener for a reverse remote, and `server_handler.go` checks that `R:<host>:<port>` against the authfile before `CanListen()`. Generate a fixed exact-address `^R:127[.]0[.]0[.]<last-octet>:<allocated-port>$` regex per connector. Assert relay bind addresses, version, authorized reverse listens, and reject wildcard listeners/socks. **No `r` ACL rule exists in the pinned `1.12.0` stock reverse-remote source; remove every proposal that relies on it**

The gateway-facing management layer reserves factory defaults and the SSH endpoint, and sends a typed source-host `R:127.0.0.<assigned-host>:<assigned-port>:127.0.0.1:<source-port>` instruction to a pinned connector wrapper. The wrapper validates and executes that fixed REMOTE. The stock Chisel server binds the remote `host:port` on behalf of the source client, so raw Chisel does **not** fully constrain its own source-side target. A source-host administrator is therefore a trusted operator of its backend, but cannot turn a solely scoped connector network token into general MCP/admin/LLM privileges. Document that trust explicitly; further root-level containment requires a separately reviewed authenticated source adapter, not a regex promise

The broker independent control app on `8089` exposes enrollment, connector heartbeat/rotate/revoke, and service reconciliation. It requires scoped service authentication and its bearer claims are distinct from end user/operator tool grants. Public `8088` is restricted to WebSocket upgrade; no `POST /mcp-worker/v1/**` management call reaches it. Gateway internal service auth is validated before executing connector actions; auth cache invalidation and immediate revocation have their own tests

The internal broker `/mcp-worker/v1/enroll` returns the source connector's Chisel SSH username/password and immutable manifest containing listener, source port, URL, and fingerprint. An optional separately approved inventory grant configures a user harness independently; it never replaces connector authentication or a user's ordinary MCP virtual key. The public MCP aggregate contains no source worker port or connector credentials.

The unprivileged connector service cannot use its bearer token to widen enrollment policy through a broker API because its only network function is connector establish/heartbeat for its fixed generation. It cannot list sources, mint new connectors, or sink arbitrary loopback ports via enrollment. Root on the source host necessarily supplies content for its enrolled source, so an application-vendor source credential and target ownership check still protect data integrity; the broker cannot prevent source-root takeover of that host's own service. Privileged manifest changes and target rotations require an admin/inventory-scoped mutation, and the next relay allowlist must apply them only after revalidation

The returned artifact is a SHA-256-verified archive from the existing mirror. No upstream signature policy is invented in this slice. The server cannot tell a client to execute an arbitrary remote filename or command. It selects just `os+arch`, fixed version `1.12.0`, fixed mirror path, expected `sha256`, and expected `chisel --version`. Nothing is built on the source host

### 5.2 System service and stable runtime configuration

The installer has two stages, using the same artifact and saved metadata on retries:

```text
unprivileged preflight
    |--> only curl, CA roots, uname, read enrollment URL and optional CA path
    |--> read enrollment token from stdin, /dev/tty, or protected enrollment file
    |--> fetch + SHA-256 verify + extract chisel to staging
    |--> refuse unsupported target or no usable enrollment secret
    v
one privileged one-time commit, invoked explicitly by administrator
    |--> create system user oicm-mcp and private /etc/oicm-mcp-connector
    |--> install versioned binary under /opt/oicm-mcp-connector/1.12.0/
    |--> connector.env with fixed gateway URL, fingerprint, allocated loopback port, REMOTE string, credentials 0600
    |--> install systemd unit with the gateway-issued values, Restart=always
    |--> systemctl enable --now oicm-mcp-connector
    v
one service user, all subsequent runs
    local TCP liveness + Chisel auto-reconnect + service-manager restart
```

A portable Debian/Ubuntu form is:

```ini
[Unit]
Description=OICM MCP reverse connector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=oicm-mcp
Group=oicm-mcp
EnvironmentFile=/etc/oicm-mcp-connector/connector.env
ExecStart=/opt/oicm-mcp-connector/bin/chisel client --fingerprint "$RELAY_FINGERPRINT" --keepalive 25s --auth "$AUTH" "$GATEWAY_WORKER_URL" "$REMOTE"
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths=/var/lib/oicm-mcp-connector

[Install]
WantedBy=multi-user.target
```

Hardening must not prevent dialing the local source service, and must not put such a user on root-owned listener ports. Only the connector's allowed data destination is exposed to the gateway. The per-connector `AUTH` is supplied only via a protected systemd `EnvironmentFile=`. Its argv expansion is still observable to a same-user process, so document that local trust boundary and use a Go client wrapper with in-memory credential injection in the hardened follow-up. Treating variable expansion as secrecy would be incorrect; the service secret is not embedded in the repository, installer, HTTP response log, or URL

Use the gateway `AdminRouter` to create an enrollment and render a source installer with one public base URL. It retains only the token digest and approved policy in the broker. The installer reads the one-time token from the protected bootstrap environment/stdin and exchanges it through `GET /mcp-source-broker/enroll` with `Authorization: Bearer`. The gateway authenticates its internal `8089` request to the broker with its service identity. Source-supplied target or source IDs must exactly match the approved enrollment. Its final bootstrap script never includes connector or inventory plaintext in the repository, URL, logs, or process args.

The download from `/mcp-source-broker/install` is source-host setup, independent of `GET /install` for ordinary harness setup. The source bootstrap JSON produced by its exchange has `{source_id, connector_id, relay_url, fingerprint, remote, connector_token}`. Validate its schema; never `source` returned shell variables. Its token is stored only in root-owned readable-to-service environment/config files. That installation does not need a user's virtual key and does not render or update coding-tool MCP configs.

The optional `inventoryToken` is a separate broker-issued configuration credential bound to explicitly approved sources. It can rotate/revoke its own scoped connector and view only its assigned catalog entries. It is not obtained by rendering a normal user's aggregate MCP config. Harness profiles always have stable IDs: `adeo_litellm_mcp` for aggregate, optional `adeo_litellm_<source_key>` for a source alias. These use normal user MCP virtual-key auth; connector credentials never appear in harness config and never get copied into per-app shell variables.

For unsupported source hosts, do not pretend a shipped shell provides reboot persistence. First-class Linux source platforms use systemd. Only implement Windows native services or macOS `launchd` with their own credentials, trust-store behavior, and restart validation.

### 5.3 Secret lifecycle and revocation

Use three bearer classes:

| Credential | Who holds it | Privilege |
|---|---|---|
| `enrollmentToken` | An enrollment endpoint request | Exchanges once for a connector binding after separate gateway-origin proof. It is not itself an admin credential and cannot create/approve arbitrary sources |
| `connectorToken` | Dedicated source-host system user | Establishes Chisel connection and receives one preallocated private reverse listener; no MCP keys or admin rights |
| `inventoryToken` | Authorized configuration client | Lists gateway MCP logical URLs in its granted access-group scope, changes its own member enrollment, and rotates/revokes its own connector binding. It cannot change ownership or create arbitrary bindings |

A connector bearer must not list every data source, issue new enrollment bundles, configure unrelated coding agents, call LLM inference, or mint additional credentials. An inventory token sees only explicitly granted sources; joining a source still needs a separate fresh enrollment grant with target and catalog constraints, not merely holding an inventory credential. It is never equivalent to an "internet access" entitlement.

A revocation has **three effects**, verified under test:

1. DB/Redis authorization and pool snapshot evict all affected members, so new calls cannot choose them
2. The connector must be actively disconnected. Chisel's authfile reload validates new users and new channels; an already-established session can keep its existing tunnel. Generate a superseded/Chisel auth file then restart only the relay sidecar, or run a thin fork-carried management endpoint that terminates exact authenticated session IDs. Test revocation with a persistent open tunnel instead of assuming the docs imply immediate removal
3. Rotation never returns secret state. One `connectionGeneration` distinguishes replacement from late heartbeats by the old process; rotation rejects the old generation without creating a different logical source or reordering aliases

Restarting a connector alone should cause no registry duplication. Its tunnel has a short unhealthy window, then resumes within the persistent member lease; its heartbeat/revision updates are operational state, not new logical identity. Gateway/broker Pod restart changes no public alias or logical MCP grant. Kubernetes Secrets provide the same host key and pepper, Chisel reconnects, and the DB reattaches the connector generation. No bare increase in listener port on restart. If a replacement broker Pod cannot bind the persistent loopback port assignment, its readiness must fail before accepting new routing

The deployment is intentionally one-replica initially. Use Recreate replacement semantics and drain the old relay instead of starting two Pods with the same listeners. Shutdown stops selecting draining members and closes session state; reinitialization after replacement is expected. No active-standby claim is valid before routing and tunnel/session ownership have been implemented

## 6. MCP configuration automation

### 6.1 Exact user destination

Do not serve a harness-labeled list from storage whose keys cannot be resolved by normal MCP authorization. An admin-provisioned `access_group` is simpler for the first slice: the catalog shows only entries covered by this deployment's `inventory_scope`, such as `adeo_litellm`. The existing LiteLLM permission code validates the actual key's access-group entitlement at request time. A generic list response without a specific user key can render those URLs, not grant them.

Use the already supported public remote MCP endpoint. The cc-switch client config representation is:

```json
{
  "id": "adeo_litellm_mcp",
  "name": "ADEO LiteLLM",
  "server": {
    "type": "http",
    "url": "https://litellm.ecouncil.ae/mcp",
    "headers": {"Authorization": "Bearer <user-virtual-key>"}
  },
  "apps": {"claude": true, "codex": true, "gemini": false, "opencode": false, "hermes": false}
}
```

The user supplies only a named harness and a virtual key, not a connector token, source URL, Chisel fingerprint, or fixed port. The one aggregate entry uses id `adeo_litellm_mcp`; repeated configuration reconciles that ID with `--overwrite`, rather than creating an entry per tunnel. `--app` must set the **complete** final supported-harness matrix, not accidentally turn other apps off. Command-line `--apps` versus the CLI's global `--app` is intentionally used according to cc-switch's actual enum.

Coding harnesses supported by the installed cc-switch CLI for MCP are Claude, Codex, Gemini, OpenCode and Hermes. OpenClaw and Pi have app provider support but are not MCP targets in this installer. Unsupported detected harnesses require an explicit message.

### 6.2 Exact cc-switch source change and proposed release

The existing `cc-switch-cli` **already manages MCP servers**, with `McpServer {id,name,server,apps,...}` and sync adapters for remote `http` and `sse`. Its current `mcp add`, however, drives interactive JSON editing through `crate::cli::editor::open_external_editor()`, while `McpService.upsert_server()` provides the needed overwrite operation. This is a small enabling change to the existing maintained fork rather than a new per-harness parser.

Use branch `jya0-v5.12.0` with a new release after verification. `jya0-5.11.1` is today's baseline, not a promise that noninteractive MCP add already works there.

| Exact source | Change |
|---|---|
| `src-tauri/src/cli/commands/mcp.rs` | Change `Add` from a unit variant to `Add { id: String, name: String, config_file: PathBuf, apps: Vec<AppType>, overwrite: bool }`. On the noninteractive path validate complete `McpServer` JSON with Serde, require a nonzero `server.type`, restrict allowed transport to `http` for this managed profile, call `McpService.upsert_server()`, then sync enabled targets to live config. `--config-file` is read with key file permissions and never logged. `--overwrite` must name a required same stable managed ID; otherwise duplicate ID errors. Do not invent a different overwrite rule for each harness |
| `src-tauri/src/services/mcp.rs` | Reuse `upsert_server()` and `sync_all_enabled()`. Add a structured changed-ID result only if the CLI needs reporting; never shell out to interactive `mcp edit` |
| `src-tauri/src/app_config.rs` | No shape change. Reuse `McpServer` and its `McpApps` support matrix |
| `src-tauri/src/mcp.rs`, `claude_mcp.rs`, `opencode_config.rs` | No new parser. Reuse existing remote-HTTP sync behavior and test the initial aggregate UI config |
| `src-tauri/tests/mcp_add_noninteractive.rs` | New behavior tests: first install adds one managed endpoint; re-run identical JSON updates without duplicates; changing URL and key reconciles only my ID; unrelated MCP records survive; valid per-app enablement produces actual native JSON/TOML; a private key in read config is not emitted to stdout/errors/terminal |
| `src-tauri/tests/mcp_commands.rs` | Extend mapped tests for edge cases in remote `http` / `sse`, preserving unrelated sources and explicitly disabling unsupported harnesses |

Installation command in the sidecar's rendered script is:

```sh
cc-switch mcp add \
  --id adeo_litellm_mcp \
  --name 'ADEO LiteLLM' \
  --config-file "$TMP/mcp-endpoint.json" \
  --apps "$CHOSEN_MCP_APPS" \
  --overwrite
```

The script writes the user-supplied key under `umask 077` and supplies no API key in argv. It should fail clearly if new cc-switch is required instead of silently falling back to an interactive editor on today's binary. The installer validates API key via existing gateway diagnostics before enabling a live config. If app configuration fails after server installation, the connector stays installed and the returned prompt says exactly how to reconcile harness configuration, rather than deleting a working connector.

### 6.3 Registered client configuration endpoint for generic users

For a user not using cc-switch, provide the accurate path already supported by the harness rather than pretend `curl` is itself an MCP client. The starter target is **Claude Code**. Its gateway MCP connection directly uses the aggregate remote HTTP endpoint and a user virtual-key Authorization header via the CLI's native configuration. No Chisel or tunnel is installed for this case.

The server's `GET /v1/mcp/source-broker/catalog` returns the gateway-configured aggregate and access-group namespaces to the scoped inventory client, while normal keys use existing `/v1/mcp/tools` permission resolution to see callable tools. For every configured namespace, rendering is deterministic:

```text
aggregate: /mcp
data source: /<stable-source-alias>/mcp
```

A new source becomes available only after a successful live MCP init and equivalent-tool listing. It gets the ordinary entitlement via the existing tooling (`LiteLLM_ObjectPermissionTable.mcp_servers` or delegated-key/team access behavior). Its public URL does not receive the randomly assigned Chisel port. An inventory token is needed only for administrative/auto-provisioning actions such as local catalog updates or approved source joining, not for the user's static stable entry

## 7. Precise durable contracts

### 7.1 Minimal schema shape

Do not reuse `LiteLLM_MCPServerTable.credentials` as a tunnel-credential dumping ground. It can contain sensitive upstream OAuth state, and exposing/rewriting it has different semantics than connector authentication.

```prisma
model OicmMcpSource {
  source_id           String   @id
  tenant_id           String
  source_key          String
  alias               String
  transport           String
  upstream_kind       String
  spec_location       String?
  catalog_fingerprint String
  inventory_scope     String
  status              String
  version             Int      @default(1)
  created_at          DateTime @default(now())
  updated_at          DateTime @updatedAt

  connectors OicmMcpSourceConnector[]

  @@unique([tenant_id, source_key])
  @@unique([tenant_id, alias])
}

model OicmMcpSourceConnector {
  connector_id        String    @id
  source_id           String
  credential_digest   String    @unique
  credential_generation Int     @default(1)
  inventory_digest    String?   @unique
  relay_host          String
  relay_port          Int
  remote_host         String
  remote_port         Int
  chisel_version      String
  catalog_fingerprint String?
  capability_validated_at DateTime?
  last_heartbeat_at   DateTime?
  revision            Int       @default(1)
  created_at          DateTime  @default(now())
  revoked_at          DateTime?

  source OicmMcpSource @relation(fields: [source_id], references: [source_id])

  @@unique([relay_host, relay_port])
  @@index([source_id, revoked_at])
}

model OicmMcpEnrollment {
  enrollment_id       String    @id
  token_digest        String    @unique
  operation           String
  source_id           String?
  target_json         Json
  expires_at          DateTime
  redeemed_at         DateTime?
  redeemed_generation Int?
  created_at          DateTime  @default(now())
}
```

Actual naming follows repo migration conventions. No credential or source payload may be logged by `store.py`, FastAPI access logs, the installer, or auth module. One-time enrollment redemption is atomic: only one concurrent `UPDATE ... WHERE redeemed_at IS NULL` may mint a returned secret, using a transaction and external side-effect ordering that can survive retries. Minting and external sidecar update are not magic distributed SQL transactions; reconciliation with an idempotency key reconciles their uncertainty.

One installation token does not turn into new privilege after it is exchanged. One member ID does not become a new data-source ID if a source host is reinstalled. A shared source means "same logical backend", not "same source machine" and not "same network permission". Name these objects distinctly in code.

`relay_host` is an assigned loopback IP, not a port-only reservation. The store allocates a unique `(relay_host,relay_port)` pair from an explicit generated allowlist. Without the address dimension, two separate loopback ranges could collide on the same port. The inventory store separately enforces key version and token generation.

Every Postgres transition has a typed outcome: `created`, `unchanged`, or a typed version/identity/catalog conflict. `If-Match` carries the source version. One operation updates connector and optionally source via a DB transaction with the row/table identity keys in scope. The gateway logical `LiteLLM_MCPServerTable` row and broker store cannot be committed atomically across two database connections. Serialize `broker pending -> logical registration -> broker active`, resume idempotently after restart, never silently dispatch from `pending`, and roll back/retry failed registration rather than leaving an externally visible half-pooled endpoint

### 7.2 Relay reconciliation

For every active member, state saved in the database is rendered to a generated Chisel authfile and the relay is restarted when necessary for revocation or authentication-key changes. Use a small Go management layer or a carefully guarded sidecar restart, not a shell that kills unrelated processes. The durable private key is always the same within one gateway environment unless deliberately rotated.

Redis is a disposable liveness/affinity cache in this design, not a credential ledger or session-migration protocol. On Redis loss, recover only under the short DB heartbeat fallback. During mandatory session affinity, a broker restart intentionally invalidates its downstream sessions instead of keeping an old mapping with no owning live member

An assign plan is:

```text
Pod/relay instance = mcp-source-broker-0 (sticky identity)
fixed listener range: 127.0.0.20-127.0.0.254, allocatable 18080-18120
one member lease -> (relay instance, address, port) -> on every restart:
    read DB assignment; no increment on current maximum
not-used-since is not sufficient to free a listener: explicit terminal revoke/tombstone only
released allocation is quarantined until every Chisel connection of the old generation is gone
active Chisel session_ids are registered through the controller and protected in drain/revoke
```

The connector wrapper generates `R:127.0.0.<assigned-host>:<assigned-port>:127.0.0.1:<source-service-port>` from its immutable enrollment. The real `1.12.0` Remote has only reverse `R:`, `Socks`, and `Stdio` modes in this path; the pinned `UserAddr()` returns just `R:host:port` for listener ACL checks. It has no supported `r` local-endpoint counterpart that can be relied on here. Server reverse forwarding creates listeners for the source client, and the tunnel's destination endpoint is implemented under the source client's tunnel. Do not infer stronger source-target verification than the source demonstrates. Tests must use captured configs, authfile cases, and an adversary-supplied modified client listening target rather than merely replaying normal setup. Broader target-independent forwarding is explicitly deferred

Reverse listeners use `127.0.0.20` through `127.0.0.254`, never wildcard or public bind. The broker dispatch listener `9090` and allocated reverse-listener ports must also be protected by default-deny workload egress/egress-to-loopback admission design where feasible. K8s NetworkPolicy covers Pod network boundaries but does not by itself restrict a compromised container reaching another container's loopback in the same Pod. Therefore only these two minimal audited containers share that network namespace; no untrusted sidecar may join it

## 8. Deliverable sequence and verification gate

### Phase 1: establish baseline evidence

Create only `tests/e2e/mcp_source_broker/pilot/` harness and `docs/mcp-source-broker/live-data/README.md` once evidence exists. Run the existing pilot against `1.108.5`, save raw startup log, reverse probe, OpenAPI register and direct invocation, `initialize`, `tools/list`, `tools/call`, and reconnect responses. Pin the source origin and no employer-specific service content. Compare the successful request/response to the function map above. Do not build the pool before the source works with Chisel on that baseline

### Phase 2: enrollment and persistence (vertical slice)

Implement schema, models, store, enrollment, artifact upload, connector service, and gateway control router. Prove one reconnect after source reboot and one gateway restart yields **one** saved logical server, zero duplicate recipient tools, working service restart, and no secret exposure. Prove correct one-time install on a fresh Linux machine with denied internet except the gateway.

### Phase 3: source router and pooled capacity

Implement a stable private source router and its independent broker deployment. Use two distinct real endpoints with distinct markers for targeted probe testing; do not combine those fake markers into the production source. Verify the publicly exposed tool namespace is one, actual catalog equality is required, failure only removes one member, and two eligible healthy independent connectors share calls. Validate serialization of per-member in-flight state and pagination with real MCP not trivial `GET`s.

### Phase 4: harness setup and administration

Ship cc-switch `jya0-5.12.0` after its tests and live config checks, then update the client manifest. Test first install and idempotent re-run, per-app enablement, unrelated MCP preservation, client restart persistence and `inventoryToken` rotation. Verify exact curl shown by the installer, service status, live gateway direct tool use, inference route use, an ordinary coding agent session, and an admin revoke. All real logs must be sanitized.

### Definition of done

| Requirement | Passing result |
|---|---|
| Exact one endpoint | Authenticated user downloads `GET /mcp-source-broker/install?token=...`, reviews the script if wanted, runs once, is ready |
| Actual binary | Official Chisel `v1.12.0`, static Linux AMD64/ARM64 build, fetched from same-origin mirror and verified with version+hash |
| Reboot persistence | Dedicated-user unit auto-starts, survives service crash and host reboot, reconnects without new enrollment |
| Server restart persistence | Connector and registration records rebuild from DB/Kubernetes Secret with Recreate rollout semantics, with no duplicate alias or tool entry. Native MCP sessions reinitialize during a relay/broker replacement |
| Correct MCP setup | Supported harness calls **`/mcp`**, with only its normal user virtual key; duplicate managed ID does not pile up |
| Deduplication | One `(tenant, source key)` yields one logical record and one visible tool namespace; member A/B/C need compatible proven catalogs |
| Useful distribution | Healthy independent connectors carry concurrent calls, with live data in their own request logs proving selection; one disabled connector stops receiving new calls |
| Safe failover | Non-execution connection error may repeat once, stream/mutation/application failure is not replayed |
| Session correctness | All native MCP state uses one gateway worker and one member until termination; no stateful session can be switched secretly |
| Authorization | Existing virtual-key MCP admission and grants protect actual tool access; relay token supplies network access but not LLM/harness privilege |
| Revocation | Active connection is actively closed and new dispatch chooses other members immediately, not merely after token expiry |
| Independence | Unrelated public/direct services cannot be grouped through internet permissions, shared target host, name, or access-group |
| Isolation | Public ingress can only reach `/mcp-worker` broker port; pool listener ports are private and never Service-published |

## 9. Open questions settled by this design and ones deferred

Do not silently defer these while implementing the first slice:

1. **HA downstream MCP Sessions:** initial production uses one MCP-worker execution lane per public downstream session registry and one broker worker/member binding per downstream session. A multiple-worker MCP deployment cannot be enabled for this path without distributed session affinity or shared migration; test stateless/OpenAPI independently before claiming HA
2. **Shared WebSocket across replicas:** the design does not attempt to round-robin the same persistent tunnel listener between proxy replicas. A dedicated relay has the connection until client reconnect; DB+Redis membership state carries the logical binding
3. **Performance/caching:** pooling adds usable redundancy. Caching, multicast and response deduplication are later capabilities with explicit cache ownership and safety; sending every read to every source would harm bandwidth and expose response contents unnecessarily
4. **Arbitrary network egress:** users request a service, not a free TCP relay. Add any `R:socks` or remote-IP target only through a separately reviewed product capability, an allowlist, and authenticated logging. It cannot be inferred from `available_on_public_internet: true`
5. **Source restart without administrator:** connector enrollment is one-time because the service identity is durable. Replacing ownership or rotating connector credentials requires scoped admin/inventory authority, not a stateless URL accessible forever
6. **First-class server platforms:** initial support intentionally omits generic SSH installers, Windows services, macOS `launchd`, rootless reboot persistence, and free-form arbitrary remote protocols. Document unsupported Linux environments before offering them a download

## 10. Reference anchors used to form the plan

- LiteLLM `1.108.5` LTS: [release](https://github.com/BerriAI/litellm/releases/tag/v1.108.5), commit `2a61f21`; the `1.108.5` deployment remains `ghcr.io/berriai/litellm:v1.108.5` / its corresponding private mirrored `litellm-src` image, not a mutable `latest` channel
- `MCPServer` single URL, auth, limits, `spec_path`: [`litellm/types/mcp_server/mcp_server_manager.py`](https://github.com/BerriAI/litellm/blob/v1.108.5/litellm/types/mcp_server/mcp_server_manager.py)
- Existing OpenAPI tools execute a registration-time bound base URL, without general connection rebalancing: [`openapi_to_mcp_generator.py#L502-L628`](https://github.com/BerriAI/litellm/blob/v1.108.5/litellm/proxy/_experimental/mcp_server/openapi_to_mcp_generator.py#L502-L628)
- Native upstream session state is keyed by downstream session and logical server, not member: [`mcp_server_manager.py#L6257-L6307`](https://github.com/BerriAI/litellm/blob/v1.108.5/litellm/proxy/_experimental/mcp_server/mcp_server_manager.py#L6257-L6307)
- Chisel `1.12.0`: [release and CLI behavior](https://github.com/jpillora/chisel/tree/v1.12.0), [reverse-remotes format](https://github.com/jpillora/chisel/blob/v1.12.0/share/settings/remote.go), [live authfile behavior](https://github.com/jpillora/chisel/blob/v1.12.0/share/settings/users.go), [relay session handling](https://github.com/jpillora/chisel/blob/v1.12.0/server/server_handler.go). The official Linux checksum must be rechecked at ingest and tested against the pilot's hash, never copied blindly into any installer
- cc-switch-cli `jya0-v5.11.1`: [MCP service reuse](https://github.com/AITDEV00/cc-switch-cli/tree/jya0-v5.11.1/src-tauri/src/services/mcp.rs), [interactive CLI handling](https://github.com/AITDEV00/cc-switch-cli/tree/jya0-v5.11.1/src-tauri/src/cli/commands/mcp.rs), [existing remote MCP adapters](https://github.com/AITDEV00/cc-switch-cli/tree/jya0-v5.11.1/src-tauri/src/mcp.rs). Branch `jya0-v5.12.0` is the proposed next feature branch for noninteractive MCP enrollment, not an already-published client
