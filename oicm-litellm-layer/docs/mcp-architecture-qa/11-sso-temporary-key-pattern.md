# Pattern: app uses SSO, each user mints a temporary key, that key calls the gateway

Short answer: **this is a first-class supported pattern, not a hack, and the code has explicit machinery for it.** It is a good design for interactive apps that act per-user. It is the wrong design for unattended server-to-server traffic, and there are three things you must get right or the security property collapses.

Verified against the source in this tree, not the docs.

---

## 1. It is supported: the UI/CLI session token is a delegating credential

The flow you describe already exists natively, because LiteLLM's own Admin UI and `lite login` CLI do exactly this:

1. A user authenticates by SSO (or password).
2. The gateway issues a **session token**, a signed/encrypted blob, not an `sk-` key (`auth_checks.py`, `LITELLM_SESSION_TOKEN_PREFIX = "litellm_login_"`).
3. That session token is presented as a bearer.
4. The user calls `/key/generate` to mint a real virtual key.
5. The minted `sk-` key is what calls `/chat/completions` and the MCP routes.

So `POST /key/generate` with a session token **is** "the user creates their own API key". The code calls this a **UI session personal key**.

Evidence:

- `user_api_key_auth.py`: a non-`sk-` token is decrypted with `ExperimentalUIJWTToken.get_key_object_from_ui_hash_key`, and marked `is_session_token`.
- `key_management_endpoints.py`, `generate_key_fn`: `is_ui_session_token = user_api_key_dict.team_id == UI_SESSION_TOKEN_TEAM_ID`, and a whole branch keys off it.
- `route_checks.py`: `internal_user_routes` includes `key_management_routes`, which includes `KEY_GENERATE`, `KEY_UPDATE`, `KEY_DELETE`, `KEY_REGENERATE`. So an `internal_user` can mint, rotate, and delete its own keys. It cannot mint keys for another user: on generation, non-admins get `user_id` auto-assigned to themselves.

## 2. The delegation ceiling: the part that makes it safe

The critical property is that a minted key cannot exceed the person's own authority. There is a named guard for it.

From `key_management_endpoints.py`:

> Delegated-authority ceiling (GHSA-q775-qw9r-2r4g): a non-admin caller cannot grant a key a higher budget than their own authority. UI session personal keys are capped by `user_max_budget` when it is available.

The ceiling is computed as, in order:

1. `user_max_budget` when this is a UI session token,
2. else the session token's own `max_budget`,
3. else the team's `max_budget`.

And then:

- A requested `max_budget` above the ceiling is a `400 cannot exceed the caller's own max_budget`.
- The same check applies to every `budget_limits[*]` window.
- A session token cannot set `max_budget` at all without specifying `team_id`, unless the team table supplies one.

So the security model is: **the user can delegate to a key, but only downward.** They cannot self-escalate budget, and they cannot mint a key for someone else.

## 3. What "temporary" should mean, and how to make it so

"Temporary key" is the right instinct, and it is enforceable per key:

| Knob | Effect |
|---|---|
| `duration: "1h"` / `"30m"` | the key expires; `expires` column set |
| `max_budget` | hard spend stop for that key |
| `budget_duration` | key budget resets on an interval |
| `models` | key-level model ceiling, intersected with the user's and team's |
| `key_type: "llm_api"` / `"read_only"` | route preset, so the key only reaches what the app needs |
| `object_permission.mcp_access_groups` | key-level MCP narrowing, intersected with team |
| `throttle_on_budget_exceeded` | degrade rate rather than hard fail |
| `auto_rotate` + `rotation_interval` | periodic rotation |

For an app that sets up a user session and then makes calls, `duration` matched to the session lifetime plus a key-level `max_budget` is the canonical shape.

## 4. The three things that make or break it

### 4a. The session token must never be the long-lived app credential

If your application layer holds a session token in a server variable and reuses it forever, you have defeated the point. A **UI session token is bound to one user**. Reusing one token across many users means every request is attributed to that one user and every budget check runs against that one person. The token must be created per user, per login, and short-lived.

This is the single most common mistake with this pattern.

### 4b. The minted key must not outlive the session

A `sk-` key with no `duration` is permanent. If the app mints a key per session and never revokes it, you accumulate unbounded keys and stale access. Either set `duration` to the session length, or have the app call `/key/delete` on logout, or use the session token directly if the app is only proxying UI actions.

### 4c. MCP access is not inherited by default

A key minted by a user in a team inherits the team's `mcp_access_groups` at call time, but only because the team has them. Today no team has any MCP grants, so a minted key would resolve zero MCP servers exactly like the audit showed for your personal key. Minting keys does not solve MCP access; granting the team does.

Also watch the key-level intersection rule from `user_api_key_auth_mcp.py`: if both the key and the team declare access groups, the effective set is their intersection. If the key declares none and the team declares some, the key inherits the team's. So the clean setup is to grant on the team and leave the key's MCP fields empty unless you want to narrow further.

## 5. Which token should the app use, precisely

You have two variants, and they differ in where the SSO happens:

### Variant A: the app already has the user's Microsoft/Entra token

- Turn on `enable_jwt_auth` with the Entra JWKS, audience, and issuer.
- Register a JWT key mapping, or enable auto-register, so the Entra `sub`/`oid` resolves to a key.
- The app forwards the user's Entra token as the bearer.
- The gateway validates it and runs the request as the mapped identity.

This is the most seamless: no session token to manage, revocation at the IdP, and per-user identity in the spend log. It is the variant I would pick if the app already authenticates users against Entra.

### Variant B: the app uses LiteLLM's own SSO session token

- The app redirects the user through `/sso/key/generate`, receives the session token, and uses it to call `/key/generate`.
- Then it uses the resulting `sk-` key for the actual calls.

This works but requires the app to hold and relay a LiteLLM session token, which is more moving parts than Variant A. It is what the built-in UI does; you would be reimplementing it.

### Variant C: the app mints keys server-side with a scoped service key

- The app holds one team-owned `sk-` key with `allowed_routes` including `/key/generate`.
- On behalf of a user, it calls `/key/generate` with `user_id` set to that user.
- Non-admin callers **cannot** set `user_id` to someone else; this is blocked. So the app key would need to be an admin-owned key, which is a real escalation and not recommended.

Variant C is the one to avoid: it requires an over-privileged app credential. Use A or B.

## 6. Is this best practice?

For an **interactive, per-user application**, yes, with caveats:

- Good: per-user attribution, per-user budget enforced by the delegation ceiling, per-user revocation, and the key carries only the user's own ceiling.
- Good: it is the pattern the product itself uses, so it is maintained and tested.
- Risk: token handling. The session token is a bearer for that user; leaking it is leaking that user's ability to mint keys until it expires.

For an **unattended backend or batch job**, no. There is no "user" to mint for. Use one team-owned key with a narrow `key_type`, and pass `end_user` for attribution.

So the honest recommendation: use it for the interactive path, use a team key for the unattended path, and never mix them by putting a session token in a long-running server.

## 7. Concrete plan for your setup

1. Enable `enable_jwt_auth` (Variant A) or `ui_sso` (Variant B) with Microsoft. You need SSO on for either.
2. For Variant A, register the Entra issuer, audience, JWKS URL, and a claim-to-key mapping or auto-register.
3. In the app, obtain the user's Entra token per request or per session, and forward it as the bearer.
4. If you prefer minted keys for shorter blast radius, have the app call `/key/generate` with `duration` equal to the session length, `key_type: "llm_api"`, and optionally a `max_budget` under the user ceiling.
5. Grant MCP access on the **team**, not the key, so the minted key inherits it.
6. Set a `user_max_budget` on each user, so the delegation ceiling is meaningful instead of `None` (with `None`, the ceiling falls through to the team's budget, or to no ceiling if the team has none either).

Point 6 is the one most likely to be missed: the delegation ceiling is only a real ceiling if `user_max_budget` or the team's `max_budget` is set. If both are `None`, a user can mint a key with any budget they like.

## Source files

- `litellm/proxy/auth/auth_checks.py` `LITELLM_SESSION_TOKEN_PREFIX`, encrypt/decrypt
- `litellm/proxy/auth/user_api_key_auth.py` UI hash key decryption, `is_session_token`, `user_max_budget`
- `litellm/proxy/management_endpoints/key_management_endpoints.py` `generate_key_fn`, the delegated-authority ceiling block, `is_ui_session_token`
- `litellm/proxy/auth/route_checks.py` `internal_user_routes`, `key_management_routes`
- `litellm/proxy/_experimental/mcp_server/auth/user_api_key_auth_mcp.py` key/team MCP group intersection
- `litellm/proxy/_experimental/mcp_server/ui_session_utils.py` session-token MCP handling
- `litellm/constants.py` `UI_SESSION_TOKEN_TEAM_ID`
