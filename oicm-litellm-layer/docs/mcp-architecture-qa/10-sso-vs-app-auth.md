# SSO with Microsoft: does it help an application calling LiteLLM?

Short answer: browser SSO provides an interactive gateway login, not one universal bearer token for every model and MCP client. It creates a user row, a browser UI session, and normally a dashboard virtual key. Applications can instead forward a delegated Entra access token for the configured gateway API audience, use an appropriately team-scoped virtual key, or use browser-driven MCP OAuth for an MCP-only session

The 2026-11-23 claim that prod had no SSO/JWT configuration was true at that time and is now superseded. Prod/dev now have Microsoft OIDC configuration, and guide 16 proves browser SSO on both. `JWT_AUDIENCE` and exact `JWT_ISSUER` must be pinned and tested with real delegated tokens. Suggested database-backed team selection is not enabled until `fallback_to_db_teams: true` is genuinely rolled out and verified

---

## 1. What SSO with Microsoft actually issues

Walk the code in `litellm/proxy/management_endpoints/ui_sso.py`:

1. The browser hits `/sso/key/generate`, then `/sso/callback`.
2. The IdP (Microsoft) redirects back with a code; the gateway exchanges it.
3. The gateway upserts a user row and then **encodes a UI session JWT**: `encode_ui_session_jwt(returned_ui_token_object, master_key)`.
4. That JWT is set as a cookie (`set_session_token_cookie`) and returned.

Unless the experimental UI-login mode is deliberately enabled, SSO calls key generation for a virtual key on the `litellm-dashboard` team and embeds that `sk-...` credential in the returned UI token object. `encode_ui_session_jwt` then protects the browser session cookie. The cookie JWT and its embedded virtual key are not the same credential

| Credential | Issuance/meaning | Correct scope |
|---|---|---|
| Browser UI session JWT/cookie | Encrypted login session containing UI details, usually including a generated dashboard key | Browser dashboard management session, not an Entra access token for generic `/v1` calls |
| Dashboard virtual key | Usually `sk-...`, generated on SSO callback for the dashboard team, with UI-session expiry | A virtual-key API credential within its configured routes/models; not automatically a selected ENDUSERS team key |
| Delegated Entra access token | A Microsoft token for the configured gateway audience, representing the signed-in user | API JWT authentication, distinct from UI session validation, with scope/membership/team constraints |
| Gateway `llm_session_` MCP session | Created after native MCP/DCR OAuth via browser SSO, with paired refresh token | `/mcp` admission only, not model calls or `/v1/skills` |

So SSO can indirectly lead to model-capable credentials, but a browser cookie alone is not one. Programs should not extract dashboard credentials as a substitute for a documented delegated-user integration. Likewise, an ordinary `sk-` dashboard key is not an admin key, and the gateway MCP session is not an Entra token.

## 2. What SSO *does* buy you, and it is not nothing

Even if your app uses keys, SSO still improves the system, for four reasons:

1. **Identity provisioning.** Each person gets a real user row with their email, created on first login. That is the foundation for per-user budgets, per-user spend attribution, and per-user roles. Without SSO you either create users by hand or use keys that carry no identity.
2. **Role mapping from the directory.** `ui_sso` accepts `user_allowed_roles` and role/team mappings, so a Microsoft Entra group can map to `proxy_admin`, `org_admin`, or a team membership. That is how you get RBAC without an admin hand-picking each person's role.
3. **De-provisioning.** Remove a person from the Entra group and their access to the UI and role goes away on next login. With hand-minted keys you have to remember to revoke.
4. **Audit identity.** `created_by`, spend logs, and the audit trail can name the human, not "the admin made a key for someone".

So SSO is worth doing. It just sits one layer above the application-to-gateway credential question, not in place of it.

## 3. Application authentication options and boundaries

For application REST calls to LiteLLM, there are two normal choices below. Local MCP clients can additionally use gateway MCP OAuth, covered afterward.

### Option A: virtual key per application (the normal shape)

The app holds an `sk-...` key. You mint it with a narrow scope and attach it to a team, so budget and MCP grants follow.

- Pros: simple, works today, no extra config, budget and rate limits and MCP grants all apply, revocable and rotatable per app.
- Cons: a static secret to store and rotate, and it is not tied to the human who triggered the request unless you pass `end_user` or `user` metadata.
- How it fits SSO: SSO governs the **humans**; the app gets its own key owned by a team, not by a person. You do not want an app key owned by whoever created it, because then reassignment and de-provisioning get messy (see `08-key-reassignment.md`).

This is what I would recommend for a server-side application behind your own auth.

### Option B: JWT auth (federate the app's identity)

LiteLLM can accept a JWT as the bearer and map it to a virtual key. This is the `enable_jwt_auth` path in `litellm/proxy/auth/user_api_key_auth.py`:

- `general_settings.enable_jwt_auth: true`.
- A JWKS public key URL, audience, and issuer are configured so the gateway validates the signature.
- A **JWT key mapping** ties a claim value to a virtual key: `POST /jwt/key/mapping/new` (endpoints in `litellm/proxy/management_endpoints/jwt_key_mapping_endpoints.py`). So "the `sub` claim `app-prod` maps to virtual key X", and the app presents a JWT, the gateway resolves the mapping, and the request runs as that key.
- There is also auto-register: an unseen claim can mint a key on first sight.

- Pros: no static `sk-` in the app; the app's own identity provider (your application layer's auth, or even Microsoft, if the app already gets an Entra token for the user) issues the token, and revocation is at the IdP.
- Cons: needs `enable_jwt_auth` plus JWKS reachability from the gateway, and the claim-to-key mapping is a thing you maintain. In an airgapped pod, JWKS fetching is the caveat (same one noted in `09-rbac-and-full-potential.md`).

This is the closest thing to "Microsoft SSO helps my application": if your app can obtain a Microsoft/Entra token for the end user, and you turn on JWT auth, the gateway can accept that token directly and map it to a key. That is genuinely SSO reaching the API plane, but it is **JWT auth configured separately**, not `ui_sso`.

### Option C: OAuth2 / MCP auth passthrough (for MCP specifically)

Per-server `oauth2_flow`, `oauth_passthrough`, `dcr_bridge`, `token_exchange`, and `delegate_auth_to_upstream` still apply when the **upstream origin** needs identity; those are separate from the gateway caller. For a local MCP client such as OpenCode, the gateway's dynamic-client-registration and browser-sign-in flow issues its own `llm_session_` token. It carries limited user/team/resource-session context, not the delegated Entra bearer. Guides 15 and 16 describe that supported MCP partial pilot and its missing model-credential path.

## 4. Which to pick for your situation

Your application is a layer above LiteLLM and calls models and MCP servers.

- If the app is a **service** with its own auth and no per-user identity requirement: **Option A**, one key per app, owned by a team, with `end_user` passed for attribution. SSO handles the humans who administer and use the UI.
- If the app **already has a Microsoft/Entra token per user** and you want per-user budget and audit through the gateway: **Option B**, JWT auth, mapping the Entra `sub` or `oid` claim to a key or auto-registering. Then the human's identity flows to LiteLLM without the app holding a shared secret.
- If the app is **server-to-server with no user context**: Option A, and pass `user`/`end_user` explicitly if you need attribution.

The combination that gets you the most, given what is already set up:

1. SSO with Microsoft for UI login, mapped to `internal_user` by default, with an Entra group for `org_admin` and team mappings. This creates a real user per person.
2. Applications get team-owned keys with `key_type` presets, not person-owned keys.
3. If you later need per-user federation, add `enable_jwt_auth` with the Entra issuer and a JWT key mapping, so the same Microsoft identity works on the API plane without a shared secret.

## 5. Concretely, what changes today

Browser login is already deployed; do not reimplement it as part of ordinary application onboarding. For a delegated application:

1. Inspect a real gateway-audience access token. Set `JWT_PUBLIC_KEY_URL` to its tenant JWKS, `JWT_AUDIENCE` to the exact API resource, and `JWT_ISSUER` to its literal issuer
2. Map the immutable user identity (`oid`) and verified email (`upn`), reconciled with the canonical DB row before self-managed identity duplicates appear
3. Configure either mapped team claims or `fallback_to_db_teams: true`. The latter is still a rollout item, not a claim of working arbitrary team selection in the current config
4. Validate model and actual MCP calls on dev via port-forward before prod use, including membership-denied cases and both real team IDs

Use guide 14 for the normalized procedure. Temporary dashboard key outputs or a successful UI login do not establish that a direct Entra token has the right gateway audience and team identity

## 6. One thing to be careful about

The CLI login (`/sso/cli/poll`) and the MCP connector both rely on SSO or password exchange for **humans**. The connector specifically accepts only an ordinary user, not an admin, and mints its own scoped key. That is human-side. Do not try to route a server application's identity through the connector login path; it is not built for that.

## Source files

- `litellm/proxy/management_endpoints/ui_sso.py` `encode_ui_session_jwt`, `set_session_token_cookie`, `cli_poll_key`, provider setup
- `litellm/proxy/auth/user_api_key_auth.py` `is_jwt`, JWT key mapping lookup, auto-register
- `litellm/proxy/management_endpoints/jwt_key_mapping_endpoints.py` mapping CRUD
- `litellm/proxy/utils.py` `get_custom_url` / `PROXY_BASE_URL`
- `oicm-litellm-layer/deploy/overlays/prod/gateway/config.yaml` and dev counterpart: current JWT settings; this appendix title originated before OIDC/JWT deployment
- `oicm-litellm-layer/docs/admin-api/ATHENA-READ-ONLY-KEY-GUIDE.md` the SSO note
