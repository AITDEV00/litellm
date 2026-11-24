# SSO application integration: user access to models and MCP tools

This is the current guide for the OICM layer above LiteLLM. **Part 1 is for layer developers. Part 2 is for the proxy admin.** It is derived from this checkout on 2026-11-24; older files in this folder can contain earlier deployment findings

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

The proxy admin creates a team, assigns one or more access groups to it, and adds your user to it. A group's `access_model_names` and `access_mcp_server_ids` define the model and MCP resources; optional `mcp_tool_permissions` limits tools per MCP server

One person can belong to multiple teams. That membership does not make every key inherit every team's resources. Choose one team for each request or virtual key so the application has a predictable authorization boundary

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

### Existing prod example

The ADEO organization already exists as `19c3609c-b4ba-447e-8829-0765785038fa`. The `ADEOGPT-ENDUSERS` team already exists as `b19ad7de-997e-45ec-ba34-f7980dccf621`. Reuse existing objects rather than creating another organization or team with the same alias

The **planning request occurred on 2026-11-24**. On that date, an MCP-only group named `adeogpt-endusers-mcp` was assigned eight provisioned MCP servers to `ADEOGPT-ENDUSERS` through `POST /v1/access_group`, not `/access_group/new`. Include this in repeatable provisioning and periodically verify the resulting assignments

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

Use a name for a capability bundle such as `adeogpt-endusers-knowledge`, not a name tied to one user or temporary session. Reuse capability groups across teams when they have equal resource meaning. Teams reference the group from `assigned_team_ids`, enabling access via membership rather than per-user duplication

For a full-list model ceiling inherited by that team, omit or deliberately set allowed models as the admin API contract permits. Avoid assuming that one empty field expands into universal API access: validate actual model and MCP behavior in dev

If team members need to create their own keys, allow `/key/generate` (and only the other needed operations) in `team_member_permissions` for their team. Ordinary members default to `/key/info` and `/key/health` only. Team admin or member roles are not a substitute for these operation permissions

### User provisioning and identity

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
Choose this option when an application backend acts for a signed-in user and already participates in Entra authentication. This avoids accumulating disposable virtual keys and does not require ordinary users to administer keys

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
