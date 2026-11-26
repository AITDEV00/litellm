# SSO application integration: user access to models and MCP tools

This is the current guide for the OICM layer above LiteLLM. **Part 1 is for application developers. Part 2 is for the proxy admin.** It is derived from this checkout and read-only live checks on 2026-11-24. Older files in this folder preserve dated investigation history

Distinctions to preserve:

- Delegated Entra API tokens authorize selected user/team model/MCP REST calls when gateway audience, issuer, identity, and membership mapping are configured correctly
- Browser Microsoft SSO creates a UI login and, normally, a dashboard-team virtual key embedded in its session. It is not a direct Entra bearer
- Gateway native MCP OAuth produces an `llm_session_` token accepted by `/mcp` only. That does not give OpenCode a model or `/v1/skills` credential
- Team membership alone never unions all teams' resources into a virtual key or one regular REST request
- Direct delegated-JWT team selection still requires `fallback_to_db_teams: true` rollout and dev proof. This guide does not claim that the flag is already enabled

## Part 1: guide for layer developers

You already implemented Microsoft Entra SSO in your application. This is what you add to call a model or an MCP tool as the signed-in user

### The token you need

Use the **delegated Microsoft access token for the gateway's API audience** as the request bearer. Do not send an ID token, a Microsoft Graph token, or an access token meant only for your own application API to LiteLLM

When your backend has its own application token and needs one for the gateway resource, use Entra on-behalf-of exchange to obtain the delegated gateway token. A `client_credentials` token represents the application, with no human identity, so it is suitable for a service credential rather than user access

Your proxy administrator supplies these values:

```text
GATEWAY=https://litellm.ecouncil.ae
GATEWAY_SCOPE=<gateway Entra API scope>
TEAM_ID=<the intended team's UUID>
```

Check that the gateway access token has the expected `aud`, `iss`, and immutable user claim `oid`, plus the configured human identity claim, normally `upn`. A user does not need a separate Admin UI login before their correctly mapped token can authorize an API call

### Which credential should you send?

| Your caller | Default credential | Team applied |
|---|---|---|
| Backend acting for a signed-in person | Delegated Microsoft access token | One selected team for the request |
| SDK or CLI that explicitly requires `sk-...` | Short-lived personal virtual key | The key's single `team_id` |
| Autonomous background service | Team-owned service-account virtual key | Its application team; do not impersonate a user |

**Do not mint a new virtual key for every application request.** Forward the delegated access token when you need human identity and permissions. It avoids storing an umbrella gateway key or an ever-growing set of session keys in your backend

### Shared team and access-group concepts

The proxy admin creates a team, assigns unified access groups by `assigned_team_ids`, and adds users to that team. A group's `access_model_names` and `access_mcp_server_ids` define model/MCP resources and optional `mcp_tool_permissions` narrows tools per server. Review direct group assignments to keys separately: unified key groups can be additive grants above team ceilings, unlike legacy tag intersections

One person can belong to multiple teams. Select one team for each key or delegating request. A virtual key keeps one `team_id`; API-key tests do not prove JWT team switching works, nor does a browser MCP session grant model access

`ADEOGPT-ENDUSERS` currently has an eight-server MCP group. `ADEOGPT-DESKTOP` has a separate model ceiling and no assigned MCP group at the 2026-11-24 read. A user added to both does not automatically receive MCP through Desktop; this is intentional isolation until Desktop gets its own explicit grant

### Call a model with the user's Microsoft token

The gateway must have `fallback_to_db_teams: true` for membership-based team selection, configured by your proxy admin in Part 2. Send the selected team by ID, not by a user-supplied model field or untrusted SSO metadata:

```bash
curl "$GATEWAY/v1/chat/completions" \
  -X POST \
  -H "Authorization: Bearer $MICROSOFT_DELEGATED_ACCESS_TOKEN" \
  -H "x-litellm-team-id: $TEAM_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "example-model",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

LiteLLM validates the token and team membership, then inherits that team's grants. Non-member team selection is rejected. Team IDs or unique aliases work in the header; have your application map its business context to a fixed team UUID

### Discover and call MCP tools

Use the same bearer and header everywhere:

```bash
# Servers/grants visible to this selected team
curl "$GATEWAY/v1/mcp/server" \
  -H "Authorization: Bearer $MICROSOFT_DELEGATED_ACCESS_TOKEN" \
  -H "x-litellm-team-id: $TEAM_ID"

# Discover the exact tool name, input schema, and server ID
curl "$GATEWAY/mcp-rest/tools/list" \
  -G \
  -H "Authorization: Bearer $MICROSOFT_DELEGATED_ACCESS_TOKEN" \
  -H "x-litellm-team-id: $TEAM_ID" \
  --data-urlencode "server_id=$MCP_SERVER_ID"

# Call one discovered tool with an object of arguments matching its schema
curl "$GATEWAY/mcp-rest/tools/call" \
  -X POST \
  -H "Authorization: Bearer $MICROSOFT_DELEGATED_ACCESS_TOKEN" \
  -H "x-litellm-team-id: $TEAM_ID" \
  -H "Content-Type: application/json" \
  -d "{\"server_id\":\"$MCP_SERVER_ID\",\"name\":\"$TOOL_NAME\",\"arguments\":{}}"
```

Do not infer a tool's server or bypass its input schema. Discover first, preserve the returned server ID, and use the exact tool name in `/mcp-rest/tools/call`. Team MCP grants restrict both discovery and calls

Your application should treat `401` as an authentication problem and `403` as a denied team, membership, resource, or tool grant. It should surface an actionable denial to the user rather than silently switching teams or using an admin key

### When an SDK still needs a virtual key

Create only a short-lived personal key, bound to the intended team:

```bash
curl "$GATEWAY/key/generate" \
  -X POST \
  -H "Authorization: Bearer $MICROSOFT_DELEGATED_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"team_id\":\"$TEAM_ID\",\"duration\":\"1h\",\"key_alias\":\"temporary-client-key\"}"
```

The backend, never the browser, holds the delegated token. Only send the generated key to the client credential store intended to hold it. Do not attempt `/key/generate` with another person's user ID, or request admin keys to work around a team-policy rejection. Ordinary team members require `team_member_permissions` to generate keys. A team key inherits its team's access groups; if you give it additional explicit grants, those grants can also change how downward restrictions are combined

### Integration checklist

1. Obtain a real delegated token for the gateway audience and inspect only its claims, not its secret, in diagnostics
2. Select one configured team UUID for the request
3. Validate a model request and `GET /v1/mcp/server`
4. Discover a tool then invoke it with its returned server ID and expected arguments
5. Test an ungranted team or MCP resource and preserve the expected denial
6. Never log tokens or virtual keys, and never place the gateway admin secret in either browser or server runtime

## Part 2: proxy-admin setup and best practices

Use [Part 1](#part-1-guide-for-layer-developers) to understand the calling contract before changing any gateway configuration

### Recommended RBAC hierarchy

```text
Organization: business ownership and organizational limits
  |
  +-- Team: application/security boundary, budgets, members
  |     |
  |     +-- assigned access group: reusable models/MCP/tool grants
  |     |
  |     +-- members: people or team administrators
  |     |
  |     +-- optional service-account key: autonomous service only
  |
  +-- Another team: distinct grants, never implicit union into a key
```

Structure teams around how you want to budget, authorize, and revoke one consumer or project. Do not make one team per model or tool, and do not make broad groups to avoid a second team assignment. Delegate team administration to a team member with `role: "admin"` only when needed. Use `org_admin` membership for organizational governance if you delegate that boundary, not `proxy_admin`

### Current prod reference placement

The ADEO organization is `19c3609c-b4ba-447e-8829-0765785038fa`. Reuse it; dev uses independently resolved IDs, never copied blindly

| Object | ID and relevant policy verified 2026-11-24 |
|---|---|
| `ADEOGPT-ENDUSERS` | `b19ad7de-997e-45ec-ba34-f7980dccf621`; `all-proxy-models`, group `adeogpt-endusers-mcp` (`922c1bd2-d479-4134-970e-4daf1bd27ca4`) with all eight provisioned servers |
| `ADEOGPT-DESKTOP` | `d1406939-aef6-4205-a8b2-7616c5a72663`; selected models, no MCP access group at this read. Do not deliberately reuse ENDUSERS' group until Desktop's consumers should receive those exact servers |
| `jyao@ecouncil.ae` | Existing canonical user `90714f16-39de-400f-9a79-09edd1c48eed`, ordinary member of both teams, observed as `internal_user_viewer` during prod pilot |
| `dqvu@ecouncil.ae` | Existing canonical user `bfc998f1-9eac-4264-b389-9adb403c3e37` (`Dang Vu`), `internal_user_viewer`, now verified `role: user` in both teams; no password reset or key issuance during membership operation |
| `ffkhan@ecouncil.ae` | Ordinary member of ENDUSERS in the live team record; no new role or credential is needed solely because this guide lists membership |
| `athena-mcp@ecouncil.ae` | Connector user `f7bf9877-8f4f-4683-a43d-e24f5dd5c4ed`, `internal_user`, no team or API key initially. Credentials are stored outside the repo, mode 0600 under `~/.config/adeogpt/`. Reissue/rotate through authorized credential management if misused |
| `athena-read-only@ecouncil.ae` | A distinct `proxy_admin_viewer` identity from the MCP connector. No consumer-team assignment is assumed |

These are observed reference objects, not mandates for subsequent grant or user counts. Always resolve aliases through `/v2/team/list`, `/team/info`, and `/user/list` before mutations. Do not silently replace requested aliases with similar names. Group membership only assigns the intended team's grants; it does not duplicate keys or modify a user's global LiteLLM role

The unified group endpoint is `POST /v1/access_group`, not `/access_group/new`. Listing and read are `GET /v1/access_group` and `/v1/access_group/{id}`. Do not reuse an older production observation that these newer endpoints are unavailable; current source/prod behavior supersedes it

### Organization and membership

Create an organization only when it does not exist. Team ownership follows `organization_id`; ordinary team membership is separate from organization admin membership

To delegate an organization's governance, add a named owner as `org_admin`:

```bash
curl "$GATEWAY/organization/member_add" \
  -X POST \
  -H "Authorization: Bearer $LITELLM_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"organization_id\": \"$ORGANIZATION_ID\",
    \"member\": {\"user_email\": \"$OWNER_EMAIL\", \"role\": \"org_admin\"}
  }"
```

Do not make every ordinary consumer an org or team member at both levels. Grant only the membership needed for their authorization and governance roles. Set the organization's intended model ceiling and budget/rate limits, reviewing any legacy `all-proxy-models` value instead of keeping it blindly

### Team and grants

Create a team under the organization, with its own budget and model scope:

```bash
curl "$GATEWAY/team/new" \
  -X POST \
  -H "Authorization: Bearer $LITELLM_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"team_alias\": \"$TEAM_ALIAS\",
    \"organization_id\": \"$ORGANIZATION_ID\",
    \"models\": [\"$MODEL_NAME\"]
  }"
```

Create one reusable resource grant and assign it to that team:

```bash
curl "$GATEWAY/v1/access_group" \
  -X POST \
  -H "Authorization: Bearer $LITELLM_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"access_group_name\": \"$ACCESS_GROUP_NAME\",
    \"access_model_names\": [\"$MODEL_NAME\"],
    \"access_mcp_server_ids\": [\"$MCP_SERVER_ID\"],
    \"mcp_tool_permissions\": {\"$MCP_SERVER_ID\": [\"$TOOL_NAME\"]},
    \"assigned_team_ids\": [\"$TEAM_ID\"]
  }"
```

Use a capability name such as `adeogpt-endusers-knowledge`, not a user or temporary session name. Reuse a bundle across teams only when those teams intentionally receive equal resources. Team references live in the group's `assigned_team_ids`; team membership activates them rather than duplicating each user's grants

Creation is not idempotent and duplicate names return 409. Before creating, list existing groups and resolve the ID. To change one, read it and `PUT /v1/access_group/{id}` with a **deliberate complete replacement** of its relevant arrays. New `access_mcp_server_ids` and `assigned_team_ids` can detach what you omit, so merging/reviewing is necessary. Never blindly repost the group or wipe unrelated servers/teams from a shared group. After an update, read the group and both team records and compare intended exact membership/server sets. For a small isolated team group with no shared assignments, omitting optional fields is less error-prone; test the actual deployed handler in dev first

For model assignments, do not infer that `[]` means full model access. Test the model list with the selected team/access-group context. ENDUSERS intentionally uses an `all-proxy-models` team ceiling at the audit date, whereas DESKTOP uses an explicit model set. A narrower model access group cannot silently be treated as an MCP grant or vice versa

For a full-list model ceiling inherited by that team, omit or deliberately set allowed models as the admin API contract permits. Avoid assuming that one empty field expands into universal API access: validate actual model and MCP behavior in dev

If team members need to create their own keys, allow `/key/generate` (and only the other needed operations) in `team_member_permissions` for their team. Ordinary members default to `/key/info` and `/key/health` only. Team admin or member roles are not a substitute for these operation permissions

### User provisioning and identity

Set ordinary new-user defaults in **Admin UI -> Internal Users -> Default User Settings**, or `PATCH /update/internal_user_settings` backed by `litellm_settings.default_internal_user_params`. This can set user role, model/budget ceilings, existing teams and per-team defaults without requiring Entra app-role edits. The gateway reads these for UI SSO and `/user/new`; preserve valid explicitly mapped SSO roles, which take precedence. These defaults do not revise existing accounts or independently guarantee direct-JWT creation uses the same provisioning function. Use the [administration capability map](../admin-api/ADMIN-CAPABILITY-MAP.md) to separate default creation from existing-user and membership edits.

Current prod defaults on 2026-11-25 are `internal_user`, no set max-budget/budget-period/default models, and ordinary membership of ENDUSERS plus DESKTOP, as verified from `GET /get/internal_user_settings`. Role and group mappings must therefore be evaluated alongside that baseline, not solely or automatically in Entra. Do not confuse these settings with the separate `password_reset_required` bearer connector requirement.

Prefer Entra `oid` as the gateway `user_id`; use `upn` as an identity email attribute. For human users:

- Option A, UI SSO-driven provisioning: sign into the Admin UI, or use an explicitly provisioned SCIM/directory membership if implemented. The SSO callback upserts a user into `LiteLLM_UserTable`
- Option B, API JWT-driven provisioning: with `user_id_upsert: true`, the first valid mapped JWT request can provision the user. Existing records matched by email must be reconciled with the `oid`-derived identity before enabling broad team self-selection

Add a user to an existing team:

```bash
curl "$GATEWAY/team/member_add" \
  -X POST \
  -H "Authorization: Bearer $LITELLM_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"team_id\": \"$TEAM_ID\",
    \"member\": {\"user_email\": \"$USER_EMAIL\", \"role\": \"user\"}
  }"
```

Adding the user to a second team is valid. It does not merge two teams' access into a virtual key pointing at the first team. Use an explicit per-request team header under DB fallback, or separate keys for different team contexts

### Gateway configuration for delegated users

Enable enterprise JWT validation and explicitly validate both audience and literal issuer. Apply the following to both dev and prod configuration overlays, and propagate the prod base-body copy correctly:

```yaml
general_settings:
  enable_jwt_auth: true
  litellm_jwtauth:
    user_id_jwt_field: "oid"
    user_email_jwt_field: "upn"
    user_id_upsert: true
    team_id_jwt_field: null
    fallback_to_db_teams: true
```

The first four settings currently exist in prod. **`fallback_to_db_teams: true` is a recommended change still to be rolled out when enabling direct delegated team calls.** Omitting it means you cannot rely on a membership-only `x-litellm-team-id` header as documented above

Point `JWT_PUBLIC_KEY_URL` at your tenant's Entra JWKS, pin `JWT_AUDIENCE` to the gateway resource audience, and pin `JWT_ISSUER` to the actual issuer of a real token for your intended flow. Entra v1 app-only and v2 delegated issuer forms can differ. A signing-valid token for the wrong audience is wrongly trusted if audience validation is omitted

If groups rather than DB membership are the authority, use explicit team-claim mappings instead of `fallback_to_db_teams`, with controlled group-to-team provisioning. Do not combine group-derived selection and a free caller-selected team unless that security intent has been explicitly tested

### Service application keys

An unattended workload should use a separately owned application team and a team-owned `POST /key/service-account/generate` credential, provisioning it out-of-band with a managed admin credential. Inject the result from a secret manager, restrict routes/resources/budget, and rotate it. Keep virtual keys away from JavaScript bundles, URLs, logs, tickets, and commits

For application-level consumer attribution when human JWT identity is unavailable, use LiteLLM's end-user metadata rather than pretending each request comes from a different employee. Removing a person must not delete a service key, and removing the service's access should not require deleting a human user row

### Verification and lifecycle

Run this with a real delegated user token on dev before prod:

1. Member calls the model in the intended team
2. Same member sees exactly the intended MCP servers and can invoke a permitted tool
3. A non-member cannot select that team or call its tool
4. A team-A virtual key remains limited to team A after its owner also joins team B
5. All requests preserve expected user/team attribution and satisfy budget limits
6. Existing user identity and domain allowlist behavior are correct before wider rollout

Use dev port-forward for the live probe:

```bash
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000
```

The public `litellm.ecouncil.ae` host is prod, not dev. Re-run the port-forward after dev rollouts. To remove a person's access, revoke their team membership or directory assignment and check selected team behavior. If you intentionally issued a client virtual key, revoke or block it too. Do not leave unowned temporary keys or master-key credentials in application infrastructure

## Source references

| Behavior | Source |
|---|---|
| Access-group models, MCP server IDs, tool permissions, and assigned teams | `litellm/types/access_group.py` |
| JWT claim mappings, user upsert, and DB team fallback | `litellm/proxy/_types.py`, `litellm/proxy/auth/handle_jwt.py` |
| Browser SSO provisioning separately from API JWT authentication | `litellm/proxy/management_endpoints/ui_sso.py` |
| Team key self-binding and key-management permission enforcement | `litellm/proxy/management_endpoints/key_management_endpoints.py`, `litellm/proxy/management_helpers/team_member_permission_checks.py` |
| REST MCP discovery and invocation body contract | `litellm/proxy/_experimental/mcp_server/rest_endpoints.py` |
| Key-pinned single-team MCP grant resolution | `litellm/proxy/_experimental/mcp_server/auth/user_api_key_auth_mcp.py` |

The [Admin REST API reference](../admin-api/LITELLM-ADMIN-REST-API.md) has other provisioning fields. Runnable SSO scripts and token inspection examples live at `examples/sso-to-api-token/README.md` outside this MkDocs tree

## Native MCP clients and private skills

For OpenCode, native MCP OAuth is the browser sign-in pattern documented in guides 15 and 16, not a delegated Entra bearer configured as a static header. The successful `/mcp` login/list proof stores `llm_session_` credentials with refresh tokens; `/v1` models reject them. Until a supported proxy-credential exchange is configured in the client, provide OpenCode model access through a separate approved team-bound credential rather than a dashboard/admin key

OpenCode's generic unauthenticated HTTP skill catalog is not LiteLLM `/v1/skills`. Do not enable `public_skills_index` or `enable_mcp_registry` to make private synchronization work. Existing `/v1/skills` owner scopes select `user:<id>` first when user identity exists, even when the request has a team. Current `access_skill_names` does not make ordinary teammate skill rows shareable and has specific skill-plugin visibility semantics elsewhere. Any private sharing requires explicit owner/team-aware design and denial tests, followed by actual OpenCode content-loading proof

See [the OpenCode plan](../../mcp/opencode-sso-oauth-skills-pilot.md) for required dev gates and [the measured evidence](../../mcp/opencode-pilot-evidence.md) for what is complete. Treat model credentials, actual per-server tool invocation, refresh-after-expiry, agent-only D3 negative proof, and private skill loading as unfinished according to that record. Membership provisioning alone does not change those pilot statuses

## Credential lifecycle and audit secrets

Use an authorized proxy-admin service credential only for provisioning or explicit server-side revocation. Obtain secrets from approved secret references or a mode-0600 local store, never print/export them to project documentation, screenshots, browser/client config, or tracked shell scripts. A local `~/.config/adeogpt/` file with restrictive permissions is working storage, not a managed team secret provider

Do not reset or regenerate an existing user's password when merely adding team membership. Use `POST /user/new` with `auto_create_key: false` only for a genuinely new email-managed account, followed by an explicit password update if required

**OICM provisioning rule: whenever an account is created using the proxy-admin key, finish with `password_reset_required=false` before handing it to its owner.** The password-setting administration path currently stamps true, so checking the flag after `/user/new` and `/user/update` is a required step, not an optional polish item. Use the exact-ID/verified-role, two-column update in [the source-agent guide](../../usage-guides/MCP_SOURCE_AGENT_GUIDE.md#step-2-log-in) to finish enrollment; verify the successful UI login and, for a connector, the true `POST /sso/cli/password` proof. Any later genuine breach-screen flag needs password-quality review rather than recurring reset suppression

The provisioning password is duty-sensitive even when temporary; keep it local and non-recycled in protected storage, and handle login/API key material separately. Plan user-managed replacement without confusing it with a routine team-membership update. The known raw-UI-session/Entra-JWT routing problem for self-service password changes must be fixed as such, not turned into an admin impersonation workaround. `/team/member_add` verifies membership and leaves the global user role unchanged

Enabled direct delegated-JWT team calls require real dev negative tests: wrong audience/issuer, non-member team selection, and revoked resource grants. Never assume a JWT-authenticated key-generation example establishes model/MCP access or that cross-team key permissions are enabled. Ordinary teammates baseline `/key/info` and `/key/health`; do not circumvent missing `/key/generate` permission with a master or dashboard credential.

### Flow

```mermaid
sequenceDiagram
    actor User
    participant App as Application backend
    participant Entra as Microsoft Entra
    participant GW as LiteLLM gateway
    User->>App: Sign in with Microsoft SSO
    App->>Entra: Obtain delegated access token for gateway audience
    Entra-->>App: Access token with user identity claims
    App->>App: Select one authorized team for this request
    App->>GW: Authorization: Bearer <access-token>; x-litellm-team-id: <team>
    GW->>Entra: Verify signature through tenant JWKS
    GW->>GW: Verify audience, issuer, user, team membership, and grants
    GW-->>App: Authorized model or MCP response
```

The application creates or signs into its own SSO session. It then requests a **delegated access token whose audience is the LiteLLM application registration or exposed gateway API scope**. If an existing application access token is issued for a different resource and grants no gateway audience, use Entra's on-behalf-of flow to exchange it for the correct delegated token

Do not send a token for Microsoft Graph or simply whichever token the application received for its own API. LiteLLM validates its configured audience and exact issuer as well as the token signature. An app-only `client_credentials` token identifies the service principal, not a signed-in person; use Option 2 for an autonomous service instead

### Team selection when the token has no team claim

Enable membership-backed DB fallback:

```yaml
general_settings:
  litellm_jwtauth:
    user_id_jwt_field: "oid"
    user_email_jwt_field: "upn"
    user_id_upsert: true
    team_id_jwt_field: null
    fallback_to_db_teams: true
```

Apply that setting to both dev and prod overlays, including the prod block copied from the base configuration. **This is a recommended configuration change, not a claim that the setting is already enabled.** `fallback_to_db_teams` does not make a caller a team member. It allows a token with no team claim to use a team already assigned in the gateway database, verified against membership

The application chooses **one** team for every operation and supplies its team ID in the header. A unique team alias is also supported by the header. Example request placeholders:

```bash
curl "$GATEWAY/v1/chat/completions" \
  -X POST \
  -H "Authorization: Bearer $MICROSOFT_DELEGATED_ACCESS_TOKEN" \
  -H "x-litellm-team-id: $TEAM_ID" \
  -H "Content-Type: application/json" \
  -d '{"model":"example-model","messages":[{"role":"user","content":"Hello"}]}'
```

Do not place `user_id` or `team_id` in untrusted browser JSON. The backend decides which of its known team mappings applies and uses only a membership LiteLLM has authorized. The gateway is responsible for rejecting accidental or malicious attempts to switch to a different team. The source implementation lives in `litellm/proxy/auth/handle_jwt.py`: `resolve_team_from_header`, `authorize_jwt`, `get_objects`, and `user_api_key_auth_from_result`

Use tokens with only the gateway scope the application actually needs. Keep refresh tokens and other tokens for unrelated APIs in the application's confidential server-side session, not in gateway calls or logs

## Option 2: create a LiteLLM virtual key

Choose this option when a consumer requires a LiteLLM `sk-...` credential, or an application must operate without a user's interactive login

### Short-lived personal key for CLI or SDK access

A signed-in user with a valid Microsoft JWT can request `POST /key/generate` from the gateway. The caller must specify the intended team when team access is required:

```bash
curl "$GATEWAY/key/generate" \
  -X POST \
  -H "Authorization: Bearer $MICROSOFT_DELEGATED_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"team_id\":\"$TEAM_ID\",\"duration\":\"1h\",\"key_alias\":\"temporary-client-key\"}"
```

For a non-admin caller, key generation binds an omitted `user_id` to the caller's own identity. A supplied user ID is also limited to that caller unless a permitted team admin operation applies. For a team key, the target user must belong to the team and the caller needs the team's key-generation permission. Ordinary team members have only the baseline key-information and key-health permissions by default; enable `team_member_permissions` deliberately, rather than making everyone a team admin

Generate keys only after verifying the authenticated identity and selected team. Give them an expiry suited to the client, avoid granting additional access groups or elevated roles, and delete or block a key when its client access ends. Keep returned keys only in the credential storage intended for the consuming client

The virtual key subsequently carries a single `team_id`. Changing the user's team membership affects membership-driven authorization, but membership in another team is not added to that key automatically. Select another team by generating a separate key or intentionally updating a key through the proper team-management operation

### Long-lived autonomous application credential

Do not put the master key or an admin key in a client or browser. Do not make one service application pretend to be each signed-in user. For an actual service identity:

1. Use a representation-owned team corresponding to the application security boundary
2. Provision a key through `POST /key/service-account/generate` with a managed administrator credential outside the application runtime
3. Give the key only the intended model, MCP, route, rate-limit, and budget permissions
4. Store it in the application's secret manager and rotate it
5. Represent consumer attribution separately as end-user metadata or end-user identity where needed

A service-account key belongs to the team rather than to the human account that created it. It therefore avoids deletion when an employee account is removed and does not revoke that employee's membership. Revoking an individual employee should have no effect on service credentials, while revoking the service credential should not delete a person

## Deployment checklist

Before enabling either option in prod:

1. Expose the gateway API scope in Entra and request a token with the correct scope and delegated consent for user traffic
2. Verify the token's real `aud`, `iss`, `oid`, and email claim. Pin the exact issuer in gateway configuration
3. Provision organization, teams, assigned access groups, and users first for planned membership
4. Test a member against team A and confirm team A's models and MCP servers work
5. Test the same user against team B only if they actually belong to B; test a non-member against A and require rejection
6. Confirm that a key for team A does not acquire team B's grants after the user joins B
7. If allowing user key generation, explicitly grant the intended team-member key-management permission and test that ordinary members cannot create keys for other users or teams
8. Validate delegated JWTs on dev through `kubectl port-forward`, not the public production ingress. Repeat before prod rollout

For the two runnable SSO-to-token experiments, including audience and issuer handling, see `examples/sso-to-api-token/README.md` outside this MkDocs tree

## Source references

| Behavior | Source |
|---|---|
| Access groups list MCP server IDs and assigned teams | `litellm/types/access_group.py` |
| JWT claim mappings, user upsert, and DB team fallback | `litellm/proxy/_types.py`, `litellm/proxy/auth/handle_jwt.py` |
| Browser SSO user upsert and team mapping | `litellm/proxy/management_endpoints/ui_sso.py` |
| User/team self-binding and team-member permission enforcement | `litellm/proxy/management_endpoints/key_management_endpoints.py`, `litellm/proxy/management_helpers/team_member_permission_checks.py` |
| Key-pinned single-team MCP resolution | `litellm/proxy/_experimental/mcp_server/auth/user_api_key_auth_mcp.py` |
