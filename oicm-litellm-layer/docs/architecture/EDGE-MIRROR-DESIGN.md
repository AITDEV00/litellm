# Secondary gateway (edge mirror) — design exploration

Status: exploration, nothing implemented. Records what the fork can already do, what it cannot,
and which of the four candidate designs is worth building. Written 2026-10-26 against branch
`jyao-v1.106.1`.

## Problem

A second environment cannot reach the model backends directly, but needs the same models, the same
per-key policy, and its own local capabilities (internal databases, MCP servers, internet egress).
A gateway in that environment should behave as much like `litellm.ecouncil.ae` as possible without
becoming a second source of truth.

The requirement evolved during the exploration, in this order, and each step eliminated a design:

1. Mirror models, credentials, and usage into a second database.
2. Extend the fork so the second gateway mirrors production.
3. Forward by wildcard, derive keys and model lists from production per request.
4. Stateless, no logs, MCP configurable over REST instead of YAML.
5. A pure transparent shell that forwards every route unchanged.

The final answer is a spectrum, not a single design. Which point on it is correct depends on whether
the edge can reach production directly, and on how much of the admin surface the secondary
environment actually needs.

## The constraint that removes most designs

Stateless, runtime-configurable over REST, and restart-safe across replicas cannot all hold. A REST
write has to land somewhere durable, or the next pod boot forgets it and the second replica never
sees it. So "stateless" cannot mean "no storage exists anywhere". It can only mean:

> The forwarding process holds no state of its own, and no state is ever copied from production.

State is either delegated to its owner at request time, or owned in exactly one place. Nothing is
mirrored. That rule eliminates every sync loop, reconciler, and copied key table considered earlier,
because all of them are state that can drift.

## What the fork already supports

Verified by reading the tree, not from documentation.

### Provider surface for `litellm_proxy`

`ProviderConfigManager` has a `LITELLM_PROXY` branch for:

| Surface | Location |
|---|---|
| Chat | `litellm/utils.py:8685` |
| Responses | `litellm/utils.py:9302` |
| Image edit | `litellm/utils.py:9733` |
| Image generation | `litellm/utils.py:9932` |
| Rerank | `litellm/rerank_api/main.py:206` |
| Model listing (`get_provider_model_info`) | `litellm/utils.py:9414` |

Missing, and therefore a code change if needed: embeddings (`get_provider_embedding_config`,
`litellm/utils.py:8860` returns `None` for this provider) and audio transcription
(`get_provider_audio_transcription_config`, `litellm/utils.py:9111`). Text-to-speech, files, and
batches also have no branch. Chat, responses, images, and rerank work today.

### Wildcard forwarding

`model_name: "*"` with `litellm_params.model: litellm_proxy/*` forwards any incoming model name
upstream. `PatternMatchRouter.set_deployment_model_name`
(`litellm/router_utils/pattern_match_deployments.py:196`) substitutes the captured segment, and
`LiteLLMProxyChatConfig` strips the `litellm_proxy/` prefix before the wire call. Production
therefore does all real routing, health checks, fallbacks, and model-table work.

A bare `model_name: "*"` is a trap for the auto-router, which looks deployments up by the literal
requested model string with no wildcard resolution (`litellm/proxy/client/cli/README.md:653`).

### Key auth is a hash lookup

Virtual keys are stored as `sha256(token)` (`hash_token`, `litellm/proxy/_types.py:290`) and auth is
a hash lookup, so identical client keys authenticate on any gateway holding the row. `/key/list` with
`return_full_object=true` returns rows including the hash. `/key/generate` refuses any `key` that does
not start with `sk-` (`litellm/proxy/management_endpoints/key_management_endpoints.py:1520`), and
`generate_key_helper_fn(token=...)` is not exposed on any route, so importing a pre-hashed row needs
one small admin route.

`hash_token_if_needed` (`litellm/proxy/utils.py:7319`) treats a value without the `sk-` prefix as
already hashed, which makes such an import route straightforward.

### Per-key model listing is free if auth populates `models`

`get_complete_model_list` (`litellm/proxy/auth/model_checks.py:202`) prefers `key_models`, then
`team_models`, then the proxy model list. So if a custom auth returns the production answer for that
caller's key in `models`, `/v1/models` on the edge is exactly what production grants that key, with
no copy and no sync. Leaving `models` empty falls through to wildcard expansion against the
deployment's own credential, which would show every caller everything the deployment key can see.

`enforced_model_allowlists` (`litellm/proxy/auth/auth_checks.py:5016`) returns
`(key_models, team_models)` directly when `prisma_client is None`, so model allowlists still enforce
on a database-less proxy.

### Custom auth is the whole feature, and one flag is mandatory

`custom_auth` returns a `UserAPIKeyAuth` from an arbitrary callable. Two properties matter:

- `custom_auth_run_common_checks: true` is required. Without it, `run_centralized_common_checks`
  returns early (`litellm/proxy/auth/user_api_key_auth.py:2905`) and budgets, route checks, team
  blocking, and model access are all skipped, with only a startup warning.
- `api_key` must be the hash, not the plaintext. `get_logged_api_key`
  (`litellm/proxy/litellm_pre_call_utils.py:1689`) returns it into spend logs and Prometheus labels.

`allowed_routes` on the returned object is enforced by
`RouteChecks.is_virtual_key_allowed_to_call_route` (`litellm/proxy/auth/route_checks.py:100`), which
gives inference-only behaviour without the enterprise `DISABLE_ADMIN_ENDPOINTS` gate
(`enterprise/litellm_enterprise/proxy/auth/route_checks.py:39`).

### Database-less operation

`setup_prisma_client` returns `None` when `database_url is None`
(`litellm/proxy/proxy_server.py:11762`). Every spend writer no-ops on `prisma_client is None`
(`litellm/proxy/db/db_spend_update_writer.py:1646`, `3088`, `3108`). `_safe_fetch`
(`litellm/proxy/auth/user_api_key_auth.py:2710`) swallows non-auth lookup failures, so
`get_team_object` raising `No DB Connected` (`litellm/proxy/auth/auth_checks.py:3417`) does not 500
a request; `_token_can_vouch_for_team` (`2761`) decides whether the token's own team fields may stand
in.

Budget enforcement is the exception: there is an explicit warning that without a database budgets are
never enforced and requests are never blocked (`litellm/proxy/proxy_server.py:10495`). On the edge
that is correct, because production enforces the budget when it authorizes the forwarded key.

### MCP works without a database

`mcp_servers` is read from config at startup (`litellm/proxy/proxy_server.py:7362`) and handed to
`load_servers_from_config` (`litellm/proxy/_experimental/mcp_server/mcp_server_manager.py:2435`),
which builds an in-memory registry. Database-backed features degrade rather than fail: BYOM
submissions (`3450`), toolsets (`3701`), per-user env vars (`4146`). `allow_all_keys: true` on a
server makes it reachable by every caller without any per-key grant table, which matters when the
edge has no key table to put grants in.

### Transparent pass-through

Generic pass-through forwards byte-transparently when configured to: `forward_headers: true` merges
caller headers over defaults and keeps `Authorization`, `include_subpath: true` forwards the whole
subtree, and multipart bodies are not parsed or re-encoded. Streaming works.
`_AUTH_ENFORCED_PASS_THROUGH_ROUTE_GROUPS` (`litellm/proxy/auth/route_checks.py:71`) is the only
allow-list nuance.

### Silencing logs

No single switch exists. It takes three settings: `LITELLM_LOG=ERROR` for the global level
(`litellm/_logging.py:653`, applied to uvicorn at `litellm/proxy/proxy_cli.py:324`; the default is
`DEBUG`), `turn_off_message_logging: true` to redact prompt and response content
(`litellm/__init__.py:209`), and `LITELLM_DISABLE_ACCESS_LOG_PATHS` to drop uvicorn access lines per
path (`litellm/_logging.py:241`). That variable is a path list, not a global off switch; zero access
lines needs a custom `--log_config` (`litellm/proxy/proxy_cli.py:836`) with no `uvicorn.access`
handler. `uvicorn.access` logs the full request target, which is why the redaction filter exists
(`litellm/_logging.py:916`): a credential in a query string lands on stdout verbatim.

## Designs considered, and why each was rejected

**Share production Postgres.** Couples blast radius and migrations. Production's CNPG instance
already OOMKilled 24 times at a 2 GB limit with eight pools of ten connections.

**Share production Redis.** Already an acknowledged smell between dev and prod in this cluster: dev
traffic mutates production HTB counters, spend keys, and deployment affinity.

**Postgres logical replication of `LiteLLM_VerificationToken`.** Works, but couples the edge to
production's schema and needs privileges that should not be granted across environments.

**Reconciler that mirrors models and keys.** Highest-risk component of every design considered: it
has delete-on-absence semantics and no natural source tag, and this cluster has already seen a
reconciler delete the wrong environment's row when a Service selector pointed at the wrong pods.

**`control_plane_url` plus `worker_registry`.** This is UI routing only and shares no state, so it
does not address the requirement.

**Kubernetes API as the MCP control plane.** Elegant in isolation (RBAC, encrypted Secrets,
`resourceVersion` optimistic concurrency, watch-based convergence, audit history, no second
database), but it replaces one configuration interface with another and adds a REST-to-ConfigMap
adapter whose scope balloons the moment toolsets, per-user grants, or OAuth dynamic client
registration are wanted. Not chosen, but recorded because it is the best option if the edge ever
needs a durable control plane without owning a database.

## The three viable points on the spectrum

### A. Pure shell (transparent proxy)

A dedicated FastAPI app whose entire surface is generic pass-throughs to production, with no
database. `auth: false` and let production reject unauthenticated callers with its own 401, which is
the most transparent possible behaviour.

Runs on a separate app rather than `proxy_server:app` for one concrete reason: LiteLLM's native
routers are mounted before generic pass-throughs, so a `/v1/chat/completions` pass-through may never
run because the native handler claims the path first. A separate app has no competing handlers, which
removes the route-priority question entirely.

Zero code. Fails the UI requirement, and has three real seams:

- **Files and batches.** Production's `/v1/files/{file_id}/content` and any presigned URL point at
  production's object store. Forwarded faithfully, the client gets an ID and a URL that may not be
  reachable from the secondary environment.
- **The Admin UI.** Production's UI is a static Next.js build whose `proxyBaseUrl` is baked at build
  time. Serving its assets through a shell works, but the browser then calls production's API
  directly, so the UI only functions if the browser can also reach production.
- **Realtime websockets.** `create_websocket_passthrough_route` forwards only a narrow header
  allow-list (`_WEBSOCKET_FORWARDED_HEADERS = frozenset(("x-goog-user-project",))`), so realtime
  through a shell is not equivalent to calling production.

### B. Delegating edge (recommended)

Wildcard forwarding to production plus a `custom_auth` that asks production's `/key/info` about the
caller's key and returns the policy. No database, no key table, no model table.

What it buys over A: per-key model listing works, inference-only enforcement comes from production's
`allowed_routes`, revocation is instant because production is consulted rather than copied, and MCP
stays local. Costs one HTTP hop per uncached auth, so cache by hash with a short TTL keyed on
production's `updated_at`.

The auth hook must exempt `/health/*` and pass-through paths, or health probes reach production.
`user_role` must never be `PROXY_ADMIN`.

### C. Edge with a local database

A stock LiteLLM with its own small Postgres, custom auth delegating to production, and the wildcard
model list. Local key creation is inert because auth consults production before the local table is
read, so this ships quickly and gets the Admin UI and REST MCP configuration with no new code. Carries
a second database, its migrations, backups, and janitor forever. A phase-0 baseline, not a
destination.

## Decision

The correct point depends on one question the design cannot answer on its own: can the secondary
environment reach `litellm.ecouncil.ae` directly?

- If yes, A is sufficient and nearly free.
- If no, A's three seams (files, UI, realtime) are the real work, and B is the smallest design that
  still satisfies per-key policy without copying state.
- If the Admin UI and REST MCP configuration are non-negotiable and the timeline is short, C.

The one code change common to every point except A: `get_provider_embedding_config` needs a
`litellm_proxy` branch, or `/v1/embeddings` fails through the wildcard.

## Open questions

- Which endpoints the Admin UI's MCP page calls, so any adapter's surface is exact rather than
  guessed.
- Whether the edge is single-replica. If it is, the watch/poll machinery for MCP changes collapses
  into a boot-time read plus a short poll, which is materially less to build.
- Whether the secondary environment's browsers can reach production, which decides whether the
  Admin UI requirement is satisfiable at all through a shell.
