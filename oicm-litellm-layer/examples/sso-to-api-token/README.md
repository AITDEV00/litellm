# Using Microsoft SSO to obtain LiteLLM API credentials

Two supported flows. They answer different questions, and picking the wrong one
is the usual reason people get stuck.

| | Flow A: CLI SSO | Flow B: JWT bearer |
|---|---|---|
| Question | "How does a person turn their login into a token?" | "My service already holds the user's Microsoft token. Can LiteLLM trust it?" |
| Human browser step | Required, every login | None |
| Works unattended | No | Yes |
| Enterprise license | Not needed | Required |
| Credential your app stores | The returned JWT | Nothing; it forwards the user's token |
| Config | none extra | `enable_jwt_auth` + `JWT_PUBLIC_KEY_URL` / `JWT_AUDIENCE` / `JWT_ISSUER` |

Both were run for real against `litellm-proxy-dev` on 2026-11-24. The evidence is
quoted in each section.

---

## Flow A: CLI SSO to a data-plane token

The protocol is what the `litellm` CLI speaks. `flow_a_cli_sso_virtual_key.py`
reproduces it so a service can drive it in-process.

### Steps

1. `POST /sso/cli/start` returns `login_id`, `poll_secret`, `user_code`.
2. A human opens the verification URL and completes the Microsoft login. This
   step is the point of the flow and cannot be automated.
3. `GET /sso/cli/poll/{login_id}` with header `x-litellm-cli-poll-secret`:
   - `{"status": "pending"}` until the browser step finishes
   - `{"status": "ready", "key": "<jwt>", "user_id": ..., "team_id": ...}`
   - `{"status": "ready", "requires_team_selection": true, "teams": [...]}` for a
     multi-team user. Poll again with `?team_id=<id>`.
4. The returned `key` is a **JWT**, not an `sk-` key. It expires. Use it as
   `Authorization: Bearer`.

### Real run

```
$ python3 flow_a_cli_sso_virtual_key.py --base-url http://localhost:14001 --wait-seconds 12
Step 1: POST /sso/cli/start
  login_id  : cli-8VSsLEQxU6nBhGaQ9N4dD4zGm83Jd_DR
  user_code : 5XPZ-T3TR

Step 2 (HUMAN): open this URL and sign in with Microsoft
  http://localhost:14001/sso/key/generate

Step 3: polling /sso/cli/poll/cli-... for 12s
  no token yet. If nobody opened the URL, that is expected.
```

That is the proof for steps 1 and 3. Completing it needs a person at the URL,
which is the designed behavior, not a limitation of the script.

### Gotcha: the verification URL is generic by default

`verification_uri_complete` (the URL that pre-fills the code) is gated behind
`general_settings.allow_cli_sso_verification_uri_complete` and defaults to false.
Without it the user must visit `/sso/key/generate` and enter the `user_code` by
hand. Set the flag if you want the one-click URL.

---

## Flow B: forward the user's Microsoft token (recommended for services)

This is the answer to "my backend already has the user's Microsoft token, and I
want that same credential to work on LiteLLM".

### How the proxy validates it

From `litellm/proxy/auth/user_api_key_auth.py`:

- With `general_settings.enable_jwt_auth: true`, a bearer that parses as a JWT is
  validated against `JWT_PUBLIC_KEY_URL` (the Entra JWKS), then `aud` against
  `JWT_AUDIENCE` and `iss` against `JWT_ISSUER`.
- If `premium_user` is not True the request is rejected:
  `"JWT Auth is an enterprise only feature."`
- Claims map to identity through `general_settings.litellm_jwtauth`
  (`user_id_jwt_field`, `user_email_jwt_field`, `team_id_jwt_field`, ...).

### Config used on dev

`deploy/overlays/dev/gateway/config.yaml`:

```yaml
general_settings:
  enable_jwt_auth: true
  litellm_jwtauth:
    user_id_jwt_field: "oid"
    user_email_jwt_field: "upn"
    team_id_jwt_field: null
    user_id_upsert: true
```

`deploy/overlays/dev/gateway/deployment.yaml`:

```yaml
- name: JWT_PUBLIC_KEY_URL
  value: "https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys"
- name: JWT_AUDIENCE
  value: "<app id>"
- name: JWT_ISSUER
  value: "https://sts.windows.net/<tenant>/"
```

### Real run

Get a real Entra token, inspect it, present it:

```
$ python3 flow_b_microsoft_jwt_to_litellm.py inspect --token "$TOK"
  aud: e1ad1372-fbaa-4013-a288-8547d8b7dbe7
  iss: https://sts.windows.net/99cf3350-40c8-4260-bb84-1dada4a029c0/
  oid: 09fa492f-a324-42ea-bea7-54792252cafc
  exp: ... (in 60 min)

  NOTE: no user identity claim (upn/email/preferred_username).
  This is an app-only token. It identifies the service principal,
  not a person. Use a delegated token for per-user identity.

$ python3 flow_b_microsoft_jwt_to_litellm.py call --base-url http://localhost:14001 --token "$TOK"
HTTP 200
{"data":[{"id":"Qwen3.6-35B-A3B-FP8",...},{"id":"nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B",...}]}
```

**LiteLLM accepted a real Microsoft token as the bearer and served the data
plane.** No virtual key, no second login, no shared secret.

### Negative cases, all verified

| Case | Result |
|---|---|
| Tampered token (last char changed) | `401` signature verification failed |
| Token issued for a different audience (Graph) | `401` |
| Admin route `/user/list` with a user JWT | `403` |
| `enable_jwt_auth` off, JWT presented | `401` with the "Set `enable_jwt_auth: true`" hint |

### The audience trap, which will bite you

An Entra token's `aud` is whatever API it was requested for. The staging app has
**no exposed API scope**, so this fails:

```
scope=api://<app-id>/.default
-> AADSTS500011: The resource principal named api://... was not found in the tenant
```

What does work with that app is `scope=<app-id>/.default`, which yields
`aud: <app-id>`. For a real service you have two choices:

1. Expose an API scope on the LiteLLM app registration and request that scope.
   Put the app id in `JWT_AUDIENCE`. This is the clean path.
2. Request `scope=<app-id>/.default` and set `JWT_AUDIENCE` to that app id.

### The issuer trap

Entra emits two issuer forms and PyJWT matches `iss` exactly:

- app-only tokens: `https://sts.windows.net/<tid>/`
- delegated v2 tokens: `https://login.microsoftonline.com/<tid>/v2.0`

The discovery document advertises only the v2 form. If you copy the issuer from
discovery but your callers present v1 tokens, every request fails. Decode a real
token and copy its `iss` literally.

### App-only tokens identify the app, not a person

A `client_credentials` token carries only `oid`/`sub` of the service principal and
no `upn`, `email`, or `name`. It proves the pipeline and is fine for a service
identity, but it cannot represent an end user. For per-user identity and spend
attribution you need a **delegated** token, which requires the user to consent
once. `flow_b_microsoft_jwt_to_litellm.py device-code` gets one.

### What a JWT-authenticated caller can do

`/key/generate` succeeds for any authenticated non-admin caller, and that is
intended: `key_management_endpoints.py:2301` auto-assigns
`user_id=<the caller's own id>` so a user can mint keys bound to themselves.
It is not an escalation. The minted key is scoped to that user and still gets
`403` on `/user/list`.

---

## Choosing

Use **Flow B** when your service holds the user's Microsoft token. It is the only
flow that needs no human at request time and no stored credential.

Use **Flow A** when you need a one-time account link, or when the deployment is
not enterprise-licensed.

Do not try to scrape a key out of the Admin UI session. The UI cookie is a
`litellm_login_` session token and is refused on `/key/generate` by design.

## Exercising the exemplars

```
export LITELLM_BASE=http://localhost:14001

# Flow A: prints a URL, then polls. Complete the URL in a browser to get a token.
python3 flow_a_cli_sso_virtual_key.py --base-url "$LITELLM_BASE" --wait-seconds 300

# Flow B: any Entra token
python3 flow_b_microsoft_jwt_to_litellm.py inspect --token "$TOK"
python3 flow_b_microsoft_jwt_to_litellm.py call --base-url "$LITELLM_BASE" --token "$TOK"

# Flow B: get a delegated user token by device code
python3 flow_b_microsoft_jwt_to_litellm.py device-code --tenant "$TENANT" --client-id "$CLIENT_ID" --client-secret "$SECRET"
```

Flow A notes: `--team-id` skips the interactive team prompt. The flow is
single-use; the server deletes the session when it returns the token.

## Not yet done

Flow B is enabled on **dev only**. Prod still runs browser SSO plus password.
Before enabling it on prod, decide the prod app registration's audience (see the
audience trap) and add `JWT_*` to the prod overlay. The parity test lists these
keys as dev-only on purpose so the difference cannot drift silently.
