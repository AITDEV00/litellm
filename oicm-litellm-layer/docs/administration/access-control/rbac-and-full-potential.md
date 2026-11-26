# RBAC: role ceilings, resource grants, and the current delegation model

Part 0 preserves an inventory from 2026-11-23, read before native Microsoft/JWT SSO and the ENDUSERS access group were provisioned. It is not the current count or a recommendation to retain those conditions. Current supporting evidence is in guides 06, 14, and 16

The current model uses OIDC/JWT where appropriate, selected team membership, and assigned unified groups. READ-only administrative identities and ordinary source-connector identities must not be conflated

---

## Part 0: what prod actually has, in RBAC terms

| Thing | Count | State |
|---|---|---|
| organizations | 5: ADEO, DGE, PRESIGHT, UNIFYAPPS, INCEPTION | exist, enterprise license active |
| organization admins | **0** | every org has zero `org_admin` members |
| teams | 25 | distributed across the 5 orgs, all `access_group_ids: []` |
| token-issuing users | a handful (`default_user_id` admin, two connector accounts, two end users, two viewers) | most keys minted centrally by the admin |
| guardrails | 0 | none configured |
| SSO | not configured | no `ui_sso` in `general_settings` |

So the org layer exists and is licensed, the team layer exists and is populated, and both are essentially ungoverned because no one holds an org_admin role and no team has MCP grants.

## Part 1: does admin-creating-every-key make sense

Short answer: it works as a bootstrap and it is the wrong steady state.

What it costs you:

- **Budget and rate limits fall on one actor.** Budgets, `rpm`, `tpm`, `tpd` can be set per user, per team, per org, per key, per model. If you mint every key centrally you have to also centrally maintain every limit, and the numbers live in one person's head.
- **No revocation point per consumer.** With per-team keys, blocking the team stops everyone at once. With central keys you block key by key.
- **Attribution collapses.** Spend still records per key, but "which team burned this" requires the key-to-team mapping to be right, and central keys with empty `team_id` record nothing useful. Two of the ten keys in the earlier audit have no team.
- **Role changes require the admin.** Granting someone MCP access, a new model, or a higher rate means an admin call. Teammates cannot help themselves.
- **Audit is weak.** `created_by` on the eight MCP rows was null. An admin-minted key with no `litellm-changed-by` header leaves no trail of who it was for.

What it buys you: tight control and one place to look. That is real, and it is why it is the right way to bootstrap. It just does not scale to 25 teams and 5 organizations.

**The better model**, grounded in this code: users own their identity, teams own their budget and their grants, orgs own their model ceiling, and admins own the guardrails and the ceilings. Users mint their own keys inside their team. Revocation and budget live on the team, not the key.

## Part 2: the role ladder, exactly as the code enforces it

From `docs/administration/admin-api/LITELLM-ADMIN-REST-API.md` section 1 and the source:

| Role | Scope | Source of truth |
|---|---|---|
| `proxy_admin` | everything, every org, every team | `UserAPIKeyAuth.user_role` or the master key |
| `proxy_admin_viewer` | read everything, writes blocked | `route_checks._check_proxy_admin_viewer_access`: all inference blocked, GET/HEAD/OPTIONS allowed, a short allowlist of read-POSTs, everything else 403. Single write exception is `/user/update` limited to `user_email` and `password` for self-service |
| `org_admin` | admin over one organization: create teams and users within it, bounded by the org's models and limits | `verify_org_access` in `organization_endpoints.py`: proxy admin, or a user with an `organization_memberships` row whose `user_role == ORG_ADMIN` for that org |
| team admin | a team member with `role: "admin"` | `is_team_admin` in `management/teams/authz.py` |
| `internal_user` | own keys, own spend, Admin UI | route checks |
| `internal_user_viewer` | read own data only | route checks |

The team-versus-org arbitration is one small file worth reading once, `litellm/proxy/management/teams/authz.py`. It defines `TEAM_OR_ORG_ADMIN` and `strongest_role`, and the rule is:

- proxy admin wins outright,
- team admin is checked before org admin, so the org DB lookup is skipped for ordinary team admins,
- org admin outranks team admin when a caller holds both, so an org admin keeps unrestricted edits inside their org.

So yes: **an org_admin can create teams inside their own org and cannot touch another org.** `new_team` resolves `organization_id`, checks the org exists and its team limits, and the create route uses `get_team_access().allows(..., TEAM_OR_ORG_ADMIN)`.

One important asymmetry to know: `new_team` is available to ordinary users too. A non-admin can create a standalone team with themselves as admin, and `_check_user_team_limits` bounds it by their own user budget and model set. So "can a user create their own team" is already yes; what you probably want is "can they create one **under an organization**, bounded by the org," which is the org_admin path.

## Part 3: the exact granularity that exists

This is the list I would want if I were you. Every row is a real field or check in this tree.

### Model access

- Org `models`, team `models`, key `models`, user-level model list. Each is a ceiling that intersects downward.
- Model access groups (`model_access_group_management_endpoints.py`) for named bundles.
- Per-model budget `model_max_budget`, per-model rate `model_rpm_limit` / `model_tpm_limit`, per-model fallbacks.

### Budget and rates

- `max_budget`, `budget_duration`, `soft_budget` at user, team, org, and key level.
- `rpm_limit`, `tpm_limit`, `tpd_limit`, `max_parallel_requests` at every level.
- `rpm_limit_type` / `tpm_limit_type`: `guaranteed_throughput` refuses over-allocation, `best_effort_throughput` allows it.
- Member-level budget: `team_member_budget`, `team_member_rpm_limit`, `team_member_tpm_limit`, `team_member_model_max_budget`, each optionally with its own reset duration.
- `throttle_on_budget_exceeded` per key: degrade rate instead of hard-blocking.

### MCP

- Legacy `object_permission.mcp_access_groups`: source tag grants; separate from unified access groups.
- Unified `assigned_team_ids` / `assigned_key_ids` on an access group: attached `access_mcp_server_ids`, models, agents, and other grants. Key-assigned unified groups are additive to runtime key/server grants rather than always intersection-capped by the legacy tag ceiling.
- `mcp_servers`: explicit server list, with the `no_mcp_servers` sentinel to deny absolutely.
- `mcp_tool_permissions`: `{server: [tool, ...]}`, exact tool allowlist per server.
- `mcp_toolsets`: named reusable permission bundles, cached in Redis.
- `blocked_tools` on key/team/org: hard deny by bare tool name.
- `mcp_rpm_limit`: per-MCP-server RPM, keyed by server name, on key and team.
- `mcp_tool_search_enabled`: hide tools from search until needed.
- `allowed_params` and `allowed_tools` / `disallowed_tools` on the server itself.
- `available_on_public_internet` / `allow_all_keys` as coarse server-level switches.

### Routes

- `allowed_routes` / `allowed_passthrough_routes` / `denied_passthrough_routes`.
- `key_type` presets: `LLM_API`, `MANAGEMENT`, `READ_ONLY`, `DEFAULT`. This is the clean way to make "individual users who can only read" without hand-writing route lists.
- `team_member_permissions`: the routes non-admin team members may call (`/key/generate`, `/key/update`, ...).

### Guardrails and policy

- Guardrails at global, team, key, and per-request level. `disable_global_guardrails` at team and key.
- `policies` for named guardrail-plus-condition bundles.
- `tool_policy` for trust-state tool denial.
- `require_trace_id` per team: reject untraced LLM, MCP, and agent requests.

### Identity and lifecycle

- `duration` / `expires` per key, `auto_rotate` and `rotation_interval` per key.
- `blocked` per key and per team.
- Service account keys (`/key/service-account/generate`): team-owned, no user.
- `/key/regenerate` with a grace period, for rotation.
- SCIM (`management_endpoints/scim/`) and `ui_sso` with role and team mappings, for provisioning users and their roles from an IdP.
- JWT key mapping (`jwt_key_mapping_endpoints.py`).

### Audit

- `litellm-changed-by` header on mutations to record on-behalf-of actions.
- `created_by` / `updated_by` on keys, teams, orgs.
- Audit log endpoints under `management_helpers/audit_logs.py`, used by team and key mutations.

## Part 4: the locked-ceiling question, honestly

You asked whether a user can create their own teams under an organization without being able to change what the proxy admin preconfigured. What the code gives you:

- An org_admin creating a team inside the org is capped by `_check_org_team_limits`: the org's models and budget bound the team, and the org ceiling wins when intersected.
- A guardrail a proxy admin sets globally applies to every team unless that team explicitly disables it, and `check_disable_global_guardrails_caller_permission` restricts who may disable it to a proxy admin.
- Model ceiling: org `models` is intersected down through team and key, so a team cannot grant a model the org forbids.

What the code does **not** give you natively:

- There is no "field-level lock" that prevents an org_admin from editing a specific preconfigured team value. `update_team` uses `TEAM_OR_ORG_ADMIN`, which means an org_admin can edit any team in their org, including one you preconfigured.
- So if you want some teams to be admin-controlled only, the answer is not a lock, it is structure: put the preconfigured teams in a separate org where only proxy admins hold org_admin, or do not make your consumers org_admin (make them team admins of their own team only, which `TEAM_OR_ORG_ADMIN` lets them edit and nothing else).

That is the honest boundary: ceilings cascade and cannot be exceeded downward; editing rights inside the ceiling are not per-field.

## Part 5: recommended target model for this deployment

1. **Make admins, not keys, per consumer.** One human account per person, role `internal_user`, member of exactly the teams they need.
2. **Put every team under an organization.** All 25 already have an `organization_id`, which is good. Set each org's `models` to the ceiling that org may use, so the org becomes the guarantee.
1. **Assign organization governance through the actual organization membership UI or `/organization/member_add`.** Do not imply ordinary people can be delegated only through Entra app roles. The earlier historical organization-admin count is not current. See the administration capability map for the membership-backed org/team governance contract.
4. **Move budget and rates onto the team.** Token limits per team with `team_member_budget` if you need per-person caps. Then stop setting limits per key by hand.
5. **Let users mint their own keys** with `key_type` presets. `READ_ONLY` for people who should only look, `LLM_API` for normal consumers. Add `team_member_permissions` so team admins can generate and revoke their own members' keys.
6. **Grant MCP per team** with unified `assigned_team_ids` on an access group listing intended `access_mcp_server_ids`. Use legacy `mcp_access_groups` tags only where deliberately needed, then narrow with `mcp_tool_permissions` or a toolset.
7. **Keep genuinely read-only identities separate from consumers.** `proxy_admin_viewer` can perform allowed administrative reads but not model/MCP inference. `internal_user_viewer` is inside the product-user role family; it is not equivalent to a proxy-admin read role. Attach the exact routes/resources a person needs, not merely a safer-looking name
8. **Keep `available_on_public_internet: true` for intentionally remote sources.** It governs source-public-IP eligibility, not resource grants. Removing it would deny approved remote users; authorization comes from membership, unified server group(s), and routes. Use `false` only for an explicitly private-network source.

## Part 6: promoting users to configure the MCP tunnels

The connector login rejects `proxy_admin`, `proxy_admin_viewer`, and password-reset-required accounts. Use an ordinary non-admin account with `source-connector-*` route scope. The route restrictions are the important least-privilege boundary; the login does not reject every role other than `internal_user`, including an org-admin membership on an ordinary user row. Do not promote anyone just to run an agent

- The person who will expose a source gets an ordinary `internal_user` account.
- You mint (or better, they mint) a key for it with `allowed_routes: ['/mcp-sources', '/mcp-source-link-sources/*']`. That is exactly what the six existing `source-connector-*` keys already do, and it is the least privilege available.
- They run `adma account add` with their own username and password; the agent exchanges the password for that scoped key through `/sso/cli/password` itself, so you do not even have to hand them a key.

Do not make someone a proxy administrator to configure tunnels. Give an ordinary connector identity the two source-management routes only. Org governance membership is orthogonal: the password-login role check does not create a blanket exception for org admins, although separating such identities is still better operational practice.

## Part 7: SSO, and the airgap question

**Supported providers**, per `management_endpoints/ui_sso.py`: Microsoft (`MICROSOFT_CLIENT_ID`), Google (`GOOGLE_CLIENT_ID`), any generic OIDC (`GENERIC_CLIENT_ID` plus `GENERIC_AUTHORIZATION_ENDPOINT`, `GENERIC_TOKEN_ENDPOINT`, `GENERIC_USERINFO_ENDPOINT`), and SAML (`/sso/saml/login`). Configured under `general_settings.ui_sso` with role mappings (`user_allowed_roles`, role and team mappings) so a directory group maps to a LiteLLM role or team.

**Current deployment uses native Microsoft OIDC and API-JWT validation.** Prod and dev have Microsoft login configuration, and guide 16 records successful OIDC/browser SSO. It requires gateway egress to Microsoft's code-exchange and user-information endpoints. SAML remains an airgap alternative in guide 12, not the current protocol choice. UI-SSO session cookies, delegated Entra JWTs, browser-minted dashboard virtual keys, and MCP session tokens have different authorization branches; use the distinction in guides 10, 14, and 16. Rotate any previously exposed Entra client secret, including dev material, without writing secret values into these documents

**Historical airgap analysis, not the deployed protocol recommendation:** The callback host is not the pod's address; `get_redirect_url_for_sso` calls `get_custom_url`, which prefers `PROXY_BASE_URL` over the request's own base URL (`litellm/proxy/utils.py` `get_custom_url`). So:

- Set `PROXY_BASE_URL` to the public or edge hostname that the user's browser can reach.
- The IdP's registered redirect URI is `https://<that host>/sso/callback`. The user's browser does the OIDC dance against Microsoft, gets the code, and posts it back to that same public host.
- That host must reach the in-cluster gateway. Since the browser is on the internet and the gateway is airgapped, you need an allowed bridge: a reverse proxy or ingress on an edge host that forwards to the gateway, which is the same pattern the prod tunnel Ingress already uses for the broker.
- The pod itself never needs outbound internet for the browser flow. It does need outbound reach to the IdP **only** if you use token introspection or JWKS validation that fetches keys over the network; with normal OIDC the tokens arrive on the callback, but JWKS verification still needs to fetch the IdP's keys, so either allow egress to the IdP, mirror the JWKS, or accept the constraint. This is the one real airgap caveat and it depends on how you configure verification.

So the short version: the browser-side login works from an internet-connected client through an edge URL, and `PROXY_BASE_URL` is what makes that possible despite the airgapped pod. The pod-side outbound requirement to the IdP's JWKS endpoint is the part to plan for.

## Part 8: would best practice make per-user access easier

Yes, materially, and in a specific way. The work moves from per-key bookkeeping to per-membership setup:

- **Per-user model access** becomes "which team is this person in", and the team's `models` is the answer. Add a person to a team; they inherit.
- **Per-user MCP access** becomes "assign a unified access group listing intended server IDs to this team". Group `assigned_team_ids` is the linkage to keep auditable; direct key-group assignment can widen access and is not just a narrowing modifier.
- **Per-user token limits** become `team_member_tpm_limit` / `team_member_budget` on the team, set once, applying to every member.
- **Revocation** becomes removing a team member or blocking the team, one action.
- **New-person onboarding** can use **Internal Users -> Default User Settings** or `PATCH /update/internal_user_settings` for default role/budget/models/teams, even without directory roles. Explicit valid SSO roles take precedence. Existing users need their user edit/team membership updated separately. Default users resulting on prod currently also get both chosen team memberships; this does not union access into one key.

The cost up front is real: you must populate org ceilings, team budgets, and team grants once. After that, adding a user is one membership row rather than a bespoke key with hand-picked limits.

## Part 9: what I would do next, in order

1. Decide the org ceiling for each of the 5 orgs (ADEO already says `all-proxy-models`; consider whether that is intended).
2. Make one org_admin per org.
3. Keep intentionally remote servers public-address eligible, grant only through assigned unified MCP groups, and narrow tool permissions/argument policy. `available_on_public_internet` is not an identity bypass; disabling it would break approved public-IP callers rather than solve access control.
   Default users can be provisioned through the gateway's default-user settings without requiring an added Entra app-role; use directory mapping only for intentional role precedence. The proxy-wide set of team-admin editable fields lives in UI Settings `team_admin_editable_team_fields`, independently of explicitly permitted per-team member operations.
4. Move existing per-key limits up to their team, then simplify the keys.
5. Replace ad-hoc keys with `key_type` presets, and keep `internal_user_viewer` for read-only people.
6. Only then consider SSO, which is the biggest setup and the least urgent if the team/org model is sound.

## Source files

- `oicm-litellm-layer/docs/administration/admin-api/LITELLM-ADMIN-REST-API.md` sections 1-4, 8-9
- `oicm-litellm-layer/docs/administration/admin-api/ATHENA-READ-ONLY-KEY-GUIDE.md` read-only rules and SSO note
- `litellm/proxy/management/teams/authz.py` `TEAM_OR_ORG_ADMIN`, `strongest_role`
- `litellm/proxy/management_endpoints/organization_endpoints.py` `verify_org_access`, `_enterprise_license_required`
- `litellm/proxy/management_endpoints/team_endpoints.py` `new_team` and its org scoping
- `litellm/proxy/management_endpoints/ui_sso.py` providers, `get_redirect_url_for_sso`
- `litellm/proxy/utils.py` `get_custom_url` / `PROXY_BASE_URL`
- `litellm/proxy/auth/route_checks.py` `_check_proxy_admin_viewer_access`
- `litellm/proxy/_experimental/mcp_server/auth/user_api_key_auth_mcp.py` the MCP permission inheritance
