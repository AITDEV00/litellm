# Application authorization with SSO and LiteLLM

This guide covers two supported ways for an application that uses Microsoft Entra SSO to authorize calls through the LiteLLM gateway

Use **Option 1 for a backend acting on behalf of a signed-in person**. Use **Option 2 when an SDK, CLI, or autonomous service needs a LiteLLM-issued credential**. A UI SSO login and direct API JWT authentication are independent paths. A person does not need to sign into the Admin UI before their application can present a valid, correctly mapped Microsoft access token to the gateway

| Concern | Option 1: delegated Microsoft JWT | Option 2: LiteLLM virtual key |
|---|---|---|
| Typical caller | Application backend representing a person | SDK, CLI, or autonomous application |
| Request credential | Microsoft access token for the LiteLLM audience | LiteLLM `sk-...` credential |
| Own stored key per user session | No | No. Use only when another system requires a LiteLLM key |
| Team selection | Team claim or validated `x-litellm-team-id` header with DB fallback enabled | The key's single `team_id` |
| Revocation | Entra token expiry, team membership, or grant changes | Membership/grant changes, key blocking, expiry, or deletion |
| Recommended default for user traffic | Yes | No |

## Shared authorization model

Create the organization and authorization objects before onboarding application users:

1. Create an organization to own teams and provide organization-level limits
2. Create one team for each membership and budget boundary
3. Create reusable access groups containing the required model, MCP server, tool, agent, or skill grants
4. Assign each access group to the teams that need it through `assigned_team_ids`
5. Add users to the correct teams with an ordinary `user` role unless they administer the team

Teams do not implicitly contain access groups. The relationship is made by `assigned_team_ids` on the group or `access_group_ids` on the team. `access_mcp_server_ids` controls MCP servers, and `mcp_tool_permissions` can optionally limit tools per server

An administrator can inspect and provision these objects through the endpoints in the [Admin REST API reference](LITELLM-ADMIN-REST-API.md). The unified MCP-capable access-group resource uses `POST /v1/access_group`. A team key creates a second concern: the caller needs both team membership and the team's key-management permission

### Stable user identity

Map Entra's immutable `oid` claim to the gateway's `user_id`. Keep email as user metadata and a lookup attribute, not as the durable identity. Use a delegated access token, not an ID token, for application authorization

The current prod settings are in `deploy/overlays/prod/gateway/config.yaml`:

```yaml
general_settings:
  enable_jwt_auth: true
  litellm_jwtauth:
    user_id_jwt_field: "oid"
    user_email_jwt_field: "upn"
    team_id_jwt_field: null
    user_id_upsert: true
```

With `user_id_upsert: true`, a correctly mapped first JWT call can provision the user record. UI SSO login is not a prerequisite. Existing deployments must first reconcile existing user records with the claim-derived identity before starting self-service team selection

A user can belong to more than one team. **No virtual key silently combines all of a user's teams.** A key pinned to team A does not acquire team B's grants because the same user also belongs to B. Direct JWT requests also resolve one selected team per request. This is the reason to choose team boundaries carefully and keep access-group grants attached to those teams

## Option 1: forward a delegated Microsoft access token

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
