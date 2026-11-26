# Teams, access groups, and MCP grants: the admin playbook

This is the operating guide for the common request "put these people in different teams, some of them in several teams, and let each group reach different MCP servers and semantic tools". It states the object model first, because almost every mistake here comes from assuming a virtual key can behave like a person with several teams at once. It does not. A key carries exactly one `team_id`, and that single value is its resource boundary

Reviewed against LiteLLM `1.108.5` source in this checkout, `litellm/types/access_group.py`, `litellm/proxy/management_endpoints/access_group_endpoints.py`, `litellm/proxy/management_helpers/team_member_permission_checks.py`, and the route contracts recorded in [LITELLM-ADMIN-REST-API.md](LITELLM-ADMIN-REST-API.md), then cross-checked on 2026-11-26 against the upstream pages "Virtual Keys", "How Key-Based Auth Works", and "Role-based Access Controls". Live production state is described where it is a fact of the deployment, with the date, not as a permanent invariant

## The object model in one page

Five objects, each with one job

| Object | Job | Cardinality |
|---|---|---|
| Internal user (`LiteLLM_UserTable`) | The person. Owns keys, holds a global role and ceilings | One row per human |
| Team (`LiteLLM_TeamTable`) | The resource and budget boundary. Holds `models`, budgets, limits, and `access_group_ids` | A user may be a member of many teams |
| Membership row (`LiteLLM_TeamMembership`) | The person's role inside one team plus per-team budget and limits | One row per user per team |
| Access group (`LiteLLM_AccessGroupTable`) | A reusable bundle of resources: models, MCP server IDs, per-server tool permissions, agents, skill names. Attached to teams and keys | Attached to many teams and many keys |
| Virtual key (`LiteLLM_VerificationToken`) | A credential. Carries exactly one `team_id`, optional own `models`, optional own `access_group_ids`, optional own `object_permission` | Many per person |

The one rule that explains most support tickets: **`user` membership is not a grant, and a grant is not a key**. Being in four teams gives a person four resource boundaries, but a request made with a single key only ever sees the one team that key names. There is no implicit union of a person's team memberships inside one key, and there is no "all my teams" key

## Recommended structure

### 1. One account per person, `internal_user`

Create the person once. Do not create one gateway account per team, and do not issue shared accounts

```bash
curl -sk -X POST "$PROXY_BASE_URL/user/new" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_email": "person@example.com",
    "user_alias": "Person Name",
    "user_role": "internal_user",
    "auto_create_key": false
  }'
```

`auto_create_key: false` is deliberate when you are about to place the person in specific teams. A key minted at creation time has no team, so it is a personal key with only the user's own ceilings, and you would have to update it afterwards anyway. For the password and forced-reset sequence see [LITELLM-ADMIN-REST-API.md](LITELLM-ADMIN-REST-API.md) section 8, "Strategy: How to Create a New User"

### 2. Teams carry the ceilings, access groups carry the resources

Put budgets, rate limits, and the model ceiling on the team. Put MCP servers, tool allowlists, agents, and skill names in an access group, and attach that group to the team. This split is what lets you change who can reach a server without rewriting every key

```bash
curl -sk -X POST "$PROXY_BASE_URL/team/new" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "team_alias": "consumer-support",
    "organization_id": "'"$ORGANIZATION_ID"'",
    "models": ["model-a", "model-b"],
    "max_budget": 500.0,
    "budget_duration": "30d",
    "rpm_limit": 600,
    "tpm_limit": 400000
  }'
```

Then create the resource bundle and attach it in the same call

```bash
curl -sk -X POST "$PROXY_BASE_URL/v1/access_group" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "access_group_name": "consumer-support-mcp",
    "description": "Read-only knowledge tools for consumer support",
    "access_model_names": ["model-a"],
    "access_mcp_server_ids": ["'"$MCP_SERVER_ID"'"],
    "mcp_tool_permissions": {"'"$MCP_SERVER_ID"'": ["search", "fetch"]},
    "access_agent_ids": [],
    "access_skill_names": [],
    "assigned_team_ids": ["'"$TEAM_ID"'"]
  }'
```

Name groups after a capability, never after a person or a temporary session. `assigned_team_ids` is the stable boundary: team membership activates the grant, and you do not duplicate resources onto each user's key

### 3. Membership is the switch that turns a grant on for a person

```bash
curl -sk -X POST "$PROXY_BASE_URL/team/member_add" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "team_id": "'"$TEAM_ID"'",
    "member": {"user_email": "person@example.com", "role": "user"},
    "max_budget_in_team": 100.0
  }'
```

For a person who needs two different resource sets, add them to both teams. That is supported and it is the correct primitive. Then issue one key per team they actually need a credential for

### 4. Keys are scoped to one team, so issue one per team context

```bash
curl -sk -X POST "$PROXY_BASE_URL/key/generate" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "person-consumer-support",
    "user_id": "'"$USER_ID"'",
    "team_id": "'"$TEAM_ID"'",
    "duration": "90d",
    "metadata": {"purpose": "support-tools"}
  }'
```

Give the person a personal key for their default work plus a team-scoped key for each additional team context. The alternative for a JWT caller who holds several memberships is the `x-litellm-team-id` request header under `fallback_to_db_teams: true`, documented with its validation steps in [14-sso-application-integration.md](../access-control/sso-application-integration.md). A virtual key cannot do that switching; only the team it names applies

## What actually decides an MCP tool call

Grants resolve in layers, and later layers only narrow. The full treatment with source references is in [06-permissions-and-scope.md](../access-control/permissions-and-scope.md); the short version is what to check when a call is refused

| Layer | Control | Failure you will see |
|---|---|---|
| Route | key `allowed_routes` must include MCP routes | Every MCP call refused before grant logic runs. `llm_api_routes` alone reaches nothing |
| Server | `object_permission.mcp_servers`, or the team access group's `access_mcp_server_ids` | `403 access_denied: The key is not allowed to access server <id>` |
| Tool | `mcp_tool_permissions[server]`, or a toolset | `403` naming the tool |
| Parameter | server `allowed_params[tool]` | `403` naming the allowed parameter set |
| Content | guardrail scanners, `tool_policy` | Guardrail rejection before or after dispatch |

Two facts worth internalizing. An explicit `mcp_servers: []` means zero servers even for an admin, while `None` means "fall through to team grants". A legacy `mcp_access_groups` entry holds a source tag such as `mcp-source-<id>` and is not a unified access-group UUID; do not mix the two vocabularies

Upstream draws one distinction here that the layering table hides, and it matters when you inspect a key. For **MCP**, the owner's entitlement is a ceiling and never a grant: an empty key list inherits the team's servers, and admin ownership grants no MCP access at all. For **models**, the same is not true: a unified access group attached to a key can grant models outside the team's direct model list through `assigned_key_ids`, so a key-assigned group is widening rather than another ceiling. The practical consequence is to put shared resource bundles on teams and treat any access group you attach directly to a key as an intentional widening that someone reviews

## Team member permissions, and why self-service keys fail

Ordinary members get only `/key/info` and `/key/health` on their own team's keys. Provisioning an account and then telling the person to create a key will fail until the team grants the route

```bash
curl -sk -X POST "$PROXY_BASE_URL/team/permissions_update" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "team_id": "'"$TEAM_ID"'",
    "team_member_permissions": ["/key/generate", "/key/info", "/key/health"]
  }'
```

Grant the narrowest set that meets the need, and omit `/key/update` and `/key/delete` unless members genuinely need them. Two further gates matter. Assigning `access_group_ids` to a team key from a member account is default-deny and requires the `KEY_ACCESS_GROUP_ASSIGNMENT` permission, and a non-admin may never reassign a key's `user_id` because that is an ownership change, as recorded in [08-key-reassignment.md](../access-control/key-reassignment.md)

## Semantic tools, toolsets, and skills

These are three different things that get conflated

- `mcp_tool_permissions` is the exact per-server tool allowlist and is the safest default for a shared group. Empty `allowed_tools` on a server means every advertised tool is callable, so being explicit is the conservative choice
- `mcp_toolsets` are named permission bundles cached in Redis, reused across keys and teams. Use a toolset when "web-read-only" should mean the same list in five places
- `mcp_tool_search_enabled` hides tools from search until needed, and `search_tools` scopes which search tools a caller may invoke. With `general_settings.search_tool_deny_by_default` on, the key, team, and user layers must each list a search tool before it is callable, so a group grant alone is not enough there
- `access_skill_names` on an access group is not the same as skill content sharing. Current behavior does not make ordinary teammate skill rows shareable through that field alone, as recorded in [15-opencode-sso-mcp-skills-pilot.md](../../mcp/opencode-sso-oauth-skills-pilot.md). Do not promise skill sharing on the strength of an access-group edit

## Changing a grant without breaking it

`PUT /v1/access_group/{id}` is replacement-like for the arrays you send. Omitting `access_mcp_server_ids` or `assigned_team_ids` detaches what you leave out, and creation is not idempotent, so a duplicate name returns `409`

```bash
curl -sk "$PROXY_BASE_URL/v1/access_group/$ACCESS_GROUP_ID" \
  -H "Authorization: Bearer $LITELLM_API_KEY" | tee group-before.json

curl -sk -X PUT "$PROXY_BASE_URL/v1/access_group/$ACCESS_GROUP_ID" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @group-after.json
```

Always read first, merge deliberately, write the complete intended arrays, then read the group and each affected team back and compare. Never repost a shared group from memory. The handler also mirrors attachments into the team and key rows inside the same transaction and patches the auth caches, so a divergence between the group column and the team row is a bug to report rather than something to paper over with a second write

## Deletion and offboarding

Removing a person is membership removal plus credential revocation, in that order

```bash
curl -sk -X POST "$PROXY_BASE_URL/team/member_delete" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"team_id": "'"$TEAM_ID"'", "user_id": "'"$USER_ID"'"}'

curl -sk -X POST "$PROXY_BASE_URL/key/block" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key": "'"$KEY_HASH"'"}'
```

Do not delete the user row first, because that cascades to their keys and destroys the attribution of their spend. Do not disconnect ownership of a key with `user_id: null` as an offboarding step either: the person's historical spend stays attached to the token, and a null owner is a state you then have to reason about later. Block or delete the key and remove the membership

## Roles each step needs

| Step | Sufficient role |
|---|---|
| Create user, set role, set password | `proxy_admin` or `org_admin` |
| Create or update team | `proxy_admin`, `org_admin` for teams in their org, team admin for their own team |
| Create or update access group | `proxy_admin` for writes, `proxy_admin` or `proxy_admin_viewer` for reads |
| Add or remove team member | `proxy_admin`, `org_admin`, team admin |
| Generate a key | `proxy_admin`, team admin, or a member whose team carries `/key/generate` |
| Set `team_member_permissions` | `proxy_admin` |

Two role traps. `proxy_admin_viewer` can read access groups and keys but cannot write them. A global `internal_user_viewer` role is not a team-admin restriction, because the global role and the membership role are independent

## Verification, on dev

The public `litellm.ecouncil.ae` host is production. Test grants on the dev gateway through a port-forward, and re-run it after any dev rollout because the forward dies with the pod

```bash
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000
```

Then, with a real key for the person, prove four things in order. The key sees exactly the intended servers on the MCP listing route, a permitted tool call succeeds, a tool outside `mcp_tool_permissions` returns `403`, and a key scoped to team A stays limited to team A after its owner also joins team B. The last one is the regression that matters most, because it is the assumption people carry over from directory systems

## What this model cannot do

Be explicit with requesters about these boundaries rather than discovering them in production

- A single key cannot see two teams' resources. There is no union, no "primary team plus extras", and no per-request team switching on a virtual key. One narrow exception exists for standalone keys: `personal_key_model_access_from_teams: true` makes a key with no `team_id` fit model access available through at least one of its owner's teams. It is off by default and it is still not a union, so do not build a multi-team story on it
- Grants are not per-field lockable. An `org_admin` can edit any team inside their org, including one a proxy admin preconfigured. If a team must stay admin-controlled, structure it into an organization where only proxy admins hold `org_admin`, rather than expecting a field-level lock
- Access groups do not grant skill content sharing today, and `access_skill_names` should not be sold as if they do
- `mcp_toolsets` narrow tools but do not replace route-level `allowed_routes`. A correctly granted key behind `llm_api_routes` still reaches nothing

## Source references

| Behavior | Source |
|---|---|
| Access-group fields, `assigned_team_ids`, `assigned_key_ids`, `access_skill_names` | `litellm/types/access_group.py` |
| Create, update, delete, and the team/key/agent mirror plus cache patching | `litellm/proxy/management_endpoints/access_group_endpoints.py` |
| Baseline member permissions, `KEY_ACCESS_GROUP_ASSIGNMENT` gate | `litellm/proxy/management_helpers/team_member_permission_checks.py` |
| Search tool key/team/user layering and deny-by-default | `litellm/proxy/auth/auth_checks.py` |
| Permission field set | `litellm/types/object_permission.py` |
| Route, server, tool, parameter, and guardrail layers | `oicm-litellm-layer/../access-control/permissions-and-scope.md` |
| Team versus org admin arbitration | `litellm/proxy/management/teams/authz.py` |
| What a key inherits from its owner, the MCP-versus-models distinction, and `personal_key_model_access_from_teams` | Upstream docs.litellm.ai "Virtual Keys" and "How Key-Based Auth Works", checked 2026-11-26 |
