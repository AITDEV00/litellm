# OpenCode Dev Pilot Evidence (D2 core)

Record of the OpenCode V2 Microsoft SSO + MCP OAuth pilot executed against the dev gateway on 2026-11-24, following [the implementation plan](opencode-sso-oauth-skills-pilot.md). This page documents what was actually run and proven so far. The model-access leg of D2, D3, D4, and D5 are not complete; their status is recorded under Open items

## Environment

| Component        | Value                                                                                                                                                                                        |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Gateway          | `litellm-proxy-dev` (dev cluster `adeo-litellm`, 1 replica)                                                                                                                              |
| Gateway image    | `registry.adeoaiengine.ecouncil.ae/openinnovationai/platform/mlops/mlops-serving/litellm-src:jya0-v1.108.5`                                                                                |
| Gateway access   | `kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000`, then `http://localhost:14001`. The dev gateway has no ingress; this forward is the only valid dev evidence path |
| OpenCode         | `opencode v2.0.27` (Linuxbrew install, unmodified)                                                                                                                                         |
| Pilot person     | `jyao@ecouncil.ae`, canonical user_id `90714f16-39de-400f-9a79-09edd1c48eed`                                                                                                             |
| Pilot team       | `ADEOGPT-ENDUSERS`, team_id `645ddec6-73d5-4a7f-b54e-c171672e81c3` (org `ADEO` `67a7a083-bee7-4a4c-accd-2749dfcecec9`)                                                               |
| MCP access group | `adeogpt-endusers-mcp` `7b32b866-e4b7-4cba-b72b-e4ec366f039a`, granting exactly the 4 fixture servers                                                                                    |
| Granted servers  | `alpha.127.0.0.1.18201`, `alpha.127.0.0.1.18203`, `beta.127.0.0.1.18202`, `beta.127.0.0.1.18204`                                                                                     |

## Prerequisites to replicate

```bash
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000 &
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:14001/health/liveliness
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:14001/.well-known/oauth-protected-resource/mcp
```

Both must return `200` before starting. The broker tunnel forward (`kubectl -n adeo-litellm port-forward svc/litellm-mcp-broker-dev 18093:8093`) and the adma connector must be running for the fixture servers to answer tool calls; see the D0/D1 notes in the session record for those steps

The four fixture origins and their connector workers must be live (they are dev-only processes outside the repo, under a gitignored scratch root). If `GET /v1/mcp/server` (admin auth) lists the 4 servers with healthy workers, the fixtures are up

### Replication caveat: logout before re-auth

A re-auth over an existing credential fails with `{"error":"invalid_request","error_description":"redirect_uri is not registered for this client"}`. The cause: `opencode mcp auth` reuses the DCR client registration stored in the previous credential instead of registering a new one, while its loopback callback listener binds a fresh port each run; the gateway seals each registered client with its exact `redirect_uris` and rejects any other value at `/authorize` (`_rejected_authorize_request` in `gateway_dcr_flow.py`). Always run `opencode mcp logout adeogpt-dev` before `opencode mcp auth adeogpt-dev`, and use the newly printed authorize URL (a stale browser tab on the old URL keeps the old client_id and keeps failing). Related gateway finding, recorded not changed: RFC 8252 section 7.3 says an authorization server must allow any port for loopback IP redirect URIs, and this exact-match check is stricter than that, which is what makes the failure mode possible

## Pilot project configuration

Isolated project config at a scratch path (gitignored, outside the repo), exactly:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "adeogpt-dev": {
        "type": "remote",
        "url": "http://localhost:14001/mcp"
      }
    }
  }
}
```

No `headers`, no `oauth: false`, no Entra bearer, no `x-litellm-api-key`, no master key. Native OAuth left enabled. All commands below run from that project directory

## Proven: native MCP OAuth as the real person

Sequence run (2026-11-24, ~11:16 to 11:22 UTC):

```bash
opencode mcp list                  # before auth: "needs authentication"
opencode mcp auth adeogpt-dev     # starts DCR + loopback listener, prints authorize URL
# ... browser steps as the signed-in human ...
opencode mcp list                  # after: "connected"
```

What happened at each hop, verified in gateway source and OpenCode logs:

1. OpenCode fetched `/.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-authorization-server/mcp` on `http://localhost:14001`, then did dynamic client registration at `POST /register`. Issued client IDs carry the sealed `llm_dcrc_` prefix
2. `opencode mcp auth` opened a loopback callback listener on `http://127.0.0.1:<port>/callback` (port chosen by OpenCode; observed 37809 on the first run, 46213 on the second) and printed the authorize URL: `GET /authorize/mcp-session` with `response_type=code`, PKCE `code_challenge_method=S256`, `state`, and `resource=http://localhost:14001/mcp`
3. Navigating the authorize URL without a UI session redirected through the gateway's Microsoft SSO path (`GET /sso/key/generate`, Microsoft Entra tenant `99cf3350-40c8-4260-bb84-1dada4a029c0`, callback `GET /sso/callback`) and landed on the connect page `GET /ui/connect?connect_flow=<handle>` with the per-flow cookie. The Microsoft interactive login as `jyao@ecouncil.ae` (with MFA) was completed by the human in the real browser; no password was scripted, pasted, or captured anywhere
4. The connect page header read `Account menu - Internal User - signed in as jyao@ecouncil.ae`, and listed exactly the 4 team-granted servers, not the wider registry. Each server was connected via its Connect button (4 total: `alpha.127.0.0.1.18201`, `alpha.127.0.0.1.18203`, `beta.127.0.0.1.18202`, `beta.127.0.0.1.18204`), then Finish connecting
5. `POST /authorize/complete` minted the single-use auth code and 303-redirected to the loopback callback. OpenCode exchanged it at `POST /token` with its PKCE verifier. OpenCode's log recorded `mcp oauth authorization completed ... hasRefreshToken=true expiresIn=3600` (timestamps 2026-11-24T11:17:45Z admin-session run, 2026-11-24T11:22:06Z jyao run)

An earlier full round was first completed while the browser still held the admin UI session; it minted successfully but against the wrong identity, so it was discarded: `opencode mcp logout adeogpt-dev` removed that credential, the UI `token` cookie was cleared (which returns the dashboard to `/ui/login`), and the entire flow was re-run so the session mint is attributable to `jyao@ecouncil.ae`'s Entra login. Only the jyao-attributed round counts as D2 evidence

## Proven: credential type and storage

The credential OpenCode stores is the gateway MCP session token, not a Microsoft token and not a virtual key:

- Gateway source: session access tokens are signed JWTs with prefix `llm_session_` (`SESSION_TOKEN_PREFIX`), issuer claim `litellm-mcp-gateway` (`SESSION_ISSUER`), 1h TTL (`SESSION_TTL_SECONDS = 3600`); the paired refresh token has prefix `llm_srefresh_` and a 14-day TTL (`SESSION_REFRESH_TTL_SECONDS = 1209600`), single-use with family rotation, per `litellm/proxy/_experimental/mcp_server/outbound_credentials/session_token.py`
- OpenCode stored it in its own credential store: the `credential` table of `~/.local/share/opencode/opencode.db`, integration `mcp_c3326e473ea171da`, label `adeogpt-dev`, value beginning `{"type":"oauth"...` (contents never printed). Nothing lives in the project config or repo
- The DB check for divergence: the only verification-token row minted for user `90714f16-39de-400f-9a79-09edd1c48eed` during the pilot is the `litellm-dashboard` UI login session (created 2026-11-24 11:19:53, expires +24h), which is the SSO browser session, not the MCP session. No duplicate user record was created; the Entra login mapped onto the existing canonical row `90714f16-...` / `jyao@ecouncil.ae`

## Proven: identity and team resolution at request time

From `user_api_key_auth_mcp.py` (`_admit_gateway_session`) and `session_token.py` (`SessionPrincipal`), the session token seals only `user_id`, `client_id`, optional `resource_server_ids`, and optional `team_id`. On every MCP request the gateway re-resolves the live user by `user_id` via `reload_admitted_user` and re-runs the centralized policy gate, so team membership and grants are enforced at use time, not frozen at mint time. The connect page's grant list (exactly the 4 servers above) is that live resolution: `jyao` is a member of team `645ddec6-73d5-4a7f-b54e-c171672e81c3` and the team's `adeogpt-endusers-mcp` access group carries the 4 server IDs. The negative identity `opencode-negative-1` (created D1, not in the team) was proven unable to list the team's servers or keys during D1 testing

## Proven: logout and re-login

`opencode mcp logout adeogpt-dev` printed `Removed OAuth credentials for adeogpt-dev`, `opencode mcp list` then reported `needs authentication`, and a fresh `opencode mcp auth` round re-established `connected`. This proves the local credential is destroyed on logout and the flow is reproducible end to end

## Open items (paused deliberately)

### Model access via the SSO session: not possible; model routes require a virtual-key bearer

The MCP session JWT that OpenCode obtains through the Microsoft SSO OAuth flow **cannot call the model (`/v1/chat/completions`) routes**. This was proven live on 2026-11-24 against `http://localhost:14001`, and it matches the gateway source: session-bearer admission (`_admit_gateway_session` in `litellm/proxy/_experimental/mcp_server/auth/user_api_key_auth_mcp.py`) is wired only into the MCP route handler (`handle_streamable_http_mcp`), while the standard `/v1` auth pipeline (`litellm/proxy/auth/user_api_key_auth.py`) has no arm for the `llm_session_` token family (its session handling covers only the experimental UI login blob, a different token family). In other words, the SSO login gives OpenCode MCP access; it does not give it a proxy API credential for model calls

Live evidence, all three observed the same morning:

| Request                                                               | Bearer                                               | Result                                                                                                                |
| --------------------------------------------------------------------- | ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `POST /mcp` (JSON-RPC initialize)                                   | the stored `llm_session_` JWT                      | `HTTP 200` (MCP surface accepts it)                                                                                 |
| `POST /v1/chat/completions`, model `zai-org/GLM-5.3-Flash`        | the same `llm_session_` JWT                        | `HTTP 401` `auth_error` (the standard pipeline has no arm for this token family; see permissions ruled out below) |
| `POST /v1/chat/completions`, model `zai-org/GLM-5.3-Flash`        | jyao's SSO-minted dashboard virtual key (`sk-...`) | `HTTP 200`, content `MODEL_OK`                                                                                    |
| `POST /v1/chat/completions`, model `zai-org/GLM-5.3-Flash`        | pilot team virtual key                               | `HTTP 200` with a real completion                                                                                   |
| `POST /v1/chat/completions`, model not in the team's permitted list | pilot team virtual key                               | `HTTP 403` `team_model_access_denied`                                                                             |

Note the gateway does have a designed path that mints a true proxy credential (an `sk-...` virtual key bound to user and team) from an authenticated session: the `token-exchange`/proxy-credential grant in the DCR flow (`proxy_credential_response` in `litellm/proxy/_experimental/mcp_server/gateway_dcr_flow.py`, `POST /token` with the appropriate grant/audience). That is how a signed-in person could obtain a model-capable credential without an admin handing them a static key. Whether OpenCode v2.0.27 can be driven to perform that exchange itself (via its provider auth integration, `opencode auth login --method <key-type>`, or a small helper) is exactly the open question handed to the search agent below

### Permissions ruled out as the cause (verified 2026-11-24 via admin API + DB)

The 401 on `/v1` is not a permission or access-group misconfiguration for `jyao@ecouncil.ae`. All of the following were checked live and are correctly configured:

| Check                                           | Result                                                                                                                                                                                           |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| User record (`/user/info`)                    | `internal_user`, member of teams `645ddec6...` (pilot) and `6e858610...` (legacy)                                                                                                          |
| Team membership (DB `LiteLLM_TeamMembership`) | `jyao` -> `645ddec6-73d5-4a7f-b54e-c171672e81c3` confirmed                                                                                                                                   |
| Team models (`/team/info`)                    | `['zai-org/GLM-5.3-Flash']` on the pilot team                                                                                                                                                  |
| Access groups (DB `LiteLLM_AccessGroupTable`) | `adeogpt-endusers-mcp` (4 server IDs), `adeogpt-endusers-models` (`zai-org/GLM-5.3-Flash`), `adeogpt-endusers-skills` (`opencode-pilot-fixture`), all assigned to team `645ddec6...` |
| `/v1/models` as pilot team key                | exactly `['zai-org/GLM-5.3-Flash']`                                                                                                                                                            |

Decisive positive test: the SSO UI login itself mints jyao a real virtual key (row: user `90714f16-39de-400f-9a79-09edd1c48eed`, team `litellm-dashboard`, 24h expiry, created 2026-11-24 11:19:53; the dashboard sends it as `Authorization: Bearer sk-...`). Calling `POST /v1/chat/completions` with that key returned `HTTP 200` with content `MODEL_OK` for `zai-org/GLM-5.3-Flash`. So the person's permissions are fully sufficient for model calls; what is missing is only that OpenCode holds the MCP session JWT (which the `/v1` pipeline does not accept) instead of any `sk-...` proxy credential

Two additional observations from this verification, for completeness:

- The dev config has `general_settings.enable_jwt_auth: true` with Entra JWKS (`JWT_PUBLIC_KEY_URL=login.microsoftonline.com/99cf3350-.../discovery/v2.0/keys`, `JWT_AUDIENCE=e1ad1372-fbaa-4013-a288-8547d8b7dbe7`). Any JWT-shaped bearer on `/v1` is routed to that enterprise JWT-auth validator first. That is why the MCP session JWT produced a JWKS/parse error rather than a clean 401, and why the dashboard's cookie JWT (an HS256 UI-session JWT, not an Entra token) also fails if presented raw. The dashboard's actual API calls use the minted `sk-...` key, never the cookie JWT
- `/access_group/list` on this build returns an empty list and `/access_group/{id}/info` 404s even for existing groups; the DB table is the reliable read. Treat the access-group REST surface as not-fully-exposed on this gateway version

### What the search agent should investigate

Goal: find the supported way for OpenCode v2.0.27 to authenticate model calls to a LiteLLM gateway as the signed-in user, with no long-lived/static credential in OpenCode's config, no gateway master key, and ideally reusing the SSO identity. Candidate directions, in order:

1. **OpenCode provider auth integration**: `opencode auth login <target> --method <method-id> --answer apiKey=...` stores credentials in OpenCode's credential store (`~/.local/share/opencode/opencode.db`, `credential` table) rather than config. Determine the correct `--method` for a custom `@ai-sdk/openai-compatible` provider (binary strings show `method:{type:"key",...}` shapes and a "Gateway API token" label) and whether that credential is then used for `provider.options.apiKey` automatically
2. **Whether OpenCode supports OAuth for custom providers** the way it does for MCP servers and its own console (`OPENCODE_API_KEY` + connection flow is console-only): is there a documented provider-level OAuth or OIDC flow for openai-compatible endpoints?
3. **The gateway-side token exchange**: whether LiteLLM's `/token` endpoint with the `token-exchange`/`urn:ietf:params:oauth:grant-type:token-exchange` grant and the proxy-API audience can be scripted from OpenCode's stored session (see `proxy_credential_response` and the `PROXY_API_AUDIENCE` paths in `gateway_dcr_flow.py`; also the `lite login` CLI flow in `litellm/proxy/client/cli/commands/auth.py` as a reference implementation of exactly this exchange)
4. **Official LiteLLM + OpenCode integration docs**: how LiteLLM documents OpenCode integration (e.g. `docs.litellm.ai` OpenCode pages, LiteLLM blog/recipes) and what credential pattern they prescribe

Already ruled out empirically (do not re-search): `OPENCODE_API_KEY` env var (only feeds OpenCode's own console provider), `{env:VAR}` interpolation in `provider.options.apiKey` (documented for MCP server headers only; did not expand), and any automatic env fallback in the bundled `@ai-sdk/openai-compatible` package. Also: the team virtual key works perfectly as a bearer when injected via `options.apiKey`, but that puts a static credential in config, which the pilot forbids

Reference docs for the agent:

- OpenCode v2 docs: `https://opencode.ai/v2/docs/` (config, providers, auth); migration guide `https://opencode.ai/v2/docs/migrate-v1`
- OpenCode repo: `https://github.com/sst/opencode` (provider/auth source under `packages/opencode/src/provider/`)
- LiteLLM gateway DCR/token-exchange source in this checkout: `litellm/proxy/_experimental/mcp_server/gateway_dcr_flow.py` (`proxy_credential_response`, `aggregate_token`), `litellm/proxy/_experimental/mcp_server/outbound_credentials/session_token.py` (token shapes)
- LiteLLM docs: `https://docs.litellm.ai/` (proxy API key auth, virtual keys, and any OpenCode recipe)

## Remaining D2/D3+ items (not model related)

- Refresh after expiry (1h TTL) has not been exercised yet: the token from 11:22 UTC expires at 12:22 UTC; a re-run of `opencode mcp list` after that moment (or forcing a refresh) plus a gateway log check closes this
- Multi-replica DCR/session behavior: dev runs 1 replica; the plan's second-pod check needs a temporary scale-out, pending
- D3 (per-server native tool calls through OpenCode), D4 (private skills), D5 (unit tests, cleanup record, this doc's final form) are not started

## Cleanup obligations carried forward

- Delete the `jyao-adeogpt-mcp` virtual key via `/key/delete` once the pilot no longer needs it
- Rotate the exposed Entra dev client secret per plan step 1.3.8 (user-assisted; requires Entra access)
- Remove the dev fixture skill rows, `opencode-negative-1`, the pilot team key, and the scratch fixture processes when D5 completes
- `public_skills_index` and `enable_mcp_registry` remain off throughout
