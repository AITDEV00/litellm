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

### 5. The worked example: one person, three teams, two bundles

Take a person who needs research tools in a research team, deployment tools in a platform team, and only chat models in a shared team. Create three teams, two capability groups, and three keys

| Object | Contents | Assigned to |
|---|---|---|
| Team `research` | `models: [model-a]`, budget 200 | person as `user` |
| Team `platform` | `models: [model-a, model-b]`, budget 300 | person as `user` |
| Team `shared-chat` | `models: [model-a]`, budget 50 | whole company |
| Group `research-mcp` | `access_mcp_server_ids: [papers]`, `mcp_tool_permissions: {papers: [search, fetch]}`, `access_model_names: [model-a]` | team `research` |
| Group `platform-mcp` | `access_mcp_server_ids: [deploy]`, `mcp_tool_permissions: {deploy: [status, logs]}` | team `platform` |

Give the person exactly three keys, each with one `team_id`: `person-research`, `person-platform`, and `person-shared-chat`. The outcome is deterministic and worth stating to the requester in these words

- `person-research` sees `model-a` and the `papers` server with `search` and `fetch` only. It cannot see `deploy`, and it cannot call a write tool on `papers` that is not in the permission list
- `person-platform` sees `model-a` and `model-b` and the `deploy` server with `status` and `logs`. It cannot see `papers`
- `person-shared-chat` sees `model-a` only and zero MCP servers. Its team carries no access group, so there is nothing to inherit
- If the person is removed from `platform`, `person-platform` fails on its next verification because a key pointing at a team the user no longer belongs to is not a working credential. Treat leaving a team as revoking its keys
- If `research-mcp` gains `[papers, arxiv]`, every `person-research` key sees `arxiv` immediately with no per-key edit. That is the reason to put servers on the team's group rather than on keys

The anti-pattern to refuse is a single `person-all` key with no team, expecting it to reach all three. It has no team, so it reaches no team servers at all, and its models are limited to the person's own ceilings unless `personal_key_model_access_from_teams` is enabled, in which case it sees the union of the three teams' models and still no MCP servers

## What actually decides an MCP tool call

Grants resolve in layers, and later layers only narrow. The full treatment with source references is in [06-permissions-and-scope.md](../access-control/permissions-and-scope.md); the short version is what to check when a call is refused

| Layer | Control | Failure you will see |
|---|---|---|
| Route | key `allowed_routes` must reach the MCP surface | A key with `allowed_routes: ['llm_api_routes']` reaches MCP inference routes, because `llm_api_routes` includes `mcp_inference_routes`. It does not reach MCP management routes such as `/v1/mcp/server`. A key with a narrow custom list that omits MCP paths is refused before grant logic runs |
| Server | `object_permission.mcp_servers`, or the team access group's `access_mcp_server_ids` | `403 access_denied: The key is not allowed to access server <id>` |
| Tool | `mcp_tool_permissions[server]`, or a toolset | `403` naming the tool |
| Parameter | server `allowed_params[tool]` | `403` naming the allowed parameter set |
| Content | guardrail scanners, `tool_policy` | Guardrail rejection before or after dispatch |

Do not tell a requester that `llm_api_routes` blocks MCP. It does not: `RouteChecks.is_llm_api_route` counts `mcp_inference_routes` and `agent_inference_routes` as LLM routes, and `non_proxy_admin_allowed_routes_check` passes `/v1/mcp/` and `/mcp-rest/` through to the MCP layer's own authorization. MCP access is decided by the server and tool layers below, not by a missing route entry. The route layer only matters when someone writes a narrow custom list that omits the MCP paths, or when they want management reads like `/v1/mcp/server` and only hold inference routes

Two facts worth internalizing. An explicit `mcp_servers: []` means zero servers even for an admin, while an absent value means "fall through to team grants". A legacy `mcp_access_groups` entry holds a source tag such as `mcp-source-<id>` and is not a unified access-group UUID; do not mix the two vocabularies

The exact server combination is in `get_mcp_server_access` (`litellm/proxy/_experimental/mcp_server/auth/user_api_key_auth_mcp.py`). When the team grants servers and the key grants none, the key inherits the team's set, unless `general_settings.require_key_mcp_access_defined` is on, which turns the team into a hard ceiling. When both grant servers, the result is their intersection. The key's own unified access groups are then unioned on top, so an access group you attach to a key widens past the team ceiling, while key `mcp_servers` and legacy tags cannot. For models the same asymmetry exists in the other direction: a key-level denial can be rescued by a key access group, because `common_checks` falls back to `key_access_group_grants_model` when the team check fails

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
- `access_skill_names` on an access group gates **Claude Code plugin visibility**, not skill content. It lets a key or team see already-registered private plugin rows whose names are listed, read by `litellm/proxy/anthropic_endpoints/claude_code_endpoints/claude_code_skill_access.py`. It never creates, copies, or shares content, and it has no effect on `/v1/skills`, whose `LiteLLM_SkillsTable` rows are filtered by owner scopes (`user:`, `team:`, `org:`, `key:`) and ignore this field. Its combine rule is also stricter than MCP: key-side and team-side skill grants **intersect** when both are non-empty, while MCP key groups are additive, and an unresolvable listed group collapses visibility to public plugins only. Do not promise skill sharing on the strength of an access-group edit

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

Run on the dev gateway on 2026-11-26 with real dev MCP servers, the team isolation half of that list held exactly. A person was made a member of three teams, `teama` and `teamb` each had their own access group listing one server, `chatonly` had none, and the person held one key per team

| Key | `GET /v1/mcp/server` | Call to the other team's server |
|---|---|---|
| `teama` | `['alpha.127.0.0.1.18201']` | `403 access_denied: The key is not allowed to access server f89a6b02...` |
| `teamb` | `['alpha.127.0.0.1.18203']` | `403 access_denied: The key is not allowed to access server d60a71f2...` |
| `chatonly` | `[]` | `403 access_denied: The key is not allowed to access server d60a71f2...` |

The user read back while keys were live, `member_add` returned `200` for all three teams, and each key still resolved one team's servers, which is the point of the model. Creating an access group with `assigned_team_ids` also mirrored its id onto `team.access_group_ids` immediately, so the team row and the group agree without a second write. The permitted-tool and disallowed-tool probes in that run returned `500 Tool ... not found` because the dev servers do not advertise the tool names used, so treat tool-level `403` as unproven by this run and verify it against a server whose tool list you know. Test teams, groups, keys, and the user were deleted afterwards, and a follow-up list showed no `mt-` teams or groups left behind

Re-run it after any dev rollout, because the port-forward dies with the pod

## What this model cannot do

Be explicit with requesters about these boundaries rather than discovering them in production

- A single key cannot see two teams' resources. The team the key names is the only team it resolves, and there is no union, no "primary team plus extras", and no per-request team switching on a virtual key. `get_mcp_server_access` refuses to fan out across teams on purpose (`_team_ids_for_mcp_grant` returns at most the key's own `team_id`)
- One exception exists for standalone keys and it is **model-only**. With `personal_key_model_access_from_teams: true`, a key that has no `team_id`, is not a proxy-admin key, and has a `user_id` may call any model that **any one** of its owner's teams can call. That is a union across the owner's teams (`can_personal_key_call_model_via_teams` passes if `any` team grants the model), which is the opposite of the single-team rule above. It is off by default, it never grants MCP servers or skills, and it evaluates current membership per request, so removing someone from a team narrows their standalone key. Use it for convenience keys, never as a substitute for per-team keys
- Grants are not per-field lockable. An `org_admin` can edit any team inside their org, including one a proxy admin preconfigured. If a team must stay admin-controlled, structure it into an organization where only proxy admins hold `org_admin`, rather than expecting a field-level lock
- Access groups do not grant skill content sharing today, and `access_skill_names` should not be sold as if they do
- `mcp_toolsets` narrow tools but do not replace the server layer. A toolset on a key whose team grants no servers still resolves nothing, and an explicit empty `mcp_servers` on the key still means zero

## Source references

| Behavior | Source |
|---|---|
| Access-group fields, `assigned_team_ids`, `assigned_key_ids`, `access_skill_names` | `litellm/types/access_group.py` |
| Single-team resolution for a key, and the team's `object_permission` copy onto the token | `litellm/proxy/auth/user_api_key_auth.py` |
| Model check order, key access-group fallback, and `personal_key_model_access_from_teams` union | `litellm/proxy/auth/auth_checks.py` (`common_checks`, `can_key_call_resolved_model`, `can_personal_key_call_model_via_teams`) |
| MCP key/team inheritance, intersection, and additive key-group grants | `litellm/proxy/_experimental/mcp_server/auth/user_api_key_auth_mcp.py` (`get_mcp_server_access`) |
| MCP routes bypassing the `allowed_routes` filter | `litellm/proxy/auth/route_checks.py` (`non_proxy_admin_allowed_routes_check`, `is_llm_api_route`) |
| Skill grant intersection and plugin-only visibility | `litellm/proxy/anthropic_endpoints/claude_code_endpoints/claude_code_skill_access.py` |
| Create, update, delete, and the team/key/agent mirror plus cache patching | `litellm/proxy/management_endpoints/access_group_endpoints.py` |
| Baseline member permissions, `KEY_ACCESS_GROUP_ASSIGNMENT` gate | `litellm/proxy/management_helpers/team_member_permission_checks.py` |
| Search tool key/team/user layering and deny-by-default | `litellm/proxy/auth/auth_checks.py` |
| Permission field set | `litellm/types/object_permission.py` |
| Route, server, tool, parameter, and guardrail layers | `oicm-litellm-layer/../access-control/permissions-and-scope.md` |
| Team versus org admin arbitration | `litellm/proxy/management/teams/authz.py` |
| What a key inherits from its owner, the MCP-versus-models distinction, and `personal_key_model_access_from_teams` | Upstream docs.litellm.ai "Virtual Keys" and "How Key-Based Auth Works", checked 2026-11-26 |
