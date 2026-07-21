# Athena Read-Only Key Guide

`athena-read-only` is a read-only administrative key on the ADEO LiteLLM proxy. It is backed by a user with the `proxy_admin_viewer` role, which grants read parity with `proxy_admin` across every data surface (keys, teams, organizations, users, models, spend, logs, audits, activity) while hard-blocking every write and every cost-incurring inference route.

Use it for dashboards, auditors, export jobs, monitoring scrapers, and any integration that only needs to observe proxy state. It cannot create, update, or delete anything, and it cannot call LLM inference, so it cannot accrue spend.

## Connection Details

| Setting | Value |
|---------|-------|
| Proxy base URL | `https://litellm.adeoaiengine.ecouncil.ae` |
| TLS | Self-signed. Every HTTP client must skip verification (`curl -k`, `requests.verify=False`, `NODE_TLS_REJECT_UNAUTHORIZED=0`, etc.) |
| Key alias | `athena-read-only` |
| Key value | `sk-nUVF9ruGsSRqe7xOthTU1Q` (treat as a secret; rotate via a proxy admin key if leaked) |
| Backing user email | `athena-read-only@adeo.local` |
| Backing user id | `55420cc7-9b17-468d-8f2f-3d22cb29d7a8` |
| User role | `proxy_admin_viewer` |

The `athena-read-only` key itself is what you use day to day. Key rotation/deletion and backing-user management require a separate proxy admin key, which is outside the scope of this guide.

## Authentication

Every request must carry the key in the `Authorization` header:

```bash
export PROXY_BASE_URL="https://litellm.adeoaiengine.ecouncil.ae"
export ATHENA_KEY="sk-nUVF9ruGsSRqe7xOthTU1Q"

curl -sk -X GET "$PROXY_BASE_URL/key/list?page=1&size=5" \
  -H "Authorization: Bearer $ATHENA_KEY"
```

For Python clients:

```python
import requests
resp = requests.get(
    f"{PROXY_BASE_URL}/key/list",
    params={"page": 1, "size": 5},
    headers={"Authorization": f"Bearer {ATHENA_KEY}"},
    verify=False,  # self-signed cert
)
```

## Logging in to the Admin UI as the athena user

The LiteLLM Admin UI (`/ui/`) supports two login paths. The athena user can use either.

### Option A: API key login (simplest)

The UI accepts an API key directly on the login screen. Paste the `athena-read-only` key value (`sk-nUVF9ruGsSRqe7xOthTU1Q`) into the "API Key" / "Login with API Key" field. The UI will authenticate as the `proxy_admin_viewer` user and render every read-only page (Keys, Teams, Organizations, Users, Models, Spend, Logs, Activity, Audit, Settings). Write buttons and the Playground are hidden or return 403 on click.

### Option B: Username/password login

The athena user was created without a password. To use the email/password form, set a password first. The viewer role is explicitly allowed to update its own `password` (and `user_email` only); any other field is rejected.

```bash
curl -sk -X POST "$PROXY_BASE_URL/user/update" \
  -H "Authorization: Bearer $ATHENA_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "55420cc7-9b17-468d-8f2f-3d22cb29d7a8",
    "password": "REPLACE_WITH_A_STRONG_PASSWORD"
  }'
```

Then log in at `https://litellm.adeoaiengine.ecouncil.ae/ui/` with:

| Field | Value |
|-------|-------|
| Username | `athena-read-only@adeo.local` |
| Password | the password you set above |

On success the proxy issues a short-lived JWT (session duration from `LITELLM_UI_SESSION_DURATION`) scoped to `proxy_admin_viewer`.

### Option C: SSO

If `general_settings.ui_sso` is configured on the proxy, the athena user can also be mapped from an SSO identity via `user_allowed_roles` / role mappings in `ui_sso`. This is not set up by default on ADEO; contact the proxy admin if SSO is required.

## What the key can access

Enforcement lives in `litellm/proxy/auth/route_checks.py::_check_proxy_admin_viewer_access`. The rule set is:

1. All LLM/inference routes are blocked (403) because they cost money.
2. Any safe HTTP method (`GET`, `HEAD`, `OPTIONS`) on a non-inference route is allowed by default. This is the read-parity guarantee: every read endpoint the proxy exposes is covered, including ones added in future versions, without needing an allowlist update. The path tables below list the known endpoints as of this proxy build; they are illustrative, not exhaustive.
3. `POST` routes that are semantically reads are allowed via the `admin_viewer_routes` / `global_spend_tracking_routes` / `spend_tracking_routes` allowlists.
4. Everything else (writes, deletes, regenerations, blocks) is blocked (403).
5. The single write exception is `/user/update` restricted to `user_email` and `password` only, for self-service.

### API Keys (virtual keys)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/key/info` | Info for one key (or self if `key` omitted) |
| POST | `/v2/key/info` | Info for multiple keys (admin-level read) |
| GET | `/key/list` | List keys with pagination/filtering |
| GET | `/key/aliases` | List key aliases with pagination/search |
| GET | `/key/health` | Check key health (logging callbacks) |

Blocked: `/key/generate`, `/key/update`, `/key/bulk_update`, `/team/key/bulk_update`, `/key/delete`, `/key/{key}/regenerate`, `/key/regenerate`, `/key/{key}/reset_spend`, `/key/block`, `/key/unblock`, `/key/service-account/generate`, `/credentials/migrate-encryption` (POST), `/credentials/migrate-encryption/check` (GET, blocked).

### Teams

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/team/info` | One team |
| GET | `/team/list` | List teams |
| GET | `/v2/team/list` | List teams (v2) |
| GET | `/team/available` | Teams available to the caller |
| GET | `/team/permissions_list` | Team permissions list |
| GET | `/team/daily/activity` | Team daily activity (requires start/end date params) |
| GET | `/team/filter/ui` | UI team filter helper |

Blocked: `/team/new`, `/team/update`, `/team/delete`, `/team/block`, `/team/unblock`, `/team/permissions_update`, `/team/permissions_bulk_update`, `/team/member_add`, `/team/member_delete`, `/team/member_update`, `/team/bulk_member_add`, `/team/model/add`, `/team/model/delete`.

### Organizations

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/organization/list` | List organizations |
| GET | `/organization/info?organization_id=...` | One organization with members |
| GET | `/organization/daily/activity` | Organization daily activity (requires start/end date params) |

Blocked: `/organization/new`, `/organization/update`, `/organization/delete`, `/organization/member_add`, `/organization/member_update`, `/organization/member_delete`.

### Users

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/user/info` | Self user info |
| GET | `/v2/user/info` | User info (v2) |
| GET | `/user/list` | List users |
| GET | `/user/available_roles` | Available roles |
| GET | `/user/filter/ui` | UI user filter helper |
| GET | `/user/daily/activity` | User daily activity (requires start/end date params) |
| GET | `/user/daily/activity/aggregated` | Aggregated user activity (requires start/end date params) |
| POST | `/user/update` | Self-service only: `user_email` and/or `password`. Any other field returns 403 |

Blocked: `/user/new`, `/user/bulk_update`, `/user/delete`.

### Models

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/model/info` | Model info |
| GET | `/v1/model/info` | Model info (v1 alias) |
| GET | `/v2/model/info` | Model info (v2) |
| GET | `/model_group/info` | Model group info |
| GET | `/models` | List models |
| GET | `/v1/models` | List models (v1) |
| GET | `/models/{model_id}` | Retrieve a model by ID |
| GET | `/v1/models/{model_id}` | Retrieve a model by ID (v1) |
| GET | `/model/cost_map/source` | Model cost map source |
| GET | `/schedule/model_cost_map_reload/status` | Cost map reload status |

Blocked: `/model/new`, `/model/update`, `/model/delete`.

### Spend and cost tracking

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/spend/keys` | Spend per key |
| POST | `/spend/users` | Spend per user |
| POST | `/spend/tags` | Spend per tag |
| POST | `/spend/calculate` | Calculate spend |
| GET | `/spend/logs` | Spend logs |
| GET | `/spend/logs/v2` | Spend logs (v2) |
| GET | `/spend/logs/ui` | Spend logs (UI shape) |
| GET | `/spend/logs/ui/{request_id}` | Single log detail (UI drawer) |
| GET | `/spend/logs/session/ui` | Session logs (UI) |
| POST | `/cost/estimate` | Cost estimate |
| GET | `/provider/budgets` | Provider budgets (may 500 if no provider configured) |

### Global spend (proxy-wide)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/global/spend/logs` | All spend logs |
| GET | `/global/spend` | Aggregate global spend |
| GET | `/global/spend/keys` | Spend by key |
| GET | `/global/spend/teams` | Spend by team |
| GET | `/global/spend/end_users` | Spend by end user |
| GET | `/global/spend/models` | Spend by model |
| GET | `/global/spend/tags` | Spend by tag |
| GET | `/global/spend/all_tag_names` | All tag names |
| GET | `/global/spend/report` | Spend report |
| GET | `/global/spend/provider` | Spend by provider |
| GET | `/global/all_end_users` | All end users seen globally |
| GET | `/global/predict/spend/logs` | Predicted spend |
| POST | `/global/spend/reset` | BLOCKED (proxy admin only) |

### Activity and audit

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/global/activity` | Global activity (requires start/end date params) |
| GET | `/global/activity/model` | Activity by model (requires start/end date params) |
| GET | `/global/activity/cache_hits` | Cache hit activity |
| GET | `/global/activity/exceptions` | Activity exceptions |
| GET | `/global/activity/exceptions/deployment` | Activity exceptions by deployment |
| GET | `/team/daily/activity` | Team daily activity (requires start/end date params) |
| GET | `/user/daily/activity` | User daily activity (requires start/end date params) |
| GET | `/user/daily/activity/aggregated` | Aggregated user activity (requires start/end date params) |
| GET | `/organization/daily/activity` | Organization daily activity (requires start/end date params) |
| GET | `/customer/daily/activity` | Customer daily activity (requires start/end date params) |
| GET | `/end_user/daily/activity` | End-user daily activity (requires start/end date params) |
| GET | `/tag/daily/activity` | Tag daily activity (requires start/end date params) |
| GET | `/agent/daily/activity` | Agent daily activity (requires start/end date params) |
| GET | `/tag/list` | List tags |
| GET | `/audit` | Audit log |
| GET | `/audit/{id}` | Single audit entry |

### Customers / end users

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/customer/list` | List customers |
| GET | `/customer/info?user_id=...` | One customer |
| GET | `/customer/daily/activity` | Customer daily activity (requires start/end date params) |
| GET | `/end_user/list` | List end users |
| GET | `/end_user/info?end_user_id=...` | One end user |
| GET | `/end_user/daily/activity` | End-user daily activity (requires start/end date params) |

### Settings, config, and observability (read-only views)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/callbacks/list` | Callbacks list |
| GET | `/callbacks/configs` | Callback configs |
| GET | `/get/config/callbacks` | Callback config (alt) |
| GET | `/active/callbacks` | Active (running) callbacks |
| GET | `/alerting/settings` | Alerting settings |
| GET | `/email/event_settings` | Email event settings |
| GET | `/config/list` | Config list |
| GET | `/config/field/info` | Config field info |
| GET | `/config/yaml` | Rendered config YAML (public route) |
| GET | `/config/override/list` | Config override list |
| GET | `/config/override/info` | Config override info |
| GET | `/config/cost_discount_config` | Cost discount config |
| GET | `/config/cost_margin_config` | Cost margin config |
| GET | `/config/pass_through_endpoint` | Pass-through endpoint config |
| GET | `/config/pass_through_endpoint/team/{team_id}` | Pass-through endpoint config for a team |
| GET | `/budget/list` | Budget list |
| GET | `/budget/settings?budget_id=...` | Budget settings |
| GET | `/invitation/info` | Invitation info |
| GET | `/guardrails/list` | Guardrails list |
| GET | `/v2/guardrails/list` | Guardrails list (v2) |
| GET | `/guardrails/submissions` | Guardrail submissions |
| GET | `/guardrails/submissions/{guardrail_id}` | One guardrail's submissions |
| GET | `/guardrails/{guardrail_id}/info` | One guardrail's info |
| GET | `/guardrails/usage/overview` | Guardrail usage overview |
| GET | `/guardrails/usage/logs` | Guardrail usage logs |
| GET | `/guardrails/usage/detail/{guardrail_id}` | Guardrail usage detail |
| GET | `/guardrails/ui/provider_specific_params` | Provider-specific guardrail params |
| GET | `/guardrails/ui/major_airlines` | Major-airlines list (guardrail UI helper) |
| GET | `/guardrails/ui/add_guardrail_settings` | Guardrail UI settings |
| GET | `/guardrails/ui/category_yaml/{category_name}` | Category YAML for a guardrail |
| GET | `/policies/attachments/list` | Policy attachments |
| GET | `/policies/attachments/{attachment_id}` | One policy attachment |
| GET | `/policies/usage/overview` | Policy usage overview |
| GET | `/policies/compare` | Compare policies (requires query params) |
| GET | `/policies/list` | List policies |
| GET | `/policies/{policy_id}` | One policy |
| GET | `/policies/{policy_id}/resolved-guardrails` | Resolved guardrails for a policy |
| GET | `/policies/name/{policy_name}/versions` | Policy versions by name |
| GET | `/policy/list` | List policies (legacy policy module) |
| GET | `/policy/templates` | Policy templates (legacy policy module) |
| GET | `/get/mcp_semantic_filter_settings` | MCP semantic filter settings |
| GET | `/cache/settings` | Cache settings |
| GET | `/cache/ping` | Cache ping (connectivity) |
| GET | `/cache/redis/info` | Redis cache info |
| GET | `/cost_tracking/settings` | Cost tracking settings |
| GET | `/cost_tracking/settings/model` | Per-model cost tracking settings |
| GET | `/fallbacks` | Fallback config |
| GET | `/fallback/{model}` | Fallback config for a specific model |
| GET | `/router/settings` | Router settings |
| GET | `/router/fields` | Router settings field info |
| GET | `/access_group/list` | Access group list |
| GET | `/access_group/{access_group}/info` | Access group info |
| GET | `/v1/access_group` | Access group (v1 API) |
| GET | `/v1/access_group/{access_group_id}` | One access group (v1 API) |
| GET | `/v1/unified_access_group` | Unified access group list |
| GET | `/v1/unified_access_group/{access_group_id}` | One unified access group |
| GET | `/model/access/group/list` | Model access group list |
| GET | `/model/access/group/info` | Model access group info |
| GET | `/model/settings` | Model settings |
| GET | `/model/streaming_metrics` | Streaming metrics per model |
| GET | `/adaptive_router/state` | Adaptive router state (only when adaptive router enabled) |
| GET | `/get/allowed_ips` | Allowed IPs list |
| GET | `/get/ui_settings` | UI settings |
| GET | `/get/ui_theme_settings` | UI theme settings |
| GET | `/get/sso_settings` | SSO settings |
| GET | `/get/default_team_settings` | Default team settings |
| GET | `/get/internal_user_settings` | Internal user settings |
| GET | `/settings` | Proxy settings |
| GET | `/credentials` | Credentials list |
| GET | `/credentials/by_model/{model_id}` | Credentials by model |
| GET | `/credentials/by_name/{credential_name}` | Credentials by name |
| GET | `/credentials/by_name/{credential_name}` | Credentials by name |
| GET | `/v1/memory` | In-memory key-value store listing |
| GET | `/v1/memory/{key}` | In-memory key-value store get |
| GET | `/memory-usage-in-mem-cache` | In-memory cache usage |
| GET | `/memory-usage-in-mem-cache-items` | In-memory cache items |
| GET | `/debug/memory/summary` | Memory debug summary |
| GET | `/debug/memory/details` | Memory debug details |
| GET | `/debug/asyncio-tasks` | Asyncio task debug |
| GET | `/otel-spans` | OpenTelemetry spans |
| GET | `/utils/supported_openai_params` | Supported OpenAI params (requires model query param) |
| GET | `/schedule/model_cost_map_reload/status` | Cost map reload status |
| GET | `/schedule/anthropic_beta_headers_reload/status` | Anthropic beta header reload status |

### Projects, prompts, tools, workflows, and search tools

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/project/list` | Project list |
| GET | `/project/info` | Project info |
| GET | `/prompts/list` | Prompt list |
| GET | `/prompts/{prompt_id}` | One prompt |
| GET | `/prompts/{prompt_id}/info` | Prompt info |
| GET | `/prompts/{prompt_id}/versions` | Prompt versions |
| GET | `/v1/tool/list` | List auto-discovered tools and their policies |
| GET | `/v1/tool/{tool_name}/detail` | Tool detail |
| GET | `/v1/tool/{tool_name}/logs` | Tool invocation logs |
| GET | `/v1/tool/{tool_name}/overrides` | Per-team/key tool overrides |
| GET | `/v1/tool/policy/options` | Available tool policy options |
| GET | `/search/tools` | Search tools list |
| GET | `/v1/search/tools` | Search tools list (v1) |
| GET | `/v1/workflows/runs` | Workflow run list |
| GET | `/v1/workflows/runs/{run_id}` | One workflow run |
| GET | `/v1/workflows/runs/{run_id}/events` | Events for a workflow run |
| GET | `/v1/workflows/runs/{run_id}/messages` | Messages for a workflow run |

Blocked: `/v1/tool/policy` (POST; sets tool policy), `/v1/tool/{tool_name}` (POST; updates tool).

### MCP servers (control-plane reads)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v1/mcp/server` | List MCP servers |
| GET | `/v1/mcp/server/{server_id}` | One MCP server |
| GET | `/v1/mcp/server/health` | MCP server health |
| GET | `/v1/mcp/server/submissions` | MCP server submissions |
| GET | `/v1/mcp/tools` | MCP tools across servers |
| GET | `/v1/mcp/toolset` | MCP toolsets |
| GET | `/v1/mcp/toolset/{toolset_id}` | One MCP toolset |
| GET | `/v1/mcp/discover` | Discover MCP resources |
| GET | `/v1/mcp/access_groups` | MCP access groups |
| GET | `/v1/mcp/user-credentials` | Current user's MCP credentials |
| GET | `/v1/mcp/user-env-vars/status` | User env-var status |
| GET | `/v1/mcp/registry.json` | MCP registry (JSON) |
| GET | `/v1/mcp/openapi-registry` | MCP OpenAPI registry |
| GET | `/v1/mcp/network/client-ip` | Client IP (network debug) |

Blocked: `/v1/mcp/server/register`, `/v1/mcp/server/{server_id}/approve`, `/v1/mcp/server/{server_id}/reject`, `/v1/mcp/server/{server_id}` (DELETE).

### Vector stores

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v1/vector_store/list` | List vector stores |
| GET | `/vector_store/list` | List vector stores (alt) |
| GET | `/v1/vector_stores` | List vector stores (OpenAI alias) |
| GET | `/vector_stores` | List vector stores (alt alias) |
| GET | `/v1/vector_stores/{vector_store_id}` | One vector store |
| GET | `/vector_stores/{vector_store_id}` | One vector store (alt) |

### Compliance

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/compliance/eu-ai-act` | EU AI Act compliance report |
| GET | `/compliance/gdpr` | GDPR compliance report |

### JWT key mapping (read)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/jwt/key/mapping/info` | JWT key mapping info |
| GET | `/jwt/key/mapping/list` | JWT key mapping list |

Blocked: `/jwt/key/mapping/new`, `/jwt/key/mapping/update`, `/jwt/key/mapping/delete`.

### Team callbacks (read)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/team/{team_id}/callback` | Callback settings for a team |

### Cloud cost export providers (read)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/cloudzero/settings` | CloudZero settings |
| GET | `/cloudzero/dry-run` | CloudZero dry-run export |
| GET | `/cloudzero/export` | CloudZero export |
| GET | `/vantage/settings` | Vantage settings |
| GET | `/vantage/dry-run` | Vantage dry-run export |
| GET | `/vantage/export` | Vantage export |

Blocked: `/cloudzero/init`, `/cloudzero/delete`, `/vantage/init`, `/vantage/delete`.

### OpenAI-compatible management routes (files, batches, assistants, threads, containers, fine-tuning, videos, interactions)

These are OpenAI API surface routes used for managing stateful objects (not inference). They pass the read-only auth gate. Some return 500 when the backing OpenAI/file-storage provider is not configured on the deployment; that is a backend config issue, not an auth block.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/files` | List files |
| GET | `/v1/files` | List files (v1) |
| GET | `/files/{file_id}` | Retrieve a file |
| GET | `/v1/files/{file_id}` | Retrieve a file (v1) |
| GET | `/files/{file_id}/content` | File content |
| GET | `/v1/files/{file_id}/content` | File content (v1) |
| GET | `/batches` | List batches |
| GET | `/v1/batches` | List batches (v1) |
| GET | `/batches/{batch_id}` | Retrieve a batch |
| GET | `/v1/batches/{batch_id}` | Retrieve a batch (v1) |
| GET | `/fine_tuning/jobs` | List fine-tuning jobs |
| GET | `/v1/fine_tuning/jobs` | List fine-tuning jobs (v1) |
| GET | `/fine_tuning/jobs/{fine_tuning_job_id}` | Retrieve a fine-tuning job |
| GET | `/v1/fine_tuning/jobs/{fine_tuning_job_id}` | Retrieve a fine-tuning job (v1) |
| GET | `/assistants` | List assistants |
| GET | `/v1/assistants` | List assistants (v1) |
| GET | `/assistants/{assistant_id}` | Retrieve an assistant |
| GET | `/v1/assistants/{assistant_id}` | Retrieve an assistant (v1) |
| GET | `/v1/agents` | List agents |
| GET | `/v1/agents/{agent_id}` | Retrieve an agent |
| GET | `/threads/{thread_id}` | Retrieve a thread |
| GET | `/v1/threads/{thread_id}` | Retrieve a thread (v1) |
| GET | `/threads/{thread_id}/messages` | Thread messages |
| GET | `/v1/threads/{thread_id}/messages` | Thread messages (v1) |
| GET | `/containers` | List containers |
| GET | `/v1/containers` | List containers (v1) |
| GET | `/containers/{container_id}` | Retrieve a container |
| GET | `/v1/containers/{container_id}` | Retrieve a container (v1) |
| GET | `/containers/{container_id}/files` | Container files |
| GET | `/v1/containers/{container_id}/files` | Container files (v1) |
| GET | `/containers/{container_id}/files/{file_id}` | Retrieve a container file |
| GET | `/v1/containers/{container_id}/files/{file_id}` | Retrieve a container file (v1) |
| GET | `/containers/{container_id}/files/{file_id}/content` | Container file content |
| GET | `/v1/containers/{container_id}/files/{file_id}/content` | Container file content (v1) |
| GET | `/videos` | List videos |
| GET | `/v1/videos` | List videos (v1) |
| GET | `/videos/{video_id}` | Retrieve a video |
| GET | `/v1/videos/{video_id}` | Retrieve a video (v1) |
| GET | `/videos/{video_id}/content` | Video content |
| GET | `/v1/videos/{video_id}/content` | Video content (v1) |
| GET | `/videos/characters/{character_id}` | Retrieve a video character |
| GET | `/v1/videos/characters/{character_id}` | Retrieve a video character (v1) |
| GET | `/interactions/{interaction_id}` | Retrieve an interaction |
| GET | `/v1beta/interactions/{interaction_id}` | Retrieve an interaction (v1beta) |
| GET | `/responses/{response_id}` | Retrieve a response by ID (retrieval only, not inference) |
| GET | `/v1/responses/{response_id}` | Retrieve a response by ID (v1) |
| GET | `/responses/{response_id}/input_items` | Response input items |
| GET | `/v1/responses/{response_id}/input_items` | Response input items (v1) |
| GET | `/openai/v1/responses/{response_id}` | Retrieve a response (OpenAI prefix) |
| GET | `/openai/v1/responses/{response_id}/input_items` | Response input items (OpenAI prefix) |
| GET | `/openai_passthrough/{endpoint}` | OpenAI passthrough (GET endpoints) |

### User agent analytics

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/tag/dau` | Daily active users (by tag) |
| GET | `/tag/wau` | Weekly active users |
| GET | `/tag/mau` | Monthly active users |
| GET | `/tag/distinct` | Distinct active users |
| GET | `/tag/summary` | Active-user summary (requires start/end date params) |
| GET | `/tag/user-agent/per-user-analytics` | Per-user agent analytics |

### Team membership (read)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/team/{team_id}/members/me` | Caller's membership in a team |

### Metrics

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/metrics` | Prometheus scrape endpoint (text format). Only available when the proxy config lists `prometheus` in `success_callbacks`; protected by `PrometheusAuthMiddleware`, which runs the same `user_api_key_auth` check, so the read-only key is accepted. Returns 404 on deployments that have not enabled the Prometheus callback. |
| GET | `/model/metrics` | Request count and average latency per model on the config (JSON). |
| GET | `/model/metrics/slow_responses` | Slow response breakdown per model (JSON). |
| GET | `/model/metrics/exceptions` | Exception-type breakdown per model (JSON). |

### Health and discovery

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Root (public) |
| GET | `/routes` | Registered routes (public) |
| GET | `/.well-known/litellm-ui-config` | UI config discovery (public) |
| GET | `/litellm/.well-known/litellm-ui-config` | UI config discovery (alt path, public) |
| GET | `/health/liveness` | Liveness (public) |
| GET | `/health/liveliness` | Liveness alt (public) |
| GET | `/health/readiness` | Readiness probe |
| GET | `/health/readiness/details` | Readiness with per-component detail |
| GET | `/health` | Full health (all deployments) |
| GET | `/health/history` | Health check history |
| GET | `/health/latest` | Latest health check |
| GET | `/health/shared-status` | Shared health status |
| GET | `/health/services` | Service health |
| GET | `/health/backlog` | Health-check backlog depth |
| GET | `/health/drain` | Drain status |
| GET | `/health/license` | License status |
| GET | `/active/callbacks` | Active callbacks |
| GET | `/sso/get/ui_settings` | UI settings (public) |
| GET | `/sso/readiness` | SSO readiness |
| GET | `/sso/key/generate` | SSO key generation (initiates flow) |
| GET | `/sso/callback` | SSO callback |
| GET | `/sso/debug/login` | SSO debug login |
| GET | `/sso/debug/callback` | SSO debug callback |
| GET | `/sso/cli/poll/{key_id}` | SSO CLI poll |
| GET | `/api/plugins` | Installed plugins |
| GET | `/api/plugins/auth-token` | Plugin auth token |
| GET | `/get_logo_url` | Logo URL |
| GET | `/get_image` | Image |
| GET | `/get_favicon` | Favicon |
| GET | `/fallback/login` | Fallback login page |
| GET | `/onboarding/get_token` | Onboarding token |
| GET | `/test` | Test endpoint |
| GET | `/settings` | Proxy settings |

### Public discovery (no auth required)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/public/model_hub` | Model hub |
| GET | `/public/model_hub/info` | Model hub info |
| GET | `/public/mcp_hub` | MCP hub |
| GET | `/public/skill_hub` | Skill hub |
| GET | `/public/agent_hub` | Agent hub |
| GET | `/public/providers` | Providers list |
| GET | `/public/providers/fields` | Provider fields |
| GET | `/public/endpoints` | Endpoints list |
| GET | `/public/agents/fields` | Agent fields |
| GET | `/public/litellm_model_cost_map` | Model cost map |
| GET | `/public/litellm_blog_posts` | Blog posts |

## What the key cannot do

The following return `403 user not allowed to access this route, role= proxy_admin_viewer` (or `401` for master-key-only routes):

- All LLM inference routes: `/chat/completions`, `/v1/chat/completions`, `/completions`, `/v1/completions`, `/embeddings`, `/v1/embeddings`, `/responses`, `/v1/responses`, `/images/generations`, `/audio/*`, `/rerank`, `/v1/messages` (Anthropic), Playground, and every `/openai/*`, `/azure/*`, `/bedrock/*`, `/vertex_ai/*`, `/gemini/*`, `/cohere/*`, `/mistral/*`, `/watsonx/*` passthrough.
- All key writes: `/key/generate`, `/key/update`, `/key/bulk_update`, `/team/key/bulk_update`, `/key/delete`, `/key/{key}/regenerate`, `/key/regenerate`, `/key/{key}/reset_spend`, `/key/block`, `/key/unblock`, `/key/service-account/generate`.
- Credential migration: `/credentials/migrate-encryption` (POST), `/credentials/migrate-encryption/check` (GET, returns 403).
- All team/org/user/model writes (see the per-section "Blocked" lines above).
- JWT key mapping writes: `/jwt/key/mapping/new`, `/jwt/key/mapping/update`, `/jwt/key/mapping/delete` (listing is allowed).
- Guardrail writes: `/guardrails` (POST create), `/guardrails/{guardrail_id}` (PUT/DELETE), `/guardrails/register`, `/guardrails/submissions/{guardrail_id}/approve`, `/guardrails/submissions/{guardrail_id}/reject`, `/guardrails/test_custom_code`, `/guardrails/validate_blocked_words_file`.
- Policy writes: `/policies` (POST), `/policies/{policy_id}` (PUT/DELETE), `/policies/attachments` (POST), `/policies/attachments/{attachment_id}` (DELETE), `/policies/test-pipeline`, `/utils/test_policies_and_guardrails`.
- MCP writes: `/v1/mcp/server/register`, `/v1/mcp/server/{server_id}/approve`, `/v1/mcp/server/{server_id}/reject`, `/v1/mcp/server/{server_id}` (DELETE), `/v1/mcp/server/oauth/{server_id}/register`, `/v1/mcp/server/{server_id}/user-credential` (PUT).
- Cloud cost provider writes: `/cloudzero/init`, `/cloudzero/delete`, `/vantage/init`, `/vantage/delete`.
- `/global/spend/reset` (proxy admin only).
- `/global/spend/refresh` (proxy admin only).
- `/memory-usage-in-mem-cache` and `/memory-usage-in-mem-cache-items` (master key only; returns 401).
- Config writes: `/config/update`, `/config/field/update`, `/config/field/delete`, `/config/callback/delete`.
- Schedule/reload writes: `/reload/model_cost_map`, `/reload/anthropic_beta_headers`, `/schedule/model_cost_map_reload`, `/schedule/anthropic_beta_headers_reload`.
- Pass-through config writes: `/config/pass_through_endpoint` (POST/PUT/DELETE).
- Onboarding token claim: `/onboarding/claim_token` (POST).

The `/chat/completions` 400 you may see when passing an invalid model name does not mean inference is allowed; it means model-name validation happens to run before the role gate on some proxy builds. The role gate still blocks real inference. If you want definitive proof, hit a deployed model name and observe the 403.

## Quick verification commands

```bash
export PROXY_BASE_URL="https://litellm.adeoaiengine.ecouncil.ae"
export ATHENA_KEY="sk-nUVF9ruGsSRqe7xOthTU1Q"

# Read works
curl -sk -X GET "$PROXY_BASE_URL/key/list?page=1&size=5" \
  -H "Authorization: Bearer $ATHENA_KEY"

curl -sk -X GET "$PROXY_BASE_URL/global/spend/logs" \
  -H "Authorization: Bearer $ATHENA_KEY"

curl -sk -X GET "$PROXY_BASE_URL/model/info" \
  -H "Authorization: Bearer $ATHENA_KEY"

# Writes are blocked (expect 403)
curl -sk -X POST "$PROXY_BASE_URL/key/generate" \
  -H "Authorization: Bearer $ATHENA_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key_alias":"should-fail"}'

# Confirm self-role
curl -sk -X GET "$PROXY_BASE_URL/user/info" \
  -H "Authorization: Bearer $ATHENA_KEY"
# user_info.user_role == "proxy_admin_viewer"
```

## Rotation and management

The athena key cannot modify itself or any other key; it is read-only. Rotation, deletion, and backing-user management require a separate proxy admin key. A proxy admin can perform these actions using the endpoints below (authenticate with an admin key, not the athena key):

Regenerate (Enterprise feature; produces a new key value, same user/role):

```bash
curl -sk -X POST "$PROXY_BASE_URL/key/athena-read-only/regenerate" \
  -H "Authorization: Bearer <PROXY_ADMIN_KEY>"
```

Delete (by alias or key value):

```bash
curl -sk -X POST "$PROXY_BASE_URL/key/delete" \
  -H "Authorization: Bearer <PROXY_ADMIN_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"keys":["athena-read-only"]}'
```

## Security notes

- This key sees all tenants' data: every team, organization, user, key, and spend record across the entire proxy. That is inherent to `proxy_admin_viewer`. Do not share it beyond trusted auditors/dashboards.
- There is no per-team or per-org scoping on this role. If scoped read-only access is ever needed, a separate mechanism (a regular key with `allowed_routes` + `object_permission`) would be required; that is not what this key is.
- Store the key value in a secret manager. Do not commit it to git or paste it into issues/PRs.
- The key has no expiry set. Set `duration` on `/key/generate` or use `/key/{key}/regenerate` with a new `duration` if time-bound access is needed.
