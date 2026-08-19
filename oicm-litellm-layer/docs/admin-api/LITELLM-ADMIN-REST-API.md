# LiteLLM Proxy Admin REST API Guide

Comprehensive reference for every HTTP admin REST endpoint in the LiteLLM proxy for managing API keys, teams, organizations, models, fallbacks, router settings, users, budgets, and spend.

All endpoints are served by the proxy's FastAPI app (default port `4000`). Unless noted otherwise, every endpoint requires the `Authorization: Bearer <key>` header and uses the `user_api_key_auth` dependency for authentication.

## Connecting to the ADEO LiteLLM Proxy

The ADEO gateway uses a self-signed TLS certificate. All `curl` commands must use the `-k` (insecure) flag.

| Setting | Value |
|---------|-------|
| **Base URL** | `https://litellm.adeoaiengine.ecouncil.ae` |
| **Master Key** | `{{ master_key }}` |
| **TLS** | Self-signed; use `-k` with curl |

Set these environment variables for convenience:

```bash
export PROXY_BASE_URL="https://litellm.adeoaiengine.ecouncil.ae"
export LITELLM_API_KEY="{{ master_key }}"
```

Quick verification:

```bash
curl -sk "$PROXY_BASE_URL/health/liveness" \
  -H "Authorization: Bearer $LITELLM_API_KEY"
```

All curl examples in this guide use `$PROXY_BASE_URL` and `$LITELLM_API_KEY`. For the ADEO proxy, every command needs the `-k` flag to skip TLS verification. For example:

```bash
curl -sk -X GET "$PROXY_BASE_URL/model/info" \
  -H "Authorization: Bearer $LITELLM_API_KEY"
```

**Source files** (all paths relative to repo root):
- `litellm/proxy/proxy_server.py` ; main app, route registration
- `litellm/proxy/management_endpoints/key_management_endpoints.py`
- `litellm/proxy/management_endpoints/team_endpoints.py`
- `litellm/proxy/management_endpoints/team_callback_endpoints.py`
- `litellm/proxy/management_endpoints/organization_endpoints.py`
- `litellm/proxy/management_endpoints/model_management_endpoints.py`
- `litellm/proxy/management_endpoints/internal_user_endpoints.py`
- `litellm/proxy/management_endpoints/budget_management_endpoints.py`
- `litellm/proxy/management_endpoints/fallback_management_endpoints.py`
- `litellm/proxy/management_endpoints/router_settings_endpoints.py`
- `litellm/proxy/_types.py` ; Pydantic request/response models
- `litellm/types/router.py` ; `UpdateRouterConfig`, `Deployment`
- `litellm/types/management_endpoints/router_settings_endpoints.py` ; `ROUTER_SETTINGS_FIELDS`

---

## Table of Contents

1. [Authentication and Roles](#1-authentication-and-roles)
2. [API Key (Virtual Key) Management](#2-api-key-virtual-key-management)
   - [Restricting Allowed Routes](#restricting-allowed-routes)
   - [Assigning Priority to Keys](#assigning-priority-to-keys)
   - [Browsing and Searching Keys](#browsing-and-searching-keys)
3. [Team Management](#3-team-management)
4. [Organization Management](#4-organization-management)
5. [Model Management and Model Rates](#5-model-management-and-model-rates)
   - [Model RPM/TPM Rate Limits](#model-rpmtpm-rate-limits)
6. [Fallback Management](#6-fallback-management)
7. [Router Settings](#7-router-settings)
8. [User Management](#8-user-management)
9. [Budget Management](#9-budget-management)
10. [Spend and Cost Tracking](#10-spend-and-cost-tracking)
11. [Cost Tracking Configuration](#11-cost-tracking-configuration)
12. [Hierarchical Router Settings](#12-hierarchical-router-settings)

---

## 1. Authentication and Roles

All endpoints validate the `Authorization: Bearer <key>` header via the `user_api_key_auth` dependency. The caller's role (stored on `UserAPIKeyAuth.user_role`) determines access scope.

| Role | Description |
|------|-------------|
| `proxy_admin` | Full access to all endpoints and data. Authenticated via the master key (`LITELLM_MASTER_KEY`) or a key with `user_role == PROXY_ADMIN`. |
| `proxy_admin_viewer` | Read-only access to all keys, teams, spend, etc. |
| `org_admin` | Admin over a specific organization; can create teams/users within it. |
| `internal_user` | Manage own keys, view own spend, access Admin UI. |
| `internal_user_viewer` | Read-only access to own data. |
| Team admin | A `Member` with `role: "admin"` in a team; can manage keys/members within that team. |

A custom header `litellm-changed-by` (optional) is accepted by mutation endpoints to record who initiated the action on behalf of another user, for audit trails.

---

## 2. API Key (Virtual Key) Management

**Source:** `litellm/proxy/management_endpoints/key_management_endpoints.py`
**Models:** `litellm/proxy/_types.py`, `litellm/types/proxy/management_endpoints/key_management_endpoints.py`

### Endpoint Summary

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/key/generate` | Create a new virtual key |
| POST | `/key/service-account/generate` | Create a service account key (team-owned, no user) |
| POST | `/key/update` | Update an existing key |
| POST | `/key/bulk_update` | Bulk update multiple keys |
| POST | `/team/key/bulk_update` | Apply one update payload to many keys in a team |
| POST | `/key/delete` | Delete one or more keys |
| GET | `/key/info` | Get info about a single key |
| POST | `/v2/key/info` | Get info about multiple keys (admin only) |
| GET | `/key/list` | List keys with pagination and filtering |
| GET | `/key/aliases` | List key aliases with pagination and search |
| POST | `/key/{key}/regenerate` | Regenerate a key (Enterprise) |
| POST | `/key/regenerate` | Alias for regenerate (body-based key) |
| POST | `/key/{key}/reset_spend` | Reset a key's spend counter |
| POST | `/key/block` | Block a key from making requests |
| POST | `/key/unblock` | Unblock a previously blocked key |
| POST | `/key/health` | Check key health (logging callbacks) |

### POST `/key/generate` ; Create a Virtual Key

**Auth:** Any authenticated user. Non-admins have `user_id` auto-assigned.

**Request body:** `GenerateKeyRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `key` | `Optional[str]` | No | User-defined key value. Auto-generated `sk-` key if omitted. |
| `key_alias` | `Optional[str]` | No | Human-friendly alias. |
| `duration` | `Optional[str]` | No | Token validity (`"30s"`, `"30m"`, `"30h"`, `"30d"`). |
| `user_id` | `Optional[str]` | No | User ID to associate. Auto-assigned for non-admins. |
| `team_id` | `Optional[str]` | No | Team ID. |
| `organization_id` | `Optional[str]` | No | Organization ID. Defaults to team's org if `team_id` set. |
| `budget_id` | `Optional[str]` | No | Existing budget ID. |
| `models` | `Optional[list]` | No | Allowed model names. Empty = all models. |
| `aliases` | `Optional[dict]` | No | Model alias mappings. |
| `config` | `Optional[dict]` | No | Key-specific config overrides. |
| `spend` | `Optional[float]` | No | Initial spend. Default `0`. |
| `max_budget` | `Optional[float]` | No | Max budget (USD). |
| `soft_budget` | `Optional[float]` | No | Soft budget; triggers Slack alert. |
| `budget_duration` | `Optional[str]` | No | Budget reset period (`"30s"`, `"30m"`, `"30h"`, `"30d"`). |
| `budget_limits` | `Optional[List[BudgetLimitEntry]]` | No | Multiple concurrent budget windows. Each: `{budget_duration: str, max_budget: float, reset_at?: datetime}`. |
| `max_parallel_requests` | `Optional[int]` | No | Max concurrent requests. |
| `metadata` | `Optional[dict]` | No | Arbitrary metadata. |
| `tpm_limit` | `Optional[int]` | No | Tokens-per-minute limit. |
| `rpm_limit` | `Optional[int]` | No | Requests-per-minute limit. |
| `model_max_budget` | `Optional[dict]` | No | Per-model budgets. |
| `model_rpm_limit` | `Optional[dict]` | No | Per-model RPM limits. |
| `model_tpm_limit` | `Optional[dict]` | No | Per-model TPM limits. |
| `tpm_limit_type` | `Optional[str]` | No | `"best_effort_throughput"`, `"guaranteed_throughput"`, or `"dynamic"`. |
| `rpm_limit_type` | `Optional[str]` | No | Same options as `tpm_limit_type`. |
| `allowed_cache_controls` | `Optional[list]` | No | Allowed cache-control values. |
| `permissions` | `Optional[dict]` | No | Key-specific permissions, e.g. `{"pii": false}`. |
| `guardrails` | `Optional[List[str]]` | No | Active guardrails. |
| `policies` | `Optional[List[str]]` | No | Policy names to apply. |
| `disable_global_guardrails` | `Optional[bool]` | No | Disable global guardrails for this key. |
| `prompts` | `Optional[List[str]]` | No | Allowed prompts. |
| `blocked` | `Optional[bool]` | No | Whether the key starts blocked. |
| `tags` | `Optional[List[str]]` | No | Tags for spend tracking and tag-based routing. |
| `allowed_routes` | `Optional[List[str]]` | No | Route groups the key is allowed to access. Empty or absent = all routes. See [Restricting Allowed Routes](#restricting-allowed-routes). |
| `router_settings` | `Optional[UpdateRouterConfig]` | No | Per-key router settings (see [Section 12](#12-hierarchical-router-settings)). |

**Response:** `GenerateKeyResponse`

| Field | Type | Description |
|-------|------|-------------|
| `key` | `str` | The generated key value (`sk-...`) |
| `key_name` | `Optional[str]` | Key name/alias |
| `expires` | `Optional[str]` | Expiry datetime |
| `user_id` | `Optional[str]` | Associated user ID |
| `team_id` | `Optional[str]` | Associated team ID |

**Example:**

```bash
curl -X POST 'http://localhost:4000/key/generate' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{
    "key_alias": "dev-team-key",
    "team_id": "team-123",
    "models": ["gpt-4o", "claude-3-5-sonnet"],
    "max_budget": 100.0,
    "rpm_limit": 500,
    "tpm_limit": 100000,
    "budget_duration": "30d",
    "metadata": {"environment": "staging"}
  }'
```

### POST `/key/update` ; Update a Key

**Auth:** Proxy admin, team admin, org admin, or key owner.

**Request body:** `UpdateKeyRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `key` | `str` | Yes | The key to update. |
| `key_alias` | `Optional[str]` | No | New alias. |
| `models` | `Optional[list]` | No | Updated allowed models. |
| `max_budget` | `Optional[float]` | No | Updated max budget. |
| `rpm_limit` | `Optional[int]` | No | Updated RPM limit. |
| `tpm_limit` | `Optional[int]` | No | Updated TPM limit. |
| `metadata` | `Optional[dict]` | No | Updated metadata (merged). |
| `blocked` | `Optional[bool]` | No | Block/unblock. |
| `tags` | `Optional[List[str]]` | No | Updated tags. |
| `budget_duration` | `Optional[str]` | No | Updated budget reset period. |
| `router_settings` | `Optional[UpdateRouterConfig]` | No | Per-key router settings. |

All other fields from `GenerateKeyRequest` are also accepted.

**Example:**

```bash
curl -X POST 'http://localhost:4000/key/update' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{
    "key": "sk-abc123...",
    "max_budget": 500.0,
    "rpm_limit": 1000
  }'
```

### POST `/key/delete` ; Delete Keys

**Request body:** `KeyRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `keys` | `List[str]` | Yes | Key values or aliases to delete. |

**Example:**

```bash
curl -X POST 'http://localhost:4000/key/delete' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{"keys": ["sk-abc123..."]}'
```

### GET `/key/info` ; Get Key Info

**Query params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `key` | `str` | No | Key value. If omitted, uses the caller's own key from the auth header. |

**Example:**

```bash
curl -X GET 'http://localhost:4000/key/info?key=sk-abc123...' \
  -H 'Authorization: Bearer {{ master_key }}'
```

### GET `/key/list` ; List Keys with Pagination

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `page` | `int` | `1` | Page number |
| `size` | `int` | `10` | Items per page |
| `user_id` | `Optional[str]` | ; | Filter by user |
| `team_id` | `Optional[str]` | ; | Filter by team |
| `organization_id` | `Optional[str]` | ; | Filter by organization |
| `key_alias` | `Optional[str]` | ; | Filter by alias |
| `sort_by` | `Optional[str]` | ; | Sort field |
| `sort_order` | `Optional[str]` | `asc` | `asc` or `desc` |

**Example:**

```bash
curl -X GET 'http://localhost:4000/key/list?page=1&size=20&team_id=team-123' \
  -H 'Authorization: Bearer {{ master_key }}'
```

### POST `/key/block` ; Block a Key

**Request body:** `BlockKeyRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `key` | `str` | Yes | Key to block. |

**Example:**

```bash
curl -X POST 'http://localhost:4000/key/block' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{"key": "sk-abc123..."}'
```

### POST `/key/unblock` ; Unblock a Key

Same request body as `/key/block`.

### POST `/key/{key}/regenerate` ; Regenerate a Key (Enterprise)

**Path param:** `key` ; the key to regenerate.

**Request body:** `RegenerateKeyRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `key` | `Optional[str]` | No | Key to regenerate (if not in path). |
| `duration` | `Optional[str]` | No | New validity duration. |
| `store_encrypted_key` | `Optional[bool]` | No | Whether to store the encrypted key. |

### Budget / Rate Limit Fields on Keys

The following fields can be set on `/key/generate` and `/key/update`:

| Field | Type | Scope | Description |
|-------|------|-------|-------------|
| `max_budget` | `float` | Key-level | Hard USD spend cap. Requests fail when exceeded. |
| `soft_budget` | `float` | Key-level | Alert-only threshold. Does not block. |
| `tpm_limit` | `int` | Key-level | Tokens-per-minute limit. |
| `rpm_limit` | `int` | Key-level | Requests-per-minute limit. |
| `model_max_budget` | `dict` | Per-model | e.g. `{"gpt-4o": 5.0}`. |
| `model_rpm_limit` | `dict` | Per-model | Per-model RPM limits. |
| `model_tpm_limit` | `dict` | Per-model | Per-model TPM limits. |
| `budget_duration` | `str` | Key-level | Reset window (`"1d"`, `"30d"`). |
| `budget_limits` | `List[BudgetLimitEntry]` | Key-level | Multiple concurrent budget windows. |
| `max_parallel_requests` | `int` | Key-level | Max concurrent requests. |
| `tpm_limit_type` | `str` | Key-level | `"best_effort_throughput"`, `"guaranteed_throughput"`, `"dynamic"`. |
| `rpm_limit_type` | `str` | Key-level | Same as `tpm_limit_type`. |
| `allowed_routes` | `List[str]` | Key-level | Route groups the key may access. Empty or absent = unrestricted. See [Restricting Allowed Routes](#restricting-allowed-routes). |

### Restricting Allowed Routes

The `allowed_routes` field on keys restricts which HTTP endpoints the key can call. When empty or absent, the key can access all routes (subject to role-based checks). When set to a non-empty list, only matching routes are allowed; all others return `403`.

**Values accepted:** Each entry can be either a route group name (a member of the `LiteLLMRoutes` enum) or a literal path string with optional wildcard (`*`) suffix.

**Route group names:**

| Group Name | Covers |
|------------|--------|
| `openai_routes` | All OpenAI-compatible inference endpoints (`/chat/completions`, `/embeddings`, `/images/generations`, `/audio/*`, `/v1/models`, batches, files, fine-tuning, assistants, threads, rerank, realtime, responses, vector stores, search, OCR, containers) |
| `llm_api_routes` | All inference endpoints: `openai_routes` + Anthropic routes (`/v1/messages`), Google routes (`/v1beta/models/*:generateContent`), pass-through routes (`/anthropic/*`, `/vllm/*`, `/openai/*`, etc.), guardrail apply routes, MCP inference routes, LiteLLM native routes, and agent routes |
| `anthropic_routes` | Anthropic-native endpoints (`/v1/messages`, `/v1/messages/count_tokens`, `/v1/skills`) |
| `google_routes` | Google Gemini native endpoints (`/v1beta/models/*:generateContent`, `/v1beta/models/*:streamGenerateContent`, Google Interactions API, Google Managed Agents API) |
| `mcp_routes` | MCP tool-call/passthrough routes + MCP server CRUD routes (both inference and management) |
| `mcp_inference_routes` | MCP tool-call/passthrough routes only (data-plane) |
| `mcp_management_routes` | MCP server CRUD routes only (control-plane) |
| `agent_routes` | Agent endpoints (`/v1/agents`, `/agents`, A2A protocol routes) |
| `info_routes` | Read-only info endpoints (`/key/info`, `/team/info`, `/team/list`, `/model/info`, `/model_group/info`, `/health`, `/v1/models`, etc.) |
| `management_routes` | All management endpoints: user CRUD, team CRUD, model CRUD, key management routes, MCP management routes |
| `spend_tracking_routes` | Spend query endpoints (`/spend/keys`, `/spend/users`, `/spend/tags`, `/spend/logs`, `/spend/logs/v2`, `/cost/estimate`) |
| `global_spend_tracking_routes` | Proxy-wide spend endpoints (`/global/spend/*`, `/global/activity`, `/health/services`) + all `info_routes` |
| `internal_user_routes` | Internal user routes: activity, tag usage, key management, compliance checks, spend tracking |
| `internal_user_view_only_routes` | Read-only subset of `internal_user_routes`: spend tracking + compliance checks + tag usage |
| `self_managed_routes` | Routes that enforce their own access logic (team member ops, model CRUD, user activity, project reads, guardrail submissions, invitations) |
| `key_management_routes` | Key CRUD endpoints: `/key/generate`, `/key/update`, `/key/delete`, `/key/info`, `/key/list`, `/key/block`, `/key/unblock`, `/key/regenerate`, `/key/bulk_update`, `/team/key/bulk_update`, spend logs, reset spend, aliases |
| `compliance_check_routes` | Compliance report endpoints (`/compliance/eu-ai-act`, `/compliance/gdpr`) |
| `apply_guardrail_routes` | Guardrail application endpoint (`/guardrails/apply_guardrail`) |
| `admin_viewer_routes` | Read-only admin routes: user/team/tag activity, audit, customer list, spend logs, config/callbacks read, budget settings, guardrails/policies read, MCP semantic filter settings, model cost map status |
| `master_key_only_routes` | Routes restricted to the master key only: `/global/spend/reset`, `/memory-usage-in-mem-cache`, `/memory-usage-in-mem-cache-items` |
| `public_routes` | Unauthenticated public routes: `/health/liveness`, `/health/readiness`, `/test`, `/config/yaml`, `/.well-known/litellm-ui-config`, `/public/model_hub`, `/public/agent_hub`, `/public/mcp_hub`, `/public/skill_hub`, `/public/litellm_model_cost_map` |
| `litellm_native_routes` | LiteLLM-native routes: `/rag/ingest`, `/rag/query` |
| `mapped_pass_through_routes` | Provider pass-through root paths (`/bedrock`, `/vertex-ai`, `/cohere`, `/anthropic`, `/azure`, `/openai`, `/vllm`, `/mistral`, `/watsonx`, etc.) |
| `passthrough_routes_wildcard` | Wildcard pass-through routes (`/bedrock/*`, `/anthropic/*`, `/vllm/*`, etc.) |
| `openai_route_names` | Logical names of OpenAI route groups (used for reference, not direct matching) |

**Literal path matching:**

Instead of a group name, you can pass a literal path or a wildcard pattern:

- Exact: `"/chat/completions"` matches only that path
- Wildcard: `"/v1/agents/*"` matches any path starting with `/v1/agents/`

**Create a key restricted to LLM inference only:**

```bash
curl -sk -X POST "$PROXY_BASE_URL/key/generate" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "inference-only-key",
    "team_id": "your-team-id",
    "models": ["gpt-4o"],
    "allowed_routes": ["llm_api_routes"]
  }'
```

**Update a key to restrict it to LLM inference + info endpoints:**

```bash
curl -sk -X POST "$PROXY_BASE_URL/key/update" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "key": "sk-xxxx...",
    "allowed_routes": ["llm_api_routes", "info_routes"]
  }'
```

**Restrict to a specific endpoint with a wildcard:**

```bash
curl -sk -X POST "$PROXY_BASE_URL/key/update" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "key": "sk-xxxx...",
    "allowed_routes": ["/v1/chat/completions", "/v1/embeddings"]
  }'
```

**How enforcement works:**

1. At request time, `RouteChecks.is_virtual_key_allowed_to_call_route()` checks the incoming path against `allowed_routes`.
2. If `allowed_routes` is `None`, not a list, or empty, the check passes (no restriction).
3. For each entry in `allowed_routes`, the checker tries (a) exact path match, (b) wildcard prefix match, then (c) `LiteLLMRoutes` enum name lookup.
4. If any entry matches, the request is allowed. If none match, the request returns `403`.
5. For `llm_api_routes`, registered pass-through endpoints are also checked, and a method-aware carve-out allows `GET` on MCP server discovery endpoints (`/v1/mcp/server`, `/v1/mcp/server/{id}`) so restricted keys can still list/inspect MCP servers.
6. The master key bypasses `allowed_routes` checks entirely.

**Verify a key's allowed_routes:**

```bash
curl -sk -X GET "$PROXY_BASE_URL/key/info?key=sk-xxxx..." \
  -H "Authorization: Bearer $LITELLM_API_KEY"
```

The response includes the `allowed_routes` array in the key object.

### Assigning Priority to Keys

Priority is not a dedicated top-level field. It is a free-form string stored inside the `metadata` dict on keys, teams, and organizations. The string must match a key in the `priority_reservation` mapping configured under `litellm_settings` in `config.yaml`.

**Config (already set on the ADEO proxy):**

```yaml
litellm_settings:
  priority_reservation:
    prior1: 0.50    # 50% of model capacity
    prior2: 0.30    # 30% of model capacity
    prior3: 0.20    # 20% of model capacity
  priority_reservation_settings:
    saturation_threshold: 0.80
    default_priority: 0.25
```

**How it works at request time:**

1. The `DynamicRateLimitHandlerV3` reads `metadata["priority"]` from the API key.
2. Team metadata takes precedence over key metadata: if the key's team has `metadata.priority`, that wins.
3. The priority string is matched against `priority_reservation` to get a reservation weight (e.g. `"prior1"` -> `0.50`).
4. Reserved RPM/TPM = `model_group_capacity * weight`. For a 180 RPM model with `prior1`, the key gets 90 RPM reserved.
5. Below `saturation_threshold` (0.80): priority limits are tracked but not enforced (generous mode; any key can use spare capacity). At or above the threshold: priority limits are strictly enforced (strict mode).
6. Keys with no priority, or a priority not in the mapping, fall into a shared `default_pool` at `default_priority` (0.25).
7. Model-wide capacity (100% of RPM/TPM) is always enforced regardless of saturation.

**Create a key with priority:**

```bash
curl -sk -X POST "$PROXY_BASE_URL/key/generate" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "premium-tier-key",
    "metadata": {"priority": "prior1"},
    "models": ["gpt-4o"],
    "rpm_limit": 100
  }'
```

**Update a key's priority:**

```bash
curl -sk -X POST "$PROXY_BASE_URL/key/update" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "key": "sk-xxxx...",
    "metadata": {"priority": "prior2"}
  }'
```

Metadata is merged on update, so passing `{"priority": "prior2"}` only changes the priority; other metadata keys are preserved.

**Set priority on a team (applies to all keys on that team):**

```bash
curl -sk -X POST "$PROXY_BASE_URL/team/new" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "team_alias": "premium-team",
    "metadata": {"priority": "prior1"}
  }'
```

Update a team's priority via `/team/update` with the same `metadata` structure. Team metadata priority takes precedence over individual key metadata priority.

**Inspect a key's priority:**

```bash
curl -sk -X GET "$PROXY_BASE_URL/key/info?key=sk-xxxx..." \
  -H "Authorization: Bearer $LITELLM_API_KEY"
```

The response includes the `metadata` field containing `{"priority": "prior1"}`.

**How `rpm_limit`/`tpm_limit` interact with priority:**

| Mechanism | Field | Scope | Behavior |
|-----------|-------|-------|----------|
| Per-key hard cap | `rpm_limit` / `tpm_limit` | Top-level on key | Hard cap regardless of model or saturation |
| Per-key per-model cap | `metadata.model_rpm_limit` / `metadata.model_tpm_limit` | Metadata on key | Hard cap for a specific model |
| Priority reservation | `metadata.priority` | Metadata on key/team | Fraction of model-group capacity reserved when saturated |
| Model-group capacity | `model_group_info.rpm` / `.tpm` | Router config | Total capacity of the model group (100% baseline) |

A key with `rpm_limit: 100` and `metadata.priority: "prior1"` (0.50 reservation on a 180 RPM model) is bounded by both: 100 RPM (key cap) and 90 RPM (priority reservation when saturated). The lower of the two wins.

### Browsing and Searching Keys

#### List and Filter Keys: `GET /key/list`

The primary endpoint for browsing keys. Supports 16 query parameters with AND-combined filtering.

**Query parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `page` | `int` | `1` | Page number |
| `size` | `int` | `10` | Page size (max 100) |
| `user_id` | `string` | ; | Filter by user. Admins get substring/case-insensitive match |
| `team_id` | `string` | ; | Filter by team ID (exact) |
| `organization_id` | `string` | ; | Filter by organization ID (exact) |
| `key_hash` | `string` | ; | Filter by hashed token (exact) |
| `key_alias` | `string` | ; | Filter by alias. Admins get substring/case-insensitive match |
| `return_full_object` | `bool` | `false` | Return full key objects instead of just token strings |
| `include_team_keys` | `bool` | `false` | Include all keys for teams where caller is admin |
| `include_created_by_keys` | `bool` | `false` | Include keys created by the calling user |
| `sort_by` | `string` | ; | `spend`, `max_budget`, `created_at`, `updated_at`, `token`, `key_alias` |
| `sort_order` | `string` | `desc` | `asc` or `desc` |
| `expand` | `list[string]` | ; | Expand related objects. Supports `user` |
| `status` | `string` | ; | Filter by status. Currently supports `deleted` |
| `project_id` | `string` | ; | Filter by project ID |
| `access_group_id` | `string` | ; | Filter by access group membership |

**Filter by team:**

```bash
curl -sk -X GET "$PROXY_BASE_URL/key/list?team_id=my-team-id&page=1&size=50" \
  -H "Authorization: Bearer $LITELLM_API_KEY"
```

**Filter by organization:**

```bash
curl -sk -X GET "$PROXY_BASE_URL/key/list?organization_id=org-uuid-here" \
  -H "Authorization: Bearer $LITELLM_API_KEY"
```

**Search by alias (admins get substring matching):**

```bash
curl -sk -X GET "$PROXY_BASE_URL/key/list?key_alias=prod" \
  -H "Authorization: Bearer $LITELLM_API_KEY"
```

**Combine team + alias + full objects + sort by spend:**

```bash
curl -sk -X GET "$PROXY_BASE_URL/key/list?team_id=my-team&key_alias=prod&return_full_object=true&sort_by=spend&sort_order=desc&size=100" \
  -H "Authorization: Bearer $LITELLM_API_KEY"
```

**Find keys that can access a specific model (client-side filter with jq):**

There is no server-side model filter. Fetch full objects and filter client-side:

```bash
curl -sk -X GET "$PROXY_BASE_URL/key/list?return_full_object=true&size=100" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  | python3 -c "import sys,json; [print(k['key_alias'],k['token'][:12]) for k in json.load(sys.stdin)['keys'] if 'gpt-4o' in (k.get('models') or [])]"
```

Note that a key's effective model access is the union of its own `models` array, the team's `models` array, and access-group models. A key with an empty `models` array may still inherit model access from its team.

#### Get All Keys for a Team: `GET /team/info`

Returns full team info plus all keys that belong to the team in the `keys` array.

```bash
curl -sk -X GET "$PROXY_BASE_URL/team/info?team_id=your-team-id" \
  -H "Authorization: Bearer $LITELLM_API_KEY"
```

#### Get All Teams (Each with Keys): `GET /team/list`

Returns all teams the caller can see, each with its `keys` array populated. Filter by `organization_id` to scope to an org.

```bash
curl -sk -X GET "$PROXY_BASE_URL/team/list?organization_id=org-uuid-here" \
  -H "Authorization: Bearer $LITELLM_API_KEY"
```

#### Search Key Aliases: `GET /key/aliases`

Returns alias strings with case-insensitive partial matching. Can be combined with `team_id` filter.

```bash
curl -sk -X GET "$PROXY_BASE_URL/key/aliases?search=prod&team_id=my-team" \
  -H "Authorization: Bearer $LITELLM_API_KEY"
```

#### Bulk Key Lookup: `POST /v2/key/info`

Admin-only. Look up multiple keys by token or alias in a single call.

```bash
curl -sk -X POST "$PROXY_BASE_URL/v2/key/info" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key_aliases": ["prod-key", "staging-key"]}'
```

#### Quick Reference: Filter Combinations

| Goal | Endpoint |
|------|----------|
| Keys for a team | `/key/list?team_id=X` |
| Keys for an org | `/key/list?organization_id=X` |
| Keys for a user | `/key/list?user_id=X` |
| Keys by alias search | `/key/list?key_alias=X` |
| Aliases by search | `/key/aliases?search=X` |
| Aliases for a team | `/key/aliases?team_id=X` |
| Team + org + alias combined | `/key/list?team_id=X&organization_id=Y&key_alias=Z` |
| Single key by token | `/key/info?key=sk-...` |
| Multiple keys by tokens/aliases | `POST /v2/key/info` |
| All keys for a team (with team info) | `/team/info?team_id=X` |
| All teams with their keys | `/team/list` |
| Keys for an org (indirect) | `/organization/info` then `/key/list?organization_id=X` |
| Keys by model | `/key/list?return_full_object=true` + client-side filter |
| Deleted keys | `/key/list?status=deleted` |

---

## 3. Team Management

**Source:** `litellm/proxy/management_endpoints/team_endpoints.py`, `litellm/proxy/management_endpoints/team_callback_endpoints.py`

### Endpoint Summary

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/team/new` | Create a new team |
| POST | `/team/update` | Update team settings |
| POST | `/team/delete` | Delete one or more teams |
| GET | `/team/info` | Get detailed info on a single team |
| GET | `/team/list` | List teams (filtered by user/org) |
| GET | `/v2/team/list` | Paginated list with filtering, sorting |
| GET | `/team/available` | List teams a user is eligible to self-join |
| GET | `/team/filter/ui` | Proxy-admin-only paginated search |
| POST | `/team/member_add` | Add one or more members |
| POST | `/team/bulk_member_add` | Bulk-add members with per-result response |
| POST | `/team/member_delete` | Remove a member |
| POST | `/team/member_update` | Update a member's budget, role, limits |
| GET | `/team/{team_id}/members/me` | Get the caller's own membership row |
| POST | `/team/block` | Block all calls from a team's keys |
| POST | `/team/unblock` | Unblock a team |
| POST | `/team/model/add` | Add models to a team's allowed list |
| POST | `/team/model/delete` | Remove models from a team's allowed list |
| GET | `/team/permissions_list` | Get team member permissions |
| POST | `/team/permissions_update` | Set permissions for one team |
| POST | `/team/permissions_bulk_update` | Append permissions across teams |
| GET | `/team/daily/activity` | Paginated daily spend/activity report |
| POST | `/team/{team_id}/callback` | Add a logging callback |
| POST | `/team/{team_id}/disable_logging` | Disable all logging callbacks |
| GET | `/team/{team_id}/callback` | Retrieve callback settings |

### POST `/team/new` ; Create a Team

**Auth:** `PROXY_ADMIN` or `ORG_ADMIN`.

**Request body:** `NewTeamRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `team_alias` | `Optional[str]` | No | Human-friendly team name. |
| `team_id` | `Optional[str]` | No | Auto-generated UUID if omitted. |
| `organization_id` | `Optional[str]` | No | Parent organization. |
| `admins` | `list` | No | List of admin user IDs. |
| `members` | `list` | No | List of member user IDs. |
| `members_with_roles` | `List[Member]` | No | Members with explicit roles. |
| `team_member_permissions` | `Optional[List[str]]` | No | Permissions available to team members. |
| `metadata` | `Optional[dict]` | No | Arbitrary metadata. |
| `tpm_limit` | `Optional[int]` | No | Team-level TPM limit. |
| `rpm_limit` | `Optional[int]` | No | Team-level RPM limit. |
| `max_budget` | `Optional[float]` | No | Team-level max budget (USD). |
| `soft_budget` | `Optional[float]` | No | Alert-only threshold. |
| `budget_duration` | `Optional[str]` | No | Reset period (`"1d"`, `"30d"`). |
| `budget_limits` | `Optional[List[BudgetLimitEntry]]` | No | Multiple concurrent budget windows. |
| `models` | `list` | No | Allowed models for team. Default `[]`. |
| `blocked` | `bool` | No | Whether team starts blocked. Default `false`. |
| `router_settings` | `Optional[UpdateRouterConfig]` | No | Per-team router settings. |
| `access_group_ids` | `Optional[List[str]]` | No | Access group IDs. |
| `default_team_member_models` | `Optional[List[str]]` | No | Default models for new team members. |

**`Member` model:**

| Field | Type | Description |
|-------|------|-------------|
| `user_id` | `Optional[str]` | User ID (one of `user_id`/`user_email` required) |
| `user_email` | `Optional[str]` | User email |
| `role` | `Literal["admin", "user"]` | Member role |

**Response:** `LiteLLM_TeamTable`

**Example:**

```bash
curl -X POST 'http://localhost:4000/team/new' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{
    "team_alias": "ml-platform",
    "models": ["gpt-4o", "claude-3-5-sonnet"],
    "max_budget": 1000.0,
    "rpm_limit": 2000,
    "tpm_limit": 500000,
    "budget_duration": "30d",
    "members_with_roles": [
      {"role": "admin", "user_id": "user-123"},
      {"role": "user", "user_id": "user-456"}
    ]
  }'
```

### POST `/team/update` ; Update a Team

**Auth:** Proxy admin, org admin, or team admin (via `_verify_team_access`).

**Request body:** `UpdateTeamRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `team_id` | `str` | Yes | Team to update. |
| All other `NewTeamRequest` fields | ; | No | Any field can be updated. |

**Response:** `{team_id, data}` with updated team object.

**Example:**

```bash
curl -X POST 'http://localhost:4000/team/update' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{
    "team_id": "team-123",
    "max_budget": 5000.0,
    "models": ["gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet"]
  }'
```

### POST `/team/delete` ; Delete Teams

**Request body:** `DeleteTeamRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `team_ids` | `List[str]` | Yes | Team IDs to delete. |

Cascades: deletes all keys and memberships for each team.

### GET `/team/info` ; Get Team Detail

**Query params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `team_id` | `str` | Yes | Team ID. |

Returns team with keys, memberships, and budget table.

### POST `/team/member_add` ; Add Members

**Request body:** `TeamMemberAddRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `team_id` | `str` | Yes | Team ID. |
| `member` | `Union[List[Member], Member]` | Yes | Single member or list. |
| `max_budget_in_team` | `Optional[float]` | No | Member's budget within the team. |
| `rpm_limit` | `Optional[int]` | No | Member's RPM limit within the team. |
| `tpm_limit` | `Optional[int]` | No | Member's TPM limit within the team. |
| `models` | `Optional[List[str]]` | No | Models this member can access. |

**Example:**

```bash
curl -X POST 'http://localhost:4000/team/member_add' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{
    "team_id": "team-123",
    "member": {"role": "user", "user_id": "user-789"},
    "max_budget_in_team": 100.0
  }'
```

### POST `/team/member_update` ; Update a Member

Same fields as `TeamMemberAddRequest` plus `team_id`. Can update `max_budget_in_team`, `role`, `rpm_limit`, `tpm_limit`, `models`.

### POST `/team/model/add` ; Add Models to Team

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `team_id` | `str` | Yes | Team ID. |
| `models` | `List[str]` | Yes | Models to add. |

### POST `/team/model/delete` ; Remove Models from Team

Same request body as `/team/model/add`.

### POST `/team/block` and `/team/unblock`

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `team_id` | `str` | Yes | Team to block/unblock. |

### Team Budget / Rate Limit Fields

All inherited from `LiteLLM_BudgetTable`:

| Field | Type | Description |
|-------|------|-------|
| `max_budget` | `Optional[float]` | Hard USD spend cap. |
| `soft_budget` | `Optional[float]` | Alert-only threshold. |
| `tpm_limit` | `Optional[int]` | Team-level TPM limit. |
| `rpm_limit` | `Optional[int]` | Team-level RPM limit. |
| `model_max_budget` | `Optional[dict]` | Per-model max budget. |
| `budget_duration` | `Optional[str]` | Reset window. |
| `budget_limits` | `Optional[List[BudgetLimitEntry]]` | Multiple concurrent budgets. |

---

## 4. Organization Management

**Source:** `litellm/proxy/management_endpoints/organization_endpoints.py`

### Endpoint Summary

| Method | Path | Purpose | Min Role |
|--------|------|---------|----------|
| POST | `/organization/new` | Create organization | `PROXY_ADMIN` |
| PATCH | `/organization/update` | Update organization | `PROXY_ADMIN` or `ORG_ADMIN` |
| DELETE | `/organization/delete` | Delete organizations | `PROXY_ADMIN` |
| GET | `/organization/list` | List organizations | Any authenticated (scoped) |
| GET | `/organization/info` | Get organization detail | `PROXY_ADMIN`/`PROXY_ADMIN_VIEW_ONLY` or `ORG_ADMIN` |
| POST | `/organization/info` | Deprecated; bulk info | Same as above |
| POST | `/organization/member_add` | Add members | `PROXY_ADMIN` or `ORG_ADMIN` |
| PATCH | `/organization/member_update` | Update member role/budget | `PROXY_ADMIN` or `ORG_ADMIN` |
| DELETE | `/organization/member_delete` | Remove member | `PROXY_ADMIN` or `ORG_ADMIN` |
| GET | `/organization/daily/activity` | Daily spend analytics | Any authenticated (scoped) |

### POST `/organization/new` ; Create an Organization

**Auth:** `PROXY_ADMIN` only.

**Request body:** `NewOrganizationRequest` (extends `LiteLLM_BudgetTable`)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `organization_alias` | `str` | Yes | Human-friendly name. |
| `organization_id` | `Optional[str]` | No | Auto-generated UUID if omitted. |
| `models` | `List` | No | Models the org can access. Default `[]`. |
| `budget_id` | `Optional[str]` | No | Existing budget ID. If omitted, one is created from budget fields. |
| `metadata` | `Optional[dict]` | No | Free-form metadata. |
| `model_rpm_limit` | `Optional[Dict[str, int]]` | No | Per-model RPM limit. |
| `model_tpm_limit` | `Optional[Dict[str, int]]` | No | Per-model TPM limit. |
| `object_permission` | `Optional[LiteLLM_ObjectPermissionBase]` | No | Org-scoped object permissions (MCP, vector stores, agents). |
| `soft_budget` | `Optional[float]` | No | Alert-only threshold. |
| `max_budget` | `Optional[float]` | No | Hard USD budget cap. |
| `tpm_limit` | `Optional[int]` | No | Org-level TPM limit. |
| `rpm_limit` | `Optional[int]` | No | Org-level RPM limit. |
| `model_max_budget` | `Optional[dict]` | No | Per-model max budget. |
| `budget_duration` | `Optional[str]` | No | Reset window (`"1d"`, `"30d"`). |
| `allowed_models` | `Optional[List[str]]` | No | Per-member model scope. |

**Response:** `NewOrganizationResponse` (extends `LiteLLM_OrganizationTable`)

**Example:**

```bash
curl -X POST 'http://localhost:4000/organization/new' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{
    "organization_alias": "acme-corp",
    "models": ["gpt-4o", "claude-3-5-sonnet"],
    "max_budget": 500.0,
    "tpm_limit": 1000000,
    "rpm_limit": 5000,
    "budget_duration": "30d"
  }'
```

### PATCH `/organization/update` ; Update an Organization

**Auth:** `PROXY_ADMIN` or `ORG_ADMIN` of target org.

**Request body:** `LiteLLM_OrganizationTableUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `organization_id` | `Optional[str]` | Yes (enforced in handler) | Target org. |
| `organization_alias` | `Optional[str]` | No | New alias. |
| `budget_id` | `Optional[str]` | No | Switch to a different budget. |
| `metadata` | `Optional[dict]` | No | Merged with existing. |
| `models` | `Optional[List[str]]` | No | Updated model access. |
| `object_permission` | `Optional[LiteLLM_ObjectPermissionBase]` | No | Upserted. |
| All inherited `LiteLLM_BudgetTable` fields | ; | No | Budget/rate-limit updates. |

Also accepts a nested `litellm_budget_table` object (UI-style payload).

**Response:** `LiteLLM_OrganizationTableWithMembers` (includes `members` and `teams` lists).

**Example:**

```bash
curl -X PATCH 'http://localhost:4000/organization/update' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{
    "organization_id": "org-123",
    "organization_alias": "acme-corp-renamed",
    "max_budget": 1000.0,
    "models": ["gpt-4o", "gpt-4o-mini"]
  }'
```

### DELETE `/organization/delete` ; Delete Organizations

**Auth:** `PROXY_ADMIN` only. Cascades: deletes all teams, memberships, and keys.

**Request body:** `DeleteOrganizationRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `organization_ids` | `List[str]` | Yes | Org IDs to delete. |

**Example:**

```bash
curl -X DELETE 'http://localhost:4000/organization/delete' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{"organization_ids": ["org-123"]}'
```

### GET `/organization/list` ; List Organizations

**Auth:** Any authenticated. Proxy admins see all; internal users see only their memberships.

**Query params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `org_id` | `Optional[str]` | No | Exact org ID match. |
| `org_alias` | `Optional[str]` | No | Case-insensitive partial match. |

### GET `/organization/info` ; Get Organization Detail

**Query params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `organization_id` | `str` | Yes | Org ID. |

Returns org with budget, members (including user objects), teams, and `object_permission`.

### POST `/organization/member_add` ; Add Members

**Request body:** `OrganizationMemberAddRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `organization_id` | `str` | Yes | Target org. |
| `member` | `Union[List[OrgMember], OrgMember]` | Yes | Single or list. |
| `max_budget_in_organization` | `Optional[float]` | No | User's max budget within the org. |

**`OrgMember`:**

| Field | Type | Description |
|-------|------|-------------|
| `user_id` | `Optional[str]` | User ID (one of `user_id`/`user_email` required) |
| `user_email` | `Optional[str]` | User email |
| `role` | `Literal["org_admin", "internal_user", "internal_user_viewer"]` | Org-scoped role |

**Example:**

```bash
curl -X POST 'http://localhost:4000/organization/member_add' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{
    "organization_id": "org-123",
    "member": {"role": "internal_user", "user_id": "user-456"},
    "max_budget_in_organization": 100.0
  }'
```

### PATCH `/organization/member_update` ; Update a Member

**Request body:** `OrganizationMemberUpdateRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `organization_id` | `str` | Yes | Target org. |
| `user_id` | `Optional[str]` | One of required | User to update. |
| `user_email` | `Optional[str]` | One of required | Resolved to `user_id` if only email given. |
| `max_budget_in_organization` | `Optional[float]` | No | Creates/updates the member's org budget. |
| `role` | `Optional[LitellmUserRoles]` | No | `org_admin`, `internal_user`, or `internal_user_viewer`. |

### DELETE `/organization/member_delete` ; Remove a Member

**Request body:** `OrganizationMemberDeleteRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `organization_id` | `str` | Yes | Target org. |
| `user_id` | `Optional[str]` | One of required | User to remove. |
| `user_email` | `Optional[str]` | One of required | Resolved to `user_id`. |

---

## 5. Model Management and Model Rates

**Source:** `litellm/proxy/management_endpoints/model_management_endpoints.py`

### Endpoint Summary

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/model/new` | Register a new model/deployment |
| POST | `/model/update` | Full update (PUT-style, legacy) |
| PATCH | `/model/{model_id}/update` | Partial update (preferred) |
| POST | `/model/delete` | Delete/deregister a model |
| GET | `/v1/models` | OpenAI-compatible list models |
| GET | `/models` | Alias for `/v1/models` |
| GET | `/v1/models/{model_id}` | Get single model |
| GET | `/models/{model_id}` | Alias for above |
| GET | `/model/info` | Detailed model info with pricing |
| GET | `/v1/model/info` | Alias for `/model/info` |
| GET | `/v2/model/info` | Paginated model info with search/sort |
| GET | `/model/settings` | Provider fields for UI |
| GET | `/model_group/info` | Pricing, mode, capabilities per model group |
| POST | `/access_group/new` | Create a model access group |
| GET | `/access_group/list` | List access groups |
| GET | `/access_group/{id}/info` | Get access group detail |
| PUT | `/access_group/{id}/update` | Update access group |
| DELETE | `/access_group/{id}/delete` | Delete access group |
| GET | `/model/streaming_metrics` | Streaming metrics |
| GET | `/model/metrics` | Model metrics |
| GET | `/model/metrics/slow_responses` | Slow response metrics |
| GET | `/model/metrics/exceptions` | Exception metrics |
| POST | `/reload/model_cost_map` | Reload the model cost map |
| POST | `/schedule/model_cost_map_reload` | Schedule a cost map reload |
| DELETE | `/schedule/model_cost_map_reload` | Cancel scheduled reload |
| GET | `/schedule/model_cost_map_reload/status` | Get scheduled reload status |
| GET | `/model/cost_map/source` | Get cost map source |

### POST `/model/new` ; Register a New Model/Deployment

**Auth:** Proxy admin or team admin. Requires `STORE_MODEL_IN_DB=True`.

**Request body:** `Deployment`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `model_name` | `str` | Yes | Name users call this model by (e.g. `"my-gpt-4o"`). |
| `litellm_params.model` | `str` | Yes | Actual model identifier (e.g. `"azure/my-deployment"`). |
| `litellm_params.api_key` | `Optional[str]` | No | Provider API key. |
| `litellm_params.api_base` | `Optional[str]` | No | API base URL. |
| `litellm_params.api_version` | `Optional[str]` | No | API version (Azure). |
| `litellm_params.timeout` | `Optional[float]` | No | Request timeout. |
| `litellm_params.max_retries` | `Optional[int]` | No | Max retries. |
| `litellm_params.custom_llm_provider` | `Optional[str]` | No | Override provider detection. |
| `litellm_params.max_budget` | `Optional[float]` | No | Deployment-level max budget (USD). |
| `litellm_params.budget_duration` | `Optional[str]` | No | Budget reset period. |
| `litellm_params.input_cost_per_token` | `Optional[float]` | No | Custom input cost per token. |
| `litellm_params.output_cost_per_token` | `Optional[float]` | No | Custom output cost per token. |

**Example:**

```bash
curl -X POST 'http://localhost:4000/model/new' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{
    "model_name": "gpt-4o",
    "litellm_params": {
      "model": "azure/gpt-4o-deployment",
      "api_key": "your-azure-key",
      "api_base": "https://my-resource.openai.azure.com",
      "api_version": "2024-06-01"
    }
  }'
```

### Duplicating a Model Without Controller Interference

There are two independent mechanisms that can delete manually-created model duplicates. Both must be accounted for.

#### 1. OICM Discovery Controller (`oicm_uuid` deduplication)

The controller (`oicm-litellm-layer/controller`) groups LiteLLM models by the `oicm_uuid` field in `model_info` and reconciles each group down to a single entry per UUID. Any duplicate entries sharing the same `oicm_uuid` are deleted on the next sync cycle.

The controller's `_pick_richest_entry` logic ranks duplicates by the number of config keys (`rpm`, `tpm`, `max_parallel_requests`, cost fields) present in `litellm_params`. A duplicate that only adds extra body fields (e.g. `chat_template_kwargs`) will lose to the original and be deleted.

Models without an `oicm_uuid` in `model_info` are completely invisible to the controller and will never be touched by it.

#### 2. LiteLLM Proxy `add_deployment` background job (in-memory eviction)

Even if a duplicate has no `oicm_uuid` (so the controller ignores it), the LiteLLM proxy itself runs a background `add_deployment` job every 30 seconds. This job fetches all models from the database, builds a combined ID list (DB models + config.yaml models), and evicts any router deployment whose ID is not in that list.

The `/model/new` endpoint returns `db_model: false` in its immediate response, which is a known inconsistency. The model IS written to the database, but the response object has `db_model: false` because it is constructed from the in-memory `LiteLLM_ProxyModelTable` before the 30-second `add_deployment` cycle corrects it to `db_model: true` in the router. As long as the model row exists in the DB, the `add_deployment` job will keep it in the router.

The real risk is a proxy restart. If the proxy restarts before the model has been confirmed in the DB (or if `store_model_in_db` is not `True`), the model will not be loaded on startup. Always verify persistence by checking `db_model: true` via `/model/info` after creation, and by confirming the model survives a PATCH to `/model/{model_id}/update`.

#### Creating a persistent duplicate

To create a duplicate that survives both mechanisms, omit the `model_info` block entirely (so no `oicm_uuid` is set) and include `"db_model": true` explicitly in `model_info` to force DB persistence from creation:

```bash
curl -sk -X POST "$PROXY_BASE_URL/model/new" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model_name": "MiniMaxAI/MiniMax-M3-MXFP8-no-think",
    "litellm_params": {
      "model": "hosted_vllm/MiniMaxAI/MiniMax-M3-MXFP8",
      "api_base": "http://s-908d3952-1e69-40a4-95b9-db1abff27fcb.adeo.svc.cluster.local:8080/v1",
      "api_key": "",
      "drop_params": true,
      "rpm": 500,
      "chat_template_kwargs": {"thinking_mode": "disabled"}
    },
    "model_info": {
      "db_model": true
    }
  }'
```

Key points:

- Include `"model_info": {"db_model": true}` to force DB persistence. The `/model/new` response may still show `db_model: false`, but the model IS in the DB; the 30-second `add_deployment` cycle will correct the router flag to `true`
- Do NOT include any `oicm_*` fields in `model_info`. Without `oicm_uuid`, the controller ignores the entry entirely
- Copy the `model` and `api_base` from the original deployment so traffic still routes to the same vLLM endpoint
- Add any extra body fields (like `chat_template_kwargs`) directly in `litellm_params`
- After creation, verify persistence by checking `/model/info` shows `db_model: true` and `oicm_uuid: NONE`, and confirm the model survives a PATCH to `/model/{model_id}/update`

### POST `/model/update` ; Full Update (Legacy)

**Request body:** `ModelUpdateRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `model_name` | `str` | Yes | Model name. |
| `litellm_params` | `dict` | Yes | Updated params. |

### PATCH `/model/{model_id}/update` ; Partial Update (Preferred)

**Path param:** `model_id` ; the LiteLLM model UUID (not the model name). Obtain it from `GET /model/info`.

**Request body:** Partial `Deployment` fields. Only the fields you want to change. Supports two top-level keys: `litellm_params` (provider connection settings, rate limits) and `model_info` (metadata, mode, custom IDs).

**Example:**

```bash
curl -X PATCH 'http://localhost:4000/model/7916282c-35b8-4242-85a3-dbfee75d54f5/update' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{
    "litellm_params": {
      "api_key": "new-api-key"
    }
  }'
```

### Model RPM/TPM Rate Limits

Per-deployment RPM and TPM limits control how many requests or tokens per minute a single model deployment will accept. The router enforces these as pre-call checks before routing a request to the deployment.

**Setting RPM/TPM via PATCH `/model/{model_id}/update`:**

The rate limit must be set inside `litellm_params`, not `model_info`. The UI (`model_info_view.tsx`) reads `litellm_params.rpm` and `litellm_params.tpm` to display the values. Setting `model_info.rpm` alone stores the value but it will not appear in the UI.

```bash
curl -sk -X PATCH "$PROXY_BASE_URL/model/{model_id}/update" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "litellm_params": {
      "rpm": 100,
      "tpm": 100000
    }
  }'
```

**Finding the `model_id`:**

The `model_id` is a LiteLLM-generated UUID, not the model name. Fetch it from `GET /model/info`:

```bash
curl -sk -X GET "$PROXY_BASE_URL/model/info" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
for m in data.get('data', []):
    name = m.get('model_name', '')
    model_id = m.get('model_info', {}).get('id', 'NO ID')
    rpm = m.get('litellm_params', {}).get('rpm', 'NOT SET')
    tpm = m.get('litellm_params', {}).get('tpm', 'NOT SET')
    print(f'{name}: model_id={model_id}  rpm={rpm}  tpm={tpm}')
"
```

**Field mapping:**

| Payload field | Stored as | UI reads | Router enforces |
|---------------|-----------|----------|-----------------|
| `litellm_params.rpm` | `litellm_params.rpm` | Yes (`litellm_params.rpm`) | Yes (pre-call RPM check) |
| `litellm_params.tpm` | `litellm_params.tpm` | Yes (`litellm_params.tpm`) | Yes (pre-call TPM check) |
| `model_info.rpm` | `model_info.rpm` + `litellm_params.rpm_limit` | No | Yes (via `rpm_limit`) |
| `model_info.tpm` | `model_info.tpm` + `litellm_params.tpm_limit` | No | Yes (via `tpm_limit`) |

To set RPM that is both enforced by the router and visible in the UI, use `litellm_params.rpm`. The `model_info.rpm` payload sets `rpm_limit` (which the router also enforces) but the UI does not display `rpm_limit`; it only displays `rpm`.

**Verifying after setting:**

```bash
curl -sk -X GET "$PROXY_BASE_URL/model/info" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
for m in data.get('data', []):
    name = m.get('model_name', '')
    lp = m.get('litellm_params', {})
    print(f'{name}: rpm={lp.get("rpm", "NOT SET")}  tpm={lp.get("tpm", "NOT SET")}')
"
```

**How the router enforces RPM/TPM:**

At request time, the router performs a pre-call check on the selected deployment. It reads the deployment's `rpm`/`tpm` (or `rpm_limit`/`tpm_limit`) from `litellm_params`, checks the current rolling-window counter, and raises a `RateLimitError` if the limit would be exceeded. The deployment is then skipped (or the request fails if no other deployment is available). This is separate from key-level and team-level `rpm_limit`/`tpm_limit`, which are enforced by the `DynamicRateLimitHandler`.

### POST `/model/delete` ; Delete a Model

**Request body:** `ModelDeleteRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | `str` | Yes | The LiteLLM model UUID to delete. |

### GET `/model/info` ; List Model Info with Pricing

Returns all registered models with pricing, provider, and configuration details.

**Query params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `litellm_model_name` | `Optional[str]` | No | Filter by LiteLLM model name. |

**Example:**

```bash
curl -X GET 'http://localhost:4000/model/info' \
  -H 'Authorization: Bearer {{ master_key }}'
```

### GET `/model_group/info` ; Model Group Info

Returns pricing, mode, and capabilities per model group (model_name).

**Example:**

```bash
curl -X GET 'http://localhost:4000/model_group/info' \
  -H 'Authorization: Bearer {{ master_key }}'
```

### Model Pricing / Cost Map

LiteLLM maintains a built-in cost map (`model_prices_and_context_window.json`) with pricing for all known models. Custom pricing can be set per-deployment via `input_cost_per_token` / `output_cost_per_token` on the `Deployment` model.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/reload/model_cost_map` | POST | Reload the cost map from source |
| `/schedule/model_cost_map_reload` | POST | Schedule a reload |
| `/schedule/model_cost_map_reload` | DELETE | Cancel scheduled reload |
| `/schedule/model_cost_map_reload/status` | GET | Get reload status |
| `/model/cost_map/source` | GET | Get the cost map source (url or file) |

---

## 6. Fallback Management

**Source:** `litellm/proxy/management_endpoints/fallback_management_endpoints.py`

> **Database requirement:** All three endpoints require `STORE_MODEL_IN_DB=True` and a connected Prisma database. If either is missing, they return `400`.

### Endpoint Summary

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/fallback` | Create or update fallbacks for a model |
| GET | `/fallback/{model}` | Get fallbacks for a model |
| DELETE | `/fallback/{model}` | Delete fallbacks for a model |

### Important: Path Parameter Bug with Model Names Containing Slashes

The GET and DELETE endpoints use `/fallback/{model}` where `{model}` is a standard FastAPI path parameter. FastAPI path parameters do **not** match slashes by default. Model names that contain a `/` (e.g. `zai-org/GLM-5.2-FP8`, `openai/gpt-4o`, `azure/my-deployment`) will **not** match the route and will return a bare `404 {"detail":"Not Found"}` (FastAPI's default unmatched-route response, not a custom error from the handler).

The POST endpoint is unaffected because it takes the model name in the JSON request body, not the URL path.

**Affected:** `GET /fallback/{model}` and `DELETE /fallback/{model}` for any model name containing `/`.

**Not affected:** `POST /fallback` (body-based), and GET/DELETE for model names without slashes (e.g. `gpt-4o`).

### Correct Approach for Setting Fallbacks

Always use `POST /fallback` to create or update fallbacks. It accepts the model name in the JSON body, so slashes are not an issue.

```bash
curl -sk -X POST "$PROXY_BASE_URL/fallback" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "zai-org/GLM-5.2-FP8",
    "fallback_models": ["zai-org/GLM-5.1-FP8", "MiniMaxAI/MiniMax-M3-MXFP8"],
    "fallback_type": "general"
  }'
```

### Correct Approach for Verifying Fallbacks

Since `GET /fallback/{model}` may return 404 for model names with slashes, use these endpoints instead to verify that fallbacks are applied:

**1. `GET /router/settings` ; returns `current_values.fallbacks`:**

```bash
curl -sk -X GET "$PROXY_BASE_URL/router/settings" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
for field in data.get('fields', []):
    if field['field_name'] == 'fallbacks':
        print(json.dumps(field['field_value'], indent=2))
"
```

This returns the live fallback configuration from the in-memory router instance, e.g.:

```json
[{"zai-org/GLM-5.2-FP8": ["zai-org/GLM-5.1-FP8", "MiniMaxAI/MiniMax-M3-MXFP8"]}]
```

**2. `GET /get/config/callbacks` ; returns `router_settings.fallbacks`:**

```bash
curl -sk -X GET "$PROXY_BASE_URL/get/config/callbacks" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(json.dumps(data.get('router_settings', {}).get('fallbacks', []), indent=2))
"
```

**3. UI:** Global fallbacks are visible in the Admin UI under **Models** page > **Fallbacks** tab. They are NOT visible in the per-team Router Settings panel, because global fallbacks are stored in the `litellm_config` DB table and the in-memory router, not in the team's `router_settings` field. Per-team fallbacks (set via `router_settings` on `/team/new` or `/team/update`) would appear in the team detail page.

### POST `/fallback` ; Create or Update Fallbacks

**Request body:** `FallbackCreateRequest`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `model` | `str` | Yes | ; | Primary model name (must exist in router). |
| `fallback_models` | `List[str]` | Yes | ; | Fallback model names in priority order (min 1, no duplicates). |
| `fallback_type` | `Literal["general", "context_window", "content_policy"]` | No | `"general"` | Type of fallback. |

**Validation performed:**

1. `model` must be non-empty.
2. `fallback_models` must have at least one entry and no duplicates.
3. The primary `model` must exist in `llm_router.model_names`. Returns `404` with `available_models` if not.
4. Every model in `fallback_models` must exist in `llm_router.model_names`. Returns `400` with `available_models` for invalid ones.
5. The primary `model` cannot be its own fallback. Returns `400`.

**Behavior:** Maps `fallback_type` to config key (`general` -> `fallbacks`, `context_window` -> `context_window_fallbacks`, `content_policy` -> `content_policy_fallbacks`). Persists to `litellm_config` DB table (param_name=`router_settings`) and updates the in-memory router live via `setattr(llm_router, <fallback_key>, existing_fallbacks)`.

**Response:** `FallbackResponse`

| Field | Type | Description |
|-------|------|-------------|
| `model` | `str` | Model name. |
| `fallback_models` | `List[str]` | Fallback model names. |
| `fallback_type` | `str` | Type of fallback. |
| `message` | `str` | Success message. |

**Example:**

```bash
curl -sk -X POST "$PROXY_BASE_URL/fallback" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "fallback_models": ["claude-3-5-sonnet", "gpt-4o-mini"],
    "fallback_type": "general"
  }'
```

### GET `/fallback/{model}` ; Get Fallbacks

**Path param:** `model`

> **Warning:** This endpoint returns `404 {"detail":"Not Found"}` for model names containing `/` (e.g. `zai-org/GLM-5.2-FP8`). This is a FastAPI path parameter limitation, not a missing fallback. Use `GET /router/settings` or `GET /get/config/callbacks` to verify fallbacks instead. See [Important: Path Parameter Bug](#important-path-parameter-bug-with-model-names-containing-slashes).

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `fallback_type` | `Literal["general", "context_window", "content_policy"]` | `"general"` | Type to retrieve. |

Reads from the in-memory router via `get_all_fallbacks()` -> `getattr(llm_router, "fallbacks", [])` -> `get_fallback_model_group()`. Returns `404` if no fallbacks configured for the model.

**Example (works for model names without slashes):**

```bash
curl -sk -X GET "$PROXY_BASE_URL/fallback/gpt-4o?fallback_type=general" \
  -H "Authorization: Bearer $LITELLM_API_KEY"
```

### DELETE `/fallback/{model}` ; Delete Fallbacks

**Path param:** `model`

> **Warning:** Same slash limitation as GET. Use `POST /config/update` with `router_settings.fallbacks` to overwrite the full fallback list if you need to remove a fallback for a model with `/` in its name.

**Query params:** Same as GET.

Removes the fallback config from DB and updates the in-memory router. Returns `404` if not found.

**Example (works for model names without slashes):**

```bash
curl -sk -X DELETE "$PROXY_BASE_URL/fallback/gpt-4o?fallback_type=general" \
  -H "Authorization: Bearer $LITELLM_API_KEY"
```

### How Fallbacks Work at Request Time

When a request to the primary model fails (e.g. timeout, rate limit, 500 error), the router checks the `fallbacks` list for a matching entry. The lookup uses `get_fallback_model_group()` which checks: (1) exact model name match, (2) stripped model name match, (3) generic wildcard `*` fallback. If a match is found, the router tries each fallback model in order until one succeeds or all are exhausted (`max_fallbacks` controls the cap, default 5).

---

## 7. Router Settings

**Source:** `litellm/proxy/management_endpoints/router_settings_endpoints.py`, `litellm/proxy/proxy_server.py`

### Endpoint Summary

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/router/settings` | Get all router settings with current live values |
| GET | `/router/fields` | Get router field definitions (no values) |
| POST | `/config/update` | Update config including `router_settings` |
| GET | `/get/config/callbacks` | Get config including live router settings |

### GET `/router/settings` ; Get Router Settings

Returns all configurable router settings with metadata and their current live values.

**Response:** `RouterSettingsResponse`

| Field | Type | Description |
|-------|------|-------------|
| `fields` | `List[RouterSettingsField]` | All configurable settings with metadata. |
| `current_values` | `Dict[str, Any]` | Current values from router instance + config. |
| `routing_strategy_descriptions` | `Dict[str, str]` | Human-readable strategy descriptions. |

**Example:**

```bash
curl -X GET 'http://localhost:4000/router/settings' \
  -H 'Authorization: Bearer {{ master_key }}'
```

### POST `/config/update` ; Update Router Settings

**Auth:** `PROXY_ADMIN` only. Requires DB connected.

**Request body:** `ConfigYAML`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `router_settings` | `Optional[UpdateRouterConfig]` | No | Router settings to update. Merged with existing. |
| `model_list` | `Optional[List[ModelParams]]` | No | Model list. |
| `litellm_settings` | `Optional[dict]` | No | LiteLLM module settings. |
| `general_settings` | `Optional[ConfigGeneralSettings]` | No | General proxy settings. |
| `environment_variables` | `Optional[dict]` | No | Environment variables. |

**`UpdateRouterConfig` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `routing_strategy` | `Optional[str]` | `simple-shuffle`, `least-busy`, `usage-based-routing-v2`, `latency-based-routing`, `cost-based-routing`. |
| `routing_strategy_args` | `Optional[dict]` | Strategy arguments (e.g. `ttl`, `lowest_latency_buffer`). |
| `routing_groups` | `Optional[List[RoutingGroup]]` | Named subsets of models sharing a strategy. |
| `num_retries` | `Optional[int]` | Number of retries for failed requests. |
| `timeout` | `Optional[float]` | Request timeout in seconds. |
| `allowed_fails` | `Optional[int]` | Failures before cooldown. |
| `cooldown_time` | `Optional[float]` | Cooldown duration (seconds). |
| `max_retries` | `Optional[int]` | Max retries. |
| `retry_after` | `Optional[float]` | Min seconds to wait before retrying. |
| `fallbacks` | `Optional[List[dict]]` | General fallback mappings. |
| `context_window_fallbacks` | `Optional[List[dict]]` | Context-window-error fallback mappings. |
| `model_group_retry_policy` | `Optional[dict]` | Per-model-group retry policy. |
| `model_group_alias` | `Optional[Dict[str, Union[str, Dict]]]` | Model group aliases. |

**Example:**

```bash
curl -X POST 'http://localhost:4000/config/update' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{
    "router_settings": {
      "num_retries": 5,
      "timeout": 30,
      "allowed_fails": 3,
      "cooldown_time": 60,
      "routing_strategy": "latency-based-routing"
    }
  }'
```

### Router Settings Field Reference

Full list of fields exposed by `GET /router/settings`, defined in `ROUTER_SETTINGS_FIELDS`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `routing_strategy` | String | `simple-shuffle` | Load-balancing strategy |
| `routing_strategy_args` | Dictionary | `{}` | Strategy arguments |
| `routing_groups` | List | `[]` | Named model subsets with their own strategy |
| `num_retries` | Integer | `0` | Retries for failed requests |
| `timeout` | Float | `null` | Request timeout (seconds) |
| `stream_timeout` | Float | `null` | Streaming request timeout |
| `max_fallbacks` | Integer | `5` | Max fallbacks before exiting |
| `fallbacks` | List | `[]` | General fallback mappings |
| `context_window_fallbacks` | List | `[]` | Context-window-error fallbacks |
| `content_policy_fallbacks` | List | `[]` | Content-policy-error fallbacks |
| `allowed_fails` | Integer | `null` | Failures before cooldown |
| `cooldown_time` | Float | `null` | Cooldown duration |
| `retry_after` | Integer | `0` | Min wait before retry |
| `retry_policy` | Dictionary | `null` | Custom retry policy per exception type |
| `model_group_alias` | Dictionary | `{}` | Model group aliases |
| `enable_pre_call_checks` | Boolean | `false` | Pre-call checks before routing |
| `default_litellm_params` | Dictionary | `null` | Default params for router |
| `set_verbose` | Boolean | `false` | Verbose router logging |
| `default_max_parallel_requests` | Integer | `null` | Default max parallel requests |
| `enable_tag_filtering` | Boolean | `false` | Tag-based routing |
| `tag_filtering_match_any` | Boolean | `true` | Match any vs all tags |
| `disable_cooldowns` | Boolean | `null` | Disable cooldown mechanism |

### Available Routing Strategies

| Strategy | Description |
|----------|-------------|
| `simple-shuffle` | Random deployment selection. Simple and fast. |
| `least-busy` | Routes to the deployment with the fewest ongoing requests. |
| `usage-based-routing-v2` | Routes to the deployment with the lowest TPM usage. |
| `latency-based-routing` | Routes to the deployment with the lowest latency over a sliding window. |
| `cost-based-routing` | Routes to the deployment with the lowest cost per token. |

---

## 8. User Management

**Source:** `litellm/proxy/management_endpoints/internal_user_endpoints.py`

### Endpoint Summary

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/user/new` | Create an internal user |
| GET | `/user/info` | Get user detail (keys + team memberships) |
| GET | `/v2/user/info` | Lightweight user info |
| GET | `/user/list` | Paginated, filterable user list |
| POST | `/user/update` | Update a user |
| POST | `/user/bulk_update` | Update multiple users |
| POST | `/user/delete` | Delete users + associated keys |
| GET | `/user/available_roles` | List available roles |
| GET | `/user/filter/ui` | Proxy-admin-only paginated search |
| GET | `/user/daily/activity` | Daily spend per user |
| GET | `/user/daily/activity/aggregated` | Aggregated daily spend |

### POST `/user/new` ; Create an Internal User

**Auth:** `PROXY_ADMIN` or `ORG_ADMIN`.

**Request body:** `NewUserRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | `string` | No | Auto-generated if omitted. |
| `user_alias` | `string` | No | Descriptive name. |
| `user_email` | `string` | No | Must be unique. |
| `user_role` | `string` | No | `proxy_admin`, `proxy_admin_viewer`, `internal_user`, `internal_user_viewer`. Only proxy admins can create admin roles. |
| `teams` | `list[string] | list[object]` | No | Team IDs or `NewUserRequestTeam` objects. |
| `organizations` | `list[string]` | No | Organization IDs. |
| `max_budget` | `float` | No | Max budget (USD). |
| `soft_budget` | `float` | No | Soft budget; alerts fire but requests are not blocked. |
| `budget_duration` | `string` | No | Reset period (`"30s"`, `"30m"`, `"30h"`, `"30d"`, `"1mo"`). |
| `budget_limits` | `list[object]` | No | Concurrent budget windows. |
| `tpm_limit` | `int` | No | TPM limit. |
| `rpm_limit` | `int` | No | RPM limit. |
| `models` | `list[string]` | No | Allowed models. |
| `auto_create_key` | `bool` | No | Whether to auto-generate a key. Default `true`. |
| `metadata` | `dict` | No | Arbitrary metadata. |

**Example:**

```bash
curl -X POST 'http://localhost:4000/user/new' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{
    "user_alias": "Jane Developer",
    "user_email": "jane@acme.com",
    "user_role": "internal_user",
    "teams": ["team-123"],
    "max_budget": 200.0,
    "budget_duration": "30d"
  }'
```

#### Strategy: How to Create a New User

Use `POST /user/new` to provision a person who needs to log into the Admin UI
and/or mint their own API keys. Follow this checklist so the user gets the
right access, the right guardrails, and a usable credential on the first call.

**1. Decide the role.** `user_role` controls what the user can see and do.

| Value | Can do | Use for |
|-------|--------|---------|
| `internal_user` | login, create/view/delete own keys, view own spend | Default for a normal developer |
| `internal_user_viewer` | login, view own keys, view own spend | Read-only humans |
| `proxy_admin_viewer` | login, view all keys + spend | Auditors, ops dashboards |
| `proxy_admin` | all permissions | Service/admin accounts (rare) |

Only a `PROXY_ADMIN` can create a `proxy_admin` or `proxy_admin_viewer`. For an
ordinary person pick `internal_user`.

**2. Set login credentials.** `NewUserRequest` does **not** accept a `password`
field, so a user created via `/user/new` starts passwordless and cannot log in
until one is set. Set the password with `POST /user/update` right after
creation (see the working example below). A user `password` authenticates that
specific account; `UI_USERNAME`/`UI_PASSWORD` govern only the top-level login.
The proxy hashes it server side. Alternatively provide `sso_user_id` so the user
authenticates via SSO instead of a password.

**3. Choose `auto_create_key`.**

- `true` (default): the response also returns a fresh `sk-...` key, so the user
  has a working credential immediately.
- `false`: only the user row is created. Use this if the user must not hold a
  key yet, or if key creation happens separately.

**4. Scope model access.** `models: []` (the default) means "all models".
To restrict, list explicit model names. To allow the user to call **no** models
outright (only inherit access from a team), set `models: ["no-default-models"]`.

**5. Put them in a team/org.** Pass `teams` (list of IDs, or
`[{"team_id": "...", "user_role": "admin"|"user", "max_budget_in_team": n}]`) and
`organizations` so the user inherits the team's model list, budgets, and
priority. Team metadata priority overrides key-level priority.

**6. Apply budget & rate limits.** `max_budget` + `budget_duration` cap spend
(e.g. `200` USD per `30d`); `soft_budget` alerts before the hard cap; `tpm_limit`
/ `rpm_limit` cap throughput. Set what is needed on day one.

**7. Capture the response.** Save `user_id` and the returned `key`. `user_id` is
the stable identifier for all subsequent `/user/update` and spend lookups.

**Working example (ADEO gateway):**

Create the user (role `internal_user`, auto-created key):

```bash
curl -sk -X POST "$PROXY_BASE_URL/user/new" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_email": "adeogpt@ecouncil.ae",
    "user_alias": "ADEOGPT",
    "user_role": "internal_user",
    "auto_create_key": true
  }'
```

Then set a password so the account can actually log in to the Admin UI:

```bash
curl -sk -X POST "$PROXY_BASE_URL/user/update" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "<user_id_from_response>",
    "password": "REPLACE_WITH_A_STRONG_PASSWORD"
  }'
```

The `/user/new` response returns `user_id`, `user_role`, `user_alias`,
`user_email`, and an auto-created `key`. Save the `user_id`; it is the handle
for `/user/update` and `/user/info`. The account can log in only after the
`/user/update` password step above succeeds.

**Verify after creation** with `GET /user/info?user_id=<id>` (shows the user's
keys and memberships). Confirm the password took effect by logging in via
`POST /v2/login` with the username and password. Rotate the auto-created key
with `/key/regenerate` or `/key/update` if it was exposed.

### POST `/user/update` ; Update a User

Same fields as `/user/new` plus `user_id` (required).

### POST `/user/delete` ; Delete Users

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `user_ids` | `List[str]` | Yes | User IDs to delete. |

Cascades: deletes associated API keys and team memberships.

### GET `/user/info` ; Get User Detail

**Query params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | `str` | No | User ID. Defaults to caller's own. |

Returns user row, all API keys, and team memberships.

### GET `/user/list` ; List Users

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `page` | `int` | `1` | Page number. |
| `size` | `int` | `10` | Items per page. |
| `user_role` | `Optional[str]` | ; | Filter by role. |
| `team_id` | `Optional[str]` | ; | Filter by team. |
| `organization_id` | `Optional[str]` | ; | Filter by org. |
| `sort_by` | `Optional[str]` | ; | Sort field. |
| `sort_order` | `Optional[str]` | `asc` | Sort direction. |

---

## 9. Budget Management

**Source:** `litellm/proxy/management_endpoints/budget_management_endpoints.py`

### Endpoint Summary

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/budget/new` | Create a budget |
| POST | `/budget/update` | Update a budget |
| POST | `/budget/info` | Query budgets by ID list |
| POST | `/budget/delete` | Delete a budget (proxy_admin only) |
| GET | `/budget/list` | List all budgets (admin view only) |
| GET | `/budget/settings` | Get configurable budget params |

### POST `/budget/new` ; Create a Budget

**Auth:** `PROXY_ADMIN`.

**Request body:** `BudgetNewRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `max_budget` | `Optional[float]` | No | Hard USD spend cap. |
| `soft_budget` | `Optional[float]` | No | Alert-only threshold. |
| `tpm_limit` | `Optional[int]` | No | TPM limit. |
| `rpm_limit` | `Optional[int]` | No | RPM limit. |
| `model_max_budget` | `Optional[dict]` | No | Per-model max budget. |
| `budget_duration` | `Optional[str]` | No | Reset window (`"1d"`, `"30d"`). |
| `budget_limits` | `Optional[List[BudgetLimitEntry]]` | No | Multiple concurrent budget windows. |

**Example:**

```bash
curl -X POST 'http://localhost:4000/budget/new' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{
    "max_budget": 5000.0,
    "tpm_limit": 1000000,
    "rpm_limit": 10000,
    "budget_duration": "30d"
  }'
```

### POST `/budget/update` ; Update a Budget

Same fields as `/budget/new` plus `budget_id` (required).

### POST `/budget/info` ; Query Budgets

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `budgets` | `List[str]` | Yes | Budget IDs to query. |

### POST `/budget/delete` ; Delete a Budget

**Auth:** `PROXY_ADMIN` only.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | `str` | Yes | Budget ID to delete. |

### GET `/budget/list` ; List All Budgets

**Auth:** Admin view only (`proxy_admin` or `proxy_admin_viewer`).

### GET `/budget/settings` ; Get Configurable Params

Returns the list of fields that can be set on a budget, for UI rendering.

---

## 10. Spend and Cost Tracking

**Source:** `litellm/proxy/proxy_server.py` and spend tracking modules.

### Endpoint Summary

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/spend/logs` | Legacy spend logs (deprecated, use v2) |
| GET | `/spend/logs/v2` | Paginated spend logs with rich filtering |
| GET | `/spend/logs/ui/{request_id}` | Get a specific log entry |
| GET | `/spend/logs/session/ui` | Get logs by session ID |
| POST | `/spend/calculate` | Calculate cost before/after a call |
| GET | `/spend/keys` | Spend by API key |
| GET | `/spend/users` | Spend by user |
| GET | `/spend/tags` | Spend by tag |
| GET | `/global/spend` | Total spend across proxy |
| GET | `/global/spend/logs` | Daily spend (30d, uses DB views) |
| GET | `/global/spend/keys` | Top-N spend by API key |
| GET | `/global/spend/teams` | Top-N spend by team |
| GET | `/global/spend/models` | Top-N spend by model |
| GET | `/global/spend/provider` | Top-N spend by provider |
| GET | `/global/spend/report` | Detailed daily report grouped by team/customer/api_key |
| GET | `/global/spend/tags` | Spend by tag (global) |
| GET | `/global/spend/all_tag_names` | List all tag names |
| POST | `/global/spend/end_users` | Spend by end user |
| POST | `/global/spend/reset` | Reset all spend counters |
| POST | `/global/spend/refresh` | Refresh materialized spend views |
| GET | `/global/activity` | Global activity metrics |
| GET | `/global/activity/model` | Activity per model |
| GET | `/global/activity/exceptions` | Exception counts |
| GET | `/global/activity/exceptions/deployment` | Exceptions per deployment |
| GET | `/global/all_end_users` | List all end users |
| GET | `/provider/budgets` | Provider budget routing status |

### GET `/spend/logs/v2` ; Paginated Spend Logs

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `api_key` | `Optional[str]` | ; | Filter by API key. |
| `user_id` | `Optional[str]` | ; | Filter by user. |
| `model` | `Optional[str]` | ; | Filter by model. |
| `start_date` | `Optional[str]` | ; | ISO date string. |
| `end_date` | `Optional[str]` | ; | ISO date string. |
| `page` | `int` | `1` | Page number. |
| `size` | `int` | `10` | Items per page. |
| `request_id` | `Optional[str]` | ; | Filter by request ID. |
| `team_id` | `Optional[str]` | ; | Filter by team. |
| `session_id` | `Optional[str]` | ; | Filter by session. |
| `customer_id` | `Optional[str]` | ; | Filter by customer. |
| `tags` | `Optional[str]` | ; | Filter by tags (JSON array). |

**Example:**

```bash
curl -X GET 'http://localhost:4000/spend/logs/v2?start_date=2026-07-01&end_date=2026-07-10&page=1&size=20' \
  -H 'Authorization: Bearer {{ master_key }}'
```

### POST `/spend/calculate` ; Calculate Spend

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `model` | `str` | Yes | Model name. |
| `messages` or `prompt` | ; | Yes | Messages or prompt text. |
| `completion_response` | `Optional[dict]` | No | For post-call calculation. |

### GET `/global/spend` ; Total Spend

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `start_date` | `Optional[str]` | ; | ISO date string. |
| `end_date` | `Optional[str]` | ; | ISO date string. |

**Example:**

```bash
curl -X GET 'http://localhost:4000/global/spend?start_date=2026-07-01&end_date=2026-07-10' \
  -H 'Authorization: Bearer {{ master_key }}'
```

### GET `/global/spend/models` ; Spend by Model

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `start_date` | `Optional[str]` | ; | ISO date string. |
| `end_date` | `Optional[str]` | ; | ISO date string. |
| `limit` | `int` | `10` | Top N models. |

---

## 11. Cost Tracking Configuration

**Source:** `litellm/proxy/proxy_server.py`

### Endpoint Summary

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/config/cost_discount_config` | Get provider discount configuration |
| PATCH | `/config/cost_discount_config` | Update provider discount configuration |
| GET | `/config/cost_margin_config` | Get provider margin configuration |
| PATCH | `/config/cost_margin_config` | Update provider margin configuration |
| POST | `/cost/estimate` | Cost estimation with margins/discounts applied |

### POST `/cost/estimate` ; Estimate Cost

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `model` | `str` | Yes | Model name. |
| `messages` or `prompt` | ; | Yes | Messages or prompt. |

Returns estimated cost with any configured discounts and margins applied.

---

## 12. Hierarchical Router Settings

Router settings are applied hierarchically at request time, resolved in priority order: **Key > Team > Global**.

### Global Router Settings

Come from the config file / DB `router_settings` row. Applied to the `Router` instance at config-load time via `llm_router.update_settings(...)`. Updated at runtime via `POST /config/update`.

### Per-Key Router Settings

Stored on the key object. Set via `router_settings` field on `/key/generate` and `/key/update`.

**Example:**

```bash
curl -X POST 'http://localhost:4000/key/generate' \
  -H 'Authorization: Bearer {{ master_key }}' \
  -H 'Content-Type: application/json' \
  -d '{
    "router_settings": {
      "model_group_retry_policy": {"max_retries": 5}
    }
  }'
```

### Per-Team Router Settings

Stored on the team object. Set via `router_settings` field on `/team/new` and `/team/update`.

### Resolution at Request Time

`ProxyConfig._get_hierarchical_router_settings()` looks up `router_settings` in priority order: Key > Team > Global. Per-key and per-team settings are not persisted to the `litellm_config` table; they live on the key/team rows in the database.

### `Router.update_settings()` Allowed Settings

When settings are applied to the router instance, only these keys are accepted (others are silently ignored):

`routing_strategy_args`, `routing_strategy`, `routing_groups`, `allowed_fails`, `cooldown_time`, `num_retries`, `timeout`, `max_retries`, `retry_after`, `fallbacks`, `context_window_fallbacks`, `model_group_retry_policy`, `model_group_alias`, `enable_weighted_failover`

Integer-cast settings: `timeout`, `num_retries`, `retry_after`, `allowed_fails`, `cooldown_time`.

When `routing_strategy` changes, `routing_strategy_init(...)` is re-run and routing groups are rebuilt.
