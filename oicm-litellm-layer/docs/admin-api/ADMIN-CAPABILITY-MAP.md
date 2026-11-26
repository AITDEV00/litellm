# Gateway administration capability map

Reviewed against the live authenticated `https://litellm.ecouncil.ae/openapi.json`, LiteLLM `1.108.5`, on 2026-11-25. The schema contains 761 HTTP path entries, then JavaScript/Python handler authorization and configuration were checked for the relevant management families below. This guide records supported surface areas and boundaries; it does not claim optional integrations are configured in prod

Start here when a task seems to require a Microsoft Entra change or a database edit. There is usually an Admin UI or management-route contract that supports the intended outcome more safely. Application-specific group mappings, proxy-admin credential access, and gateway token validation are still distinct controls

## Interpret OpenAPI as a map, not a capability stamp

`GET /openapi.json` reveals supported routes and request/response schema. It does not prove that a route is configured, enabled, safe for an unapproved POST/GET, or available to every authenticated role. Verify:

1. The route's handler and per-record checks, not only its `Depends(user_api_key_auth)` auth requirement. An authenticated internal user or read-only admin does not imply an allowed mutation
2. The owning config/DB table and persistence behavior. Many UI config mutations require `STORE_MODEL_IN_DB=True`; if off, the endpoint intentionally returns 500, not authorization success
3. The role and team/org scope: global user role, membership role, virtual-key allowed routes, attached access groups, model/tool scopes, and `team_member_permissions` are independent controls
4. Idempotence/replace semantics, current deployed version, and a dev-only functional test for mutations. Source references here are in this checkout, rather than URLs that could drift across releases

Expected auth behavior can be surprising: `proxy_admin_viewer` has broad safe-method administrative reads and blocks inference/writes except explicitly permitted user-account/commit operations. It may read ordinary default-settings routes directly but not mutate them. `/get/internal_user_settings` and `/get/default_team_settings` also allow internal reads. `update_ui_settings` explicitly permits only `proxy_admin`; do not generalize its behavior to unrelated settings without testing the actual handler. Ordinary teammates only have baseline `/key/info` and `/key/health` management permissions unless a team grants more

## 1. Default user provisioning and roles

### Dashboard: Internal Users -> Default User Settings -> Edit Settings

This is a real gateway feature. It does **not** require Microsoft Entra application-role edits just to choose default internal-user settings

| Control | Actual field | Meaning |
|---|---|---|
| Default Role | `user_role` | Global role used when a new applicable user lacks an explicitly supplied valid role. The UI role list in this card shows internal-user choices; the request model can represent additional roles |
| Max Budget (USD) | `max_budget` | Default user budget ceiling when no explicit ceiling applies |
| Reset Budget | `budget_duration` | Recurrence/budget window (for example `30d`, `daily`, `monthly` supported by deployment contract). Not the same as changing password reset state |
| Default Models | `models` | Default user's model ceiling. This is separate from selected team's model scope and from model access-group grants |
| Default Teams | `teams` | Existing team IDs/membership-role assignments copied into newly created users. The handler rejects unknown and duplicate IDs so silently dropped future defaults cannot hide |
| Per-team limit | `max_budget_in_team` | New team-member budget. The handler also updates the corresponding default team-member-budget setting through team update paths |

Call the APIs:

```bash
curl "$GATEWAY/get/internal_user_settings" -H "Authorization: Bearer $LITELLM_ADMIN_KEY"
curl "$GATEWAY/update/internal_user_settings" -X PATCH \
  -H "Authorization: Bearer $LITELLM_ADMIN_KEY" -H "Content-Type: application/json" \
  --data-binary @default-user-settings.json
curl "$GATEWAY/get/default_team_settings" -H "Authorization: Bearer $LITELLM_ADMIN_KEY"
```

`DefaultInternalUserParams` lives in `litellm/proxy/_types.py`, with default team shape in `DefaultTeamSSOParams`. `get_internal_user_settings`, `get_default_team_settings`, `_validate_default_teams_exist`, `_validate_default_organization_exists`, `update_internal_user_settings`, and `update_default_team_settings` live in `litellm/proxy/ui_crud_endpoints/proxy_setting_endpoints.py`. The update is a `PATCH` with settings object, not `/user/update` on each person. Save via the DB config setter with audit logging, no values printed as if they were secrets

### Precedence, applicability, and existing accounts

Source behavior:

- `insert_sso_user` in `litellm/proxy/management_endpoints/ui_sso.py` applies default parameters, but **preserves an explicitly supplied valid SSO role**. A Microsoft app-role/group mapping is not made mandatory by this feature and can still override the default role intentionally
- `_update_internal_new_user_params` in `litellm/proxy/management_endpoints/internal_user_endpoints.py` applies defaults to `/user/new` only for missing eligible fields, with bounds/role exceptions. Empty/default model lists get replaced with the default list under current logic. Explicit role/budget/team configuration still matters
- It affects users created by UI SSO or user management APIs. Verify any direct JWT auto-upsert path explicitly; a login authorization branch is not the same object-creation function as `/user/new` or browser SSO and may use different field treatment
- These values do not automatically retrofit limits or memberships into already-existing users. Change an existing person's role through `/user/update`; membership through `/team/member_add`; their team's resource/member ceilings through team settings
- A global `internal_user_viewer` is not a team-admin restriction. Team membership `role: "user"` is separate from the global role. Function > mere role-name semantics: inspect actual handler/team permissions before classifying UI view behavior

**Current prod default settings read (2026-11-25, not a permanent product invariant):** `user_role=internal_user`, unset global max-budget/reset/model-list, and default membership in both `ADEOGPT-ENDUSERS` and `ADEOGPT-DESKTOP`, with member role `user`. This is important facts-not-assumptions context for new users, and does not replace reviewing a particular account's roles and groups

### Protocol-specific password and forced-reset state

The UI card does **not** expose `password_reset_required`. Do not confuse a budget reset or a role default with a connector/password flag. A manually created account password currently stamps `password_reset_required=true`; complete the guarded two-field connector-specific update only as defined in the [source-agent guide](../usage-guides/MCP_SOURCE_AGENT_GUIDE.md#step-2-log-in) for that account type/onboarding path. All affected new/provisioned users must have the end-use password-reset state intentionally set and verified; inherited default `internal_user` alone does not guarantee connector readiness

`POST /user/password/change` is a first-class self-service route under `password_endpoints.py`, from the calling user's password dashboard session. The login HTTP response gives the browser a locally signed, locally minted HS256 wrapper JWT. The frontend decodes the inner `key` claim as its API `accessToken`; `/management/v1/users/me`, `/user/password/change`, and `/session/logout` receive that DB-backed password-session key. `AuthContext` and `change-password/ChangePasswordForm.tsx` show the selected behavior below the login layer

Do not call the raw wrapper JWT directly as a general Bearer credential to Entra/API routes: with `enable_jwt_auth`, that fails external JWKS validation. It does **not** prove the browser cannot reset, nor does changing roles require Entra. Proxy local password sessions and actual Entra API access tokens remain distinct credential families. The admin password flag is deliberately true until the person completes change; never use proxy-admin elevation or a reset-false shortcut merely to avoid their appropriate interactive reset path

**Verified pilot 2026-11-25:** a lowercased ordinary `internal_user` was created with `auto_create_key:false`, two live default ADEOGPT team defaults plus explicit `DEVELOPER` user membership, and a known short temporary password satisfying policy. `/user/update` set forced reset. Its inner password session failed generic `/v2/user/info` with reset-required HTTP 403, rejected wrong current password HTTP 400, and revoked via HTTP 200. Its original `DEVELOPER` key token/owner/model entitlements did not change while account provisioning occurred

User migration for email-shaped `DEVELOPER` keys is **account-only initially**. A local temporary implementation documents commands and an auditable candidate manifest. Pre-existing matching accounts are reused; their identity and credential state are never overwritten by script. Required memberships are repaired idempotently while unwarranted team roles and nondefault memberships stay untouched. `/key/update` changing `user_id` is only a later separate owner-transfer step: it preserves token hash, `team_id`, explicit model scope, budget, policy and metadata when omitted. `user_id` ownership update may remove proxy-admin fallback and change effective user/team permissions, so historical `default_user_id` ownership is not necessarily preserved forever

## 2. SSO configuration and role mapping

`GET /get/sso_settings` and `PATCH /update/sso_settings` expose SSO configuration persistence via the dedicated DB SSO table and encrypted at-rest configuration (see `SSOConfig` and `resolve_sso_config`). `GET /sso/readiness` is available for readiness checks as its own handler. It is separate from `get_internal_user_settings`; this setting does not move Entra client-secret rotation into normal user provisioning

Browser SSO first upserts the user, role defaults, and assigned teams, then generates the dashboard UI credential. An explicitly preserved SSO app-role can overrule default internal-user role, so read the selected user's resulting global role and membership, not just a directory group name

Do not replace mutual exclusive planning choices: delegated Entra API JWT validation needs `JWT_PUBLIC_KEY_URL`, audience and exact literal issuer, identity claims, and the configured team mapping or `fallback_to_db_teams`. Browser SSO/UI cookies are a different credential family, and native `llm_session_` MCP sessions are restricted to MCP. These controls are independent

## 3. Team-organization governance APIs

| Capability | Live route family | Source behavior worth knowing |
|---|---|---|
| User CRUD/search/profile | `/user/new`, `/user/update`, `/user/info`, `/user/filter/ui`, `/v2/user/info` | Only supported role values/fields are accepted; password setting stamps reset flag; only `auto_create_key: false` avoids any immediately issued key |
| Invitations and email delivery | `/invitation/new`, `/invitation/info`, `/invitation/delete` | Endpoint checks admin privileges, not admin role list only. Invitation flow must not be copied into connector-specific password workflow automatically |
| Batch users and team membership | `/management/v1/users/bulk`, `/management/v1/users/bulk_delete`, `/management/v1/teams/{team_id}/members/bulk_update`, `/management/v1/teams/{team_id}/members/bulk_delete` | Operation-specific gateway role/organization membership gates remain. Batch operations still enforce per-record identity/member checks |
| Organization hierarchy | `/organization/new`, `/organization/info`, `/organization/update`, `/v2/organization/{organization_id}`, member add/update/delete | `org_admin` governance resides in membership rows. Organization admin permission never makes that person's global LiteLLM account necessarily a proxy admin |
| Teams and defaults | `/team/new`, `/team/update`, `/team/info`, `/team/block`, `/team/unblock`, `/get/default_team_settings`, `/update/default_team_settings` | Team defaults include model/TPM/RPM/budget/org/`team_member_permissions`; `models` default specifically governs auto-created SSO-group teams. Manual calls inherit only when contract supports it |
| Per-membership budgets/lifecycle | `/team/member_add`, `/team/member_update`, `/team/{team_id}/member/{user_id}/reset_spend`, `/team/{team_id}/member/{user_id}/reset_budget`, `/team/permissions_update`, `/team/permissions_bulk_update` | Role doesn't mean unlimited user budget. Reset endpoints change current counters/limits under explicit contract, not rewrite historical spend reports |
| Permission shape | `team_member_permissions` | Non-admin members baseline `/key/info`, `/key/health`; permissible operations can add key management, shared-group assignment gating `/key/access_group_assignment`, etc. Team admin is not substitute permission for just any route |
| Legacy group surface | `/access_group/new`, `/access_group/list`, `/access_group/{access_group}/...` | Separate legacy model/MCP/tag semantics; do not route newly deployed unified resource bundles through this older family |
| Unified resources | `/v1/access_group`, `/v1/access_group/{access_group_id}`, aliases `/v1/unified_access_group` | Supports models/MCP/agents/skills/tool permissions and assigned team/key IDs. Update uses replacement-like fields; merge deliberately with POST create failing duplicates 409. No secret values belong in references |
| Team editable fields | UI Settings `team_admin_editable_team_fields` | Proxy-wide permitted team fields (`tpm_limit`, `rpm_limit`, `max_budget`, `soft_budget`, `metadata`, team_member_budget/limits). `projects`, `member_key_budgets`, `create_member_keys`, `raise_max_budget` also require intentional policies; the actual supported gates are in `team_admin_field_permissions.py` |

Per-team operation `/team/permissions_update` has authoritative member routes. Proxy-wide `team_admin_editable_team_fields` (under UI Settings) is a different policy lever; it has an explicit supported list, not a hypothetical all-fields admin. `raise_max_budget` requires `max_budget`. `create_member_keys` explicitly produces ordinary `LLM_API` virtual keys in safe member ownership context, not an administrator impersonation factory

## 4. Other centrally administered functions

Already present in live OpenAPI, with actual owning source paths enabled for scoped work:

| Area | Routes or family | Guidance |
|---|---|---|
| JWT identity mapped to keys | `/jwt/key/mapping/*` | Separate from UI app-role claims; no implicit audience/issuer trust through role defaults |
| Viewer/self routes and password | `/user/available_roles`, `/user/password/change`, `/session/logout`, logout/session keys | Authentication selection by token family matters; the UI session only mints a dashboard scoped key ordinarily, not broad app model credentials |
| Central UI settings | `/get/ui_settings`, `/update/ui_settings`, `/get/ui_theme_settings`, `/update/ui_theme_settings`, `/get/user_banner`, `/update/user_banner` | Settings protects model creation, app sidebar visibility, agent/vector-store allowances, team-admin editable fields, public UI/AI hub options. Env-derived flags are reported but not directly writable. Source values matter alongside default ones |
| Observability and budget | `/budget/*`, `/management/v1/budgets`, `/spend/*`, `/global/*`, team/user/org daily activity/report endpoints | Global reports are tenant-sensitive; internal users only authorized scoped activity. Read-only admin has broad observations but not necessarily downstream provider execution |
| Configuration storage lifecycle | `/config/list`, `/config/yaml`, `/config/field/info`, callbacks config, cache/Redis settings | Admin changes can become database desired-state backed when enabled; this may supersede replayed YAML patches. Reconcile saved desired state with DB-safe-overrides before rollout |
| SCIM lifecycle | `/scim/v2/Users`, `/scim/v2/Groups`, SCIM placeholders merge | Dedicated directory command field rules and explicit mapping; not a default UI one-to-one scim connector setting. Verify as-env/client-secret separately |
| Provider credentials | `litellm/proxy/management_endpoints/custom_llm_credentials_endpoints.py` family (`/credentials/*`) | Personal user connections and OAuth user-credential/provider metadata are separate from arbitrary shared gateway credentials. Never log/pin sensitive values |
| Agents/tools/registry | agents, prompts, guardrails/policies, vector stores, projects/tags, skills, search tools | Management endpoints route differently from cost-incurring inference. Inspect execution scopes before granting broad `management_routes` |
| Model/budget fallbacks | `/model/new`, `/model/update`, model info/cost map, fallbacks/config settings | Separate model metadata, rate ceilings, active provider health, and reachable credentials. Full current model names are not authorization constants |
| MCP server administration | `/v1/mcp/*`, server authorization/credentials, OAuth status/user credentials | Source broker's connector identity is `internal_user` scoped source/routes, not viewer admin. Public-IP eligibility is always separate from user/key/group grants and routes |

## 5. Full live route-family inventory

This is the complete tag census counted from the downloaded `761` OpenAPI paths. **Every method on every path is listed in the separate [complete operation register](OPERATION-REGISTER.md)** (1,021 method entries, 99 primary family slices, including all management, inference, public, MCP, install, and generic passthrough routes). Several tags are applied to one endpoint. The conceptual map here does not duplicate 761 sublists:

```text
llm_passthrough 118; mcp 71; untagged 47; model management 36; audio 36; team management 35;
Budget & Spend Tracking 26; vector_stores 26; guardrails 25; responses 24;
health 21; scim 20; Internal User management 19; videos 18; containers 18; public 17;
tag management 17; assistants 16; organization management 16; Policies 16; files 15;
key management 15; google genai endpoints 14; credential management 13; Customer Management 13;
install 13; batch 12; Lens 11; evals 11; [beta] A2A Agents 10; policies 10; prompts 10;
access_groups 10; WebSocket 9; Claude Code Marketplace 9; realtime 9; auto router 8;
fine-tuning 8; workflow management 8; Cost Tracking 8; interactions 8; tools 8;
config_overrides 8; agent tracing 7; budget management 7; search_tools 7;
search 6; images 6; Agent Management 6; caching 6; SSO Settings 6; Settings 6;
user agent analytics 6; vector_store_management 6; cloudzero 6; vantage 6;
OpenAI Pass-through 5; pass-through 5; UI Settings 5; memory management 5; Telemetry 5;
project management 5; gemini_agents 5; a2a 5; langfuse_passthrough 5; jwt_mappings 5;
chat/completions 4; completions 4; embeddings 4; llm utils 4; rag 4; decisions 4;
anthropic_skills 4; rerank 3; UI Theme Settings 3; Fallback Management 3;
Cache Settings 3; Coordination Redis Settings 3; email management 3;
openrouter-compatible 3; anthropic_passthrough 3; moderations 2; [beta] Agents 2;
providers 2; ocr 2; Logging Callbacks 2; moyai 2; plugins 2; Router Settings 2;
Audit Logging 2; skills 2; oicm 2; policy_resolve 2; model_insights 2; compliance 2;
adaptive_router 1; [beta] MCP 1; UI Session 1; experimental 1;
rust control plane 1; Cost Optimization 1; Agents 1; a2a_registration 1; usage_ai 1.
```

Many of these serve model-data/inference routes rather than administration. Adding every operation to a provisioning allowlist would grant substantially more access than a future application needs. Start from approved endpoint/task requirements, not a full OpenAPI export

## Practical onboarding checklist

1. Determine type: human requiring ordinary user defaults, existing user needing role edit, organization/team admin membership, directory-managed identity, connector, or unattended service-account key
2. For ordinary new SSO/API users, set `user_role` and desired team defaults via Default User Settings. Explicit non-null choices still belong on the actual create/SSO call, and valid SSO roles take precedence
3. Existing user role changes go via user edit/API. Do not claim default settings revises them. Organization/team administration is membership-based, not the default global role
4. Resolve resulting team UUIDs, unified resource group attachments, models/tools, and budgets. An SSO user and a virtual key's one selected `team_id` can expose only one resource boundary, not union every team into every request
5. Hand off password/source connector only after actual UI login and connector `/sso/cli/password` success. It should be `internal_user`, password-reset state complete, and its returned key restricted to source routes only. Admin viewer is not the correct connector user
6. For UI defaults or DB-backed SSO setting changes, verify read-back, in-memory/database sync, cross-pod behavior, then exercise creation and an existing-user negative case. Include reset/pwd/side-effects proof, not successful form click alone

See [Full admin REST reference](LITELLM-ADMIN-REST-API.md), [SSO application integration](../access-control/sso-application-integration.md), and [source-agent onboarding](../usage-guides/MCP_SOURCE_AGENT_GUIDE.md) for supporting flows. For dead service credentials no longer used, revoke by server token hash through the authorized endpoint rather than treating a deleted local file as revocation