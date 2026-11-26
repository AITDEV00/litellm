# Permissions and scope: dated prod evidence and the authorization layers

## Dataset status: read this before the historical inventory

Parts 1 and 2 preserve a **2026-11-23 read-only snapshot**, not live fleet counts. Native Microsoft/JWT SSO and MCP access grants were subsequently changed; do not follow their old zero-grant recommendations

Current read-only state verified 2026-11-24:

| Current object | Verified state |
|---|---|
| `ADEOGPT-ENDUSERS` | `b19ad7de-997e-45ec-ba34-f7980dccf621`, ADEO organization, `all-proxy-models`; assigned group `adeogpt-endusers-mcp` (`922c1bd2-d479-4134-970e-4daf1bd27ca4`) granting all eight source-broker MCP servers |
| `ADEOGPT-DESKTOP` | `d1406939-aef6-4205-a8b2-7616c5a72663`, separate selected model ceiling, no assigned MCP group at this read |
| `jyao@ecouncil.ae`, `ffkhan@ecouncil.ae`, and `dqvu@ecouncil.ae` | Ordinary members of `ADEOGPT-ENDUSERS`. jyao and dqvu also belong to `ADEOGPT-DESKTOP`. Do not infer Desktop MCP access from ENDUSERS membership |
| `athena-mcp@ecouncil.ae` | `internal_user` `f7bf9877-8f4f-4683-a43d-e24f5dd5c4ed`, created for connector use, no team at this read and no API key created by its provisioning step |
| `athena-read-only@ecouncil.ae` | A different `proxy_admin_viewer` principal from the connector user. Administrative safe-method reads do not grant model/MCP inference |

Use Parts 3 onward for authorization concepts and later implementation details. The historical raw inventory below is intentionally retained because it explains why specific grant/cleanup decisions were made. Do not use its key counts, team counts, usernames, or zero-grant conclusion as a current baseline

## Part 1: what was set up in prod on 2026-11-23

### The eight MCP servers

All eight are enrolled through the source broker and all are `active`:

| Short name | server_id (first 8) | public internet | allow_all_keys | access group |
|---|---|---|---|---|
| academic | `9cf462a5` | true | false | `mcp-source-9cf462a5-...` |
| books | `4024fe1a` | true | false | `mcp-source-4024fe1a-...` |
| indicators | `7bce03cc` | true | false | `mcp-source-7bce03cc-...` |
| podcasts | `c777ca69` | true | false | `mcp-source-c777ca69-...` |
| spotify | `a7df9f47` | true | false | `mcp-source-a7df9f47-...` |
| twitter | `984aa9c7` | true | false | `mcp-source-984aa9c7-...` |
| videos | `4300716d` | true | false | `mcp-source-4300716d-...` |
| web | `ab5a8b8a` | true | false | `mcp-source-ab5a8b8a-...` |

Shared shape of every one:

- `transport: http`, `auth_type: bearer_token`, pointing at `http://litellm-mcp-broker...:8888/sources/<id>/mcp`.
- `teams: []`, `allowed_tools: []`, `pinned_tools: null`, `allowed_params: null`, `disallowed_tools: null`, `mcp_toolsets: null`.
- `available_on_public_internet: true` is set on all eight. This is important and wrong-looking (see findings).
- `approval_status: active`, but `submitted_by`, `reviewed_at`, `review_notes` are all null. They were enrolled by an admin-created connector account rather than going through the UI approval flow, so the audit trail is empty.
- Created in a burst: all eight between 2026-11-20T09:02:32 and 09:02:39. Updated 09:18 the same day.

All 65 tools across the eight are live. Example tool names and required params:

| Source | Example tools |
|---|---|
| web | `...-search` (q), `...-fetch` (url), `fc_scrape`/`fc_crawl`/`fc_extract` (url/urls), `rss` |
| podcasts | `pod_search` (query), `pod_get`, `pod_episodes`, `pod_feed` |
| videos | `yt_search` (query), `yt_video`, `yt_channel`, `yt_playlist`, `yt_captions` |
| books | `books_search` (query), `books_volume`, `books_authors`, `books_covers` |
| academic | `paper_search` (query), `paper_get`, `paper_batch`, `paper_citations` |
| indicators | `fred_search`, `wb_indicator`, `who_indicator`, `eurostat_data`, `imf_data`, `oecd_data`, `nvd_cves` and many more |
| spotify | `search`, `lookup`, `artist_top`, `playlist_items`, `recommend` |
| twitter | `tw_user`, `tw_user_tweets`, `tw_search`, `tw_tweet`, `tw_list_followers` |

A real read-only call succeeded end to end:

```
POST /mcp-rest/tools/call
{"server_id":"ab5a8b8a-...","name":"prod.mcp.web....-search","arguments":{"q":"litellm proxy"}}
-> HTTP 200 in 2.15s, real web results through the tunnel
```

So the tunnel, the broker, the pool, and the connector are all working in prod for at least the admin key.

### The keys

Ten keys exist. Two shapes:

**Six `source-connector-*` keys**, created 2026-11-19 and 2026-11-20, owned by the two connector accounts:

| alias | user | allowed_routes | models |
|---|---|---|---|
| source-connector-61e9ea98... (x3) | adeogpt-mcp@ | `['/mcp-sources', '/mcp-source-link-sources/*']` | `[]` |
| source-connector-a863dcea... (x2) | ecas-mcp@ | `['/mcp-sources', '/mcp-source-link-sources/*']` | `[]` |

These are exactly the carve-out from `route_checks.py`: they can register sources and workers, and nothing else. Good scoping.

**Two bare keys with empty `allowed_routes` and empty `models`**, created 2026-11-19 13:19, owned by the same two accounts:

- `0c72c44e...` for ecas-mcp@, `5b070172...` for adeogpt-mcp@

These look like leftover login keys from before the `source-connector-*` scoping was added. Empty `allowed_routes` plus empty `models` is the most permissive shape a key can take, so these are worth checking. They are not the connector keys; they are unlabelled.

**One test key** `Twel-Test` owned by adeogpt-mcp@, `models: ['all-proxy-models']`, spend 0.006. Also unscoped in routes.

**Two real end-user keys**: `arifa@ECOUNCIL.AE` and `abbeesaveni@ECOUNCIL.AE`, both `team_id 05cc86a3` (DEVELOPER), `allowed_routes: ['llm_api_routes']`, `models` limited to four chat models, no MCP reach.

### Teams

25 teams. **Every single one has `access_group_ids: []`.** Not one team has been granted an MCP access group.

### Users

Seven:

| user | role | teams |
|---|---|---|
| default_user_id | proxy_admin | 25 teams, the master admin |
| adeogpt-mcp@ | internal_user | none |
| ecas-mcp@ | internal_user | none |
| adeogptdesktop@ | internal_user | ADEOGPT-DESKTOP |
| adeogpt@ | internal_user | 3 teams |
| roadmin@ | proxy_admin_viewer | none |
| athena-read-only@ | proxy_admin_viewer | none |

The two MCP connector accounts are `internal_user` with **no team**, which is exactly what the enrollment design expects.

### Guardrails

`GET /guardrails/list` returns `{"guardrails": []}`. There are **no guardrails configured at all**, global or per-server. No `tool_policy`, no content scanner, nothing.

## Part 2: the findings, stated plainly

1. **At this 2026-11-23 observation, MCP was live but not yet team-consumed.** No team/key then held `mcp_access_group`; ordinary scoped keys resolved zero MCP servers. Admin visibility was broader because explicit admin object permissions differed from normal keys. The current prod source-group assignments supersede this finding, not its dated diagnostic evidence

2. **Public-address eligibility is not an authorization grant.** The later code audit corrects an earlier interpretation here: `available_on_public_internet: true` permits a request from a public client IP through the address-eligibility check. It does **not** satisfy required route/server/group authorization. Setting it false would prevent remote public-IP users from reaching these sources even with otherwise valid grants. Keep it aligned with the intended reachable network, and enforce authorization through key routes, assigned groups/servers, and tool permissions. `allow_all_keys` is the distinct convenience switch whose careless use weakens least privilege.

3. **Use `GET /mcp-rest/tools/list?server_id=X` for filtered discovery.** The old `/v1/mcp/tools?server_id=X` observation invoked a different endpoint whose handler ignores that argument. In this source version the REST facade explicitly supports and filters by `server_id`; guide 14 shows the supported sequence. Do not pass a list/call field to the wrong route, then generalize the result into an authorization leak.

4. **The audit trail on the eight rows is empty.** `submitted_by`, `reviewed_at`, `review_notes` are null. They skipped the review path. If you want governance to mean anything, those rows should have come through `pending_review` and an admin approval, and the columns prove whether they did.

5. **Two unscoped bare keys exist on the connector accounts.** `0c72c44e...` and `5b070172...` have empty `allowed_routes` and empty `models`. Whatever created them before the `source-connector-*` scoping was introduced should be cleaned up, because an unscoped key on an account that can also enroll sources is more than either role needs.

6. **No keys have expiry.** `expires: None` on every key.

### Tested 2026-11-23: a real end-user key cannot reach MCP

Confirmed empirically on 2026-11-23 with `jyao@ECOUNCIL.AE` and a redacted ordinary production user key on team DEVELOPER `05cc86a3`:

| Field | Value |
|---|---|
| `team_id` | `05cc86a3` DEVELOPER |
| `models` | 11 chat, ASR, and TTS models, no MCP |
| `allowed_routes` | `['llm_api_routes']` |
| `object_permission.mcp_servers` | `[]` |
| `object_permission.mcp_access_groups` | `[]` |
| `object_permission.mcp_tool_permissions` | `{}` |
| `object_permission.mcp_toolsets` | `[]` |
| `object_permission.blocked_tools` | `[]` |

Results:

| Call | Result |
|---|---|
| `GET /v1/mcp/server` | `200 []`, sees zero servers |
| `GET /v1/mcp/tools` | `200 {"tools":[]}`, sees zero tools |
| `POST /mcp-rest/tools/call` on web source | `403 access_denied: The key is not allowed to access server ab5a8b8a-...` |
| `POST /mcp-rest/tools/call` on podcasts source | `403 access_denied: The key is not allowed to access server c777ca69-...` |
| `POST /mcp-rest/tools/call` with no `server_id` | `400 missing_parameter: server_id is required` |

This is the correct, intended behavior and it closes the open question from Part 2 finding 2: a key with `allowed_routes: ['llm_api_routes']` cannot reach MCP at all, and even if it could, the empty `mcp_access_groups` yields a clean 403 at the server check. The `available_on_public_internet: true` flag does **not** grant a `/llm_api_routes`-scoped key access. The residual concern in finding 2 stays narrower than it first looked: it applies only to a key with empty or MCP-inclusive `allowed_routes`, which today means the two unlabelled bare keys, not normal users.

The original test also used a different list route. In the current source `GET /mcp-rest/tools/list` is the compatible discovery surface, optionally with `server_id`. `POST` is not its listing method, and a method error is not an authorization result

## Part 3: what controls access, in order of application

The code has five layers, applied in this order when a tool call arrives. Each one narrows.

### Layer 1: route-level, does the key reach the MCP surface at all

`litellm/proxy/_types.py` `mcp_routes`:

- `mcp_inference_routes`: `/mcp-rest/tools/list`, `/mcp-rest/tools/call`, `/v1/mcp/tools`, `/introspect`, `/token`
- `mcp_management_routes`: `/v1/mcp/server`, `/v1/mcp/server/{path}`, `/v1/mcp/sessions`, `/mcp-sources`, `/mcp-source-broker/install/{grant_id}`, `/mcp-source-link-sources/{path}`

A key with `allowed_routes: ['llm_api_routes']` reaches none of these. A key with empty `allowed_routes` reaches all of them, subject to the next layers. This is why the two bare keys matter.

### Layer 2: server-level, which servers may this identity see

`mcp_server_manager.get_allowed_mcp_servers`. The precedence, straight from the source:

1. Admin with no explicit `object_permission.mcp_servers`: the whole registry.
2. Explicit `object_permission.mcp_servers` set (even `[]`, even for admin): exactly that list, intersected with later ceilings.
3. The sentinel `no_mcp_servers` in `mcp_servers`: zero, absolutely.
4. Otherwise the union of: key/team explicit `mcp_servers`, key/team `mcp_access_groups`, key/team toolsets, operator `allow_all_keys` servers, and BYOM submitted servers.
5. Then every later level intersects as a ceiling, never a widening.

There are two distinct grant mechanisms:

- Legacy `object_permission.mcp_access_groups` contains source tags such as `mcp-source-<source_id>`. Databases may store these on key/team object permissions; these are not unified access-group UUIDs.
- Unified `assigned_team_ids` / `assigned_key_ids` on `LiteLLM_AccessGroupTable` reference reusable `access_mcp_server_ids`, optionally models, agents, skills, and tool permissions. A team's attached group supplies grants at runtime as current membership/team context is resolved.

The legacy tag helper intersects key groups with team tag groups when a team declares them. This is not a blanket rule for unified key-assigned groups: in the current key resolver, explicit unified key groups are additive extras above the key/team ceiling. Use team-assigned groups as the stable boundary, and review a key's own unified group assignment as a possible widening rather than calling all attached groups downward intersections

### Layer 3: tool-level, which tools on an allowed server

`get_allowed_tools_for_server` computes `key_tools` and `team_tools` and combines them: both restricted means intersection, only team means inherit from team, neither means no restriction. Three granularities exist:

- `mcp_tool_permissions: {server_id_or_name: [bare_tool_name, ...]}`. The most precise control. `None` means no restriction on that server, `[]` means nothing on that server.
- `mcp_toolsets: [toolset_id]`. Named bundles of tool permissions you can reuse across keys and teams, resolved through `resolve_toolset_tool_permissions` and cached in Redis.
- Server-level `allowed_tools: []`. Empty means no tool allowlist, so every tool the server advertises is callable. In prod every server has this empty.

Enforcement on the call path is `check_tool_permission_for_key_team`, which raises 403 naming the tool.

### Layer 4: parameter-level, which arguments a tool may receive

Two mechanisms, both enforced before dispatch:

- **Per-tool allowed params**: `server.allowed_params[tool] = [param, ...]` checked by `validate_allowed_params`. Any argument not in the list is a 403 with a message naming the allowed set. This is the closest thing to input guardrailing at the schema layer, and it is configured nowhere in prod today.
- **Server-level `allowed_tools` / `disallowed_tools`** gate which tools exist for the server at all, via `check_allowed_or_banned_tools`.

Note the pitfall: `allowed_params` is an allowlist, so if you enable it on a tool you must list every param the model legitimately needs, including optional ones.

### Layer 5: guardrail scanners, content inspection of arguments and results

`litellm/proxy/_experimental/mcp_server/guardrail_translation/handler.py`, `MCPGuardrailTranslationHandler`, registered for `CallTypes.call_mcp_tool` and `CallTypes.list_mcp_tools`. On an MCP tool call it:

- `process_input_messages`: collects the tool description, every `description` string leaf of the input JSON schema, and every string leaf of the arguments into one `GenericGuardrailAPIInputs`, calls the guardrail, then writes masked text back into arguments and schema descriptions. If the guardrail rejects, it raises and the call never happens. A conflicting rewrite of the same path is blocked rather than silently applied.
- `process_output_response`: scans `content` text blocks and `structuredContent` and masks or raises. It rewrites in place on purpose so the spend log and span do not keep the unmasked text.

This is the real parameter- and result-guardrail hook, and it works with **any** `CustomGuardrail`, so presidio, a prompt-injection scanner, a language filter, or a bespoke class all plug in here.

### The `tool_policy` guardrail

`litellm/proxy/guardrails/guardrail_hooks/tool_policy/tool_policy_guardrail.py`. Separate from the scanners above. It reads `LiteLLM_ToolTable.input_policy` and `output_policy` and the `blocked_tools` list on key/team object permissions:

- `blocked` input policy: raise, tool never runs.
- `trusted`: only allowed if the conversation carries no untrusted tool output.
- `untrusted`: allowed, default.

It is configured as a normal `guardrails:` entry with `guardrail: tool_policy`, `mode: post_call`. Not enabled in prod.

## Part 4: what I would call best practice here, grounded in this code

Ranked by how much risk each closes for how little work.

1. **Keep network reachability and authorization as separate controls.** `available_on_public_internet: true` is needed for a remote public-IP caller of these sources. If a source should be private-network only, disable the flag intentionally and prove the expected private client addresses remain reachable. It is not a replacement for grants or URL/content policy

2. **Grant per team through unified assigned access groups.** The actual current example is `adeogpt-endusers-mcp`, listing server IDs and `assigned_team_ids` for ENDUSERS. Desktop has no such grant in the 2026-11-24 read. Use separate bundles when consumers should receive different servers; do not let ADEOGPT-DESKTOP acquire the ENDUSERS grant implicitly just because several users belong to both. Continue key-by-key cleanup for old unscoped or no-expiry tokens; team/group grants do not override their route-level configuration

3. **Use `mcp_tool_permissions` or a toolset to expose only the read tools.** Every source has read and potentially expensive or write-ish tools (`fc_crawl`, `fc_extract`, `tw_search`, `yt_captions`). `mcp_tool_permissions` is exact and per-server. A toolset lets you define "web-read-only" once and reuse it. Because empty `allowed_tools` means all tools, being explicit is the safe default.

4. **Use `allowed_params` on any tool that takes a URL or a free-text query into an external system.** `fetch`, `fc_*`, `tw_search`, `yt_captions` are the obvious ones. This is enumeration-resistant parameter restriction natively supported, and it is off everywhere.

5. **Turn on a guardrail for input and output scanning.** Zero guardrails exist today. `MCPGuardrailTranslationHandler` makes it a config change, not code. A PII scanner plus a prompt-injection scanner on inputs, and a PII scanner on outputs, is the minimum. Note the handler scans schema descriptions too, which is a nice detail because tool metadata is otherwise an injection surface.

6. **Add `tool_policy` if you want the trust tiering.** It is the only mechanism that reasons about "this tool's output might be tainted", which matters when one tool's results feed a later tool call.

7. **Clean up the two unscoped bare keys and add expiry everywhere.** Cheap, closes a real gap.

## Part 5: guardrailing MCP parameter input specifically, yes it is possible

Three distinct places, each catching a different class of problem:

| Need | Mechanism | Where |
|---|---|---|
| Deny arguments outside a fixed set | `allowed_params` per tool, 403 on extras | `validate_allowed_params` |
| Scan argument strings for PII, injection, banned content | `MCPGuardrailTranslationHandler.process_input_messages` with any `CustomGuardrail` | guardrail_translation |
| Deny the tool entirely based on trust state | `tool_policy` input policy `blocked` / `trusted` | tool_policy guardrail |
| Limit which tools exist at all | server `allowed_tools` / `disallowed_tools` and `mcp_tool_permissions` | `check_allowed_or_banned_tools`, `get_allowed_tools_for_server` |

What is **not** supported natively: per-argument JSON Schema validation beyond what the MCP server itself validates. The gateway passes arguments through after its checks; it does not re-validate against `inputSchema`. If you need schema-level enforcement, an argument-validating `CustomGuardrail` in `process_input_messages` is the extension point, and it has the schema available in the same call.

## Reference: the files that implement all of this

- `litellm/proxy/_types.py` `mcp_routes`, `mcp_inference_routes`, `mcp_management_routes`
- `litellm/proxy/_experimental/mcp_server/mcp_server_manager.py` `get_allowed_mcp_servers`, `check_tool_permission_for_key_team`, `validate_allowed_params`, `check_allowed_or_banned_tools`
- `litellm/proxy/_experimental/mcp_server/auth/user_api_key_auth_mcp.py` `get_allowed_tools_for_server`, `is_tool_allowed_for_server`, `get_mcp_access_groups`, the key/team/toolset inheritance
- `litellm/proxy/_experimental/mcp_server/guardrail_translation/handler.py` input and output scanning
- `litellm/proxy/guardrails/guardrail_hooks/tool_policy/tool_policy_guardrail.py`
- `litellm/types/object_permission.py` the permission field set
- `litellm/proxy/schema.prisma` `blocked_tools`, `allowed_params`
- `litellm/proxy/auth/route_checks.py` the self-service carve-outs
- `oicm-litellm-layer/docs/guides/mcp/MCP_SOURCE_AGENT_GUIDE.md` the known-limitation entries
