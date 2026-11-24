# SSO with Microsoft: does it help an application calling LiteLLM?

Short answer: **SSO solves human login to the Admin UI. It does not by itself give your application a credential to call the models or MCP servers.** For a machine caller you want a virtual key, or JWT auth if you want to federate the app's own identity. This file explains why, and the three real options.

Verified against prod 2026-11-23: `general_settings` in the deployed config has no `ui_sso` and no `enable_jwt_auth`. So neither is on today.

---

## 1. What SSO with Microsoft actually issues

Walk the code in `litellm/proxy/management_endpoints/ui_sso.py`:

1. The browser hits `/sso/key/generate`, then `/sso/callback`.
2. The IdP (Microsoft) redirects back with a code; the gateway exchanges it.
3. The gateway upserts a user row and then **encodes a UI session JWT**: `encode_ui_session_jwt(returned_ui_token_object, master_key)`.
4. That JWT is set as a cookie (`set_session_token_cookie`) and returned.

So SSO produces:

| Artifact | What it is | Where it works |
|---|---|---|
| UI session JWT | a signed session token tied to the user, with an expiry | the Admin UI, and API routes **only if** `enable_jwt_auth` is on |
| a user row | `LiteLLM_UserTable` entry with the email as identity, plus the role from your SSO mapping | the DB, for governance |
| a CLI JWT | the same session token surfaced to `lite login` / CLI polling | the CLI |

It does **not** mint a virtual key (`sk-...`). That is a deliberate design choice: a browser session cookie is short-lived and not something a daemon or a server application should hold.

So the direct answer: **if your application is a machine calling `/chat/completions` or `/mcp-rest/tools/call`, Microsoft SSO alone gives it nothing.** The app cannot do an interactive Microsoft login, and even if it got the JWT, without `enable_jwt_auth` the gateway rejects it on API routes as a non-matching bearer.

## 2. What SSO *does* buy you, and it is not nothing

Even if your app uses keys, SSO still improves the system, for four reasons:

1. **Identity provisioning.** Each person gets a real user row with their email, created on first login. That is the foundation for per-user budgets, per-user spend attribution, and per-user roles. Without SSO you either create users by hand or use keys that carry no identity.
2. **Role mapping from the directory.** `ui_sso` accepts `user_allowed_roles` and role/team mappings, so a Microsoft Entra group can map to `proxy_admin`, `org_admin`, or a team membership. That is how you get RBAC without an admin hand-picking each person's role.
3. **De-provisioning.** Remove a person from the Entra group and their access to the UI and role goes away on next login. With hand-minted keys you have to remember to revoke.
4. **Audit identity.** `created_by`, spend logs, and the audit trail can name the human, not "the admin made a key for someone".

So SSO is worth doing. It just sits one layer above the application-to-gateway credential question, not in place of it.

## 3. The three ways your application authenticates, and which to choose

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

For MCP, there is a separate family of options: `oauth2_flow` on the MCP server, `oauth_passthrough`, `dcr_bridge`, `token_exchange`, `delegate_auth_to_upstream`, per-server OAuth discovery. These matter when the **origin** needs the user's identity, not when your app needs to reach the gateway. For your case (app calls the gateway's MCP tools), Option A or B applies, across the gateway hop; C is about what happens after the tunnel reaches the origin.

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

Nothing, until you enable it. Currently:

- `general_settings` in the deployed config has no `ui_sso`, so the Admin UI uses username/password only.
- No `enable_jwt_auth`, so a JWT presented as a bearer is rejected on API routes.
- The application should therefore use a virtual key today.

To make SSO real, the config additions are `general_settings.ui_sso` with `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_TENANT`, `PROXY_BASE_URL` set to the browser-reachable edge host, and role/team mappings. To additionally federate the app, add `enable_jwt_auth: true`, `JWT_PUBLIC_KEY_URL` pointed at the Entra JWKS, `JWT_AUDIENCE`, and a mapping claim.

## 6. One thing to be careful about

The CLI login (`/sso/cli/poll`) and the MCP connector both rely on SSO or password exchange for **humans**. The connector specifically accepts only an ordinary user, not an admin, and mints its own scoped key. That is human-side. Do not try to route a server application's identity through the connector login path; it is not built for that.

## Source files

- `litellm/proxy/management_endpoints/ui_sso.py` `encode_ui_session_jwt`, `set_session_token_cookie`, `cli_poll_key`, provider setup
- `litellm/proxy/auth/user_api_key_auth.py` `is_jwt`, JWT key mapping lookup, auto-register
- `litellm/proxy/management_endpoints/jwt_key_mapping_endpoints.py` mapping CRUD
- `litellm/proxy/utils.py` `get_custom_url` / `PROXY_BASE_URL`
- `oicm-litellm-layer/deploy/base/gateway/config/litellm-config.yaml` `general_settings` (no sso, no jwt today)
- `oicm-litellm-layer/docs/admin-api/ATHENA-READ-ONLY-KEY-GUIDE.md` the SSO note
