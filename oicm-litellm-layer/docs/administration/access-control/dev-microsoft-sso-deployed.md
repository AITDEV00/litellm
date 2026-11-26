# Dev Microsoft SSO: what was deployed and what is still blocked

Deployment snapshot from 2026-11-23 using native Microsoft OIDC, not SAML. Gateway pods have Microsoft egress, so OIDC avoids the separate SAML metadata lifecycle. This deployment is now proven to work at the localhost dev callback; [guide 16](../../mcp/opencode-pilot-evidence.md) records the human complete Microsoft login with MFA, and its browser-to-localhost-for-dev path supersedes the suspected localhost blocker below

Keep this note for its original env/discovery evidence only. Do not repeat its recommendation to move dev testing to prod. Dev gateway evidence must use `http://localhost:14001` after a new `kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000`. Actual dev SSO takeover by a different active browser session is an identity-attribution hazard to avoid with clean profiles

## What is deployed

Files added or changed, all under `oicm-litellm-layer/deploy/overlays/dev/gateway/`:

| File | Change |
|---|---|
| `litellm-microsoft-sso-dev.yaml` | real Entra secret, **gitignored** |
| `litellm-microsoft-sso-dev.yaml.example` | committed template with placeholders |
| `.gitignore` | excludes the real secret file |
| `deployment.yaml` | adds `PROXY_BASE_URL` and the three `MICROSOFT_*` env vars from the Secret |

Env on the dev Deployment:

```yaml
- name: PROXY_BASE_URL
  value: "http://localhost:14001"
- name: MICROSOFT_CLIENT_ID / MICROSOFT_CLIENT_SECRET / MICROSOFT_TENANT
  from secret litellm-microsoft-sso-dev, optional: true
```

`optional: true` means the pod still starts if the Secret is absent, which keeps dev usable without SSO.

## Verification actually run

| Check | Result |
|---|---|
| dev pod can reach login.microsoftonline.com over HTTPS | 200 |
| dev pod can reach tenant JWKS | 200 |
| dev pod can reach graph.microsoft.com | 401 without token, expected |
| `premium_user` on dev | True, so the 5-user SSO gate does not block |
| dev `litellm_usertable` rows | 6, above the free SSO limit, but license present so fine |
| Secret applied to cluster | yes, 3 keys |
| rollout | `litellm-proxy-dev-6c75fc56dd-rfqc4` Running |
| env in pod | all four present |
| `GET /sso/key/generate` | **303 redirect to Microsoft** |

The redirect it actually produced:

```
https://login.microsoftonline.com/99cf3350-.../oauth2/v2.0/authorize
  ?response_type=code
  &client_id=a85030da-...
  &redirect_uri=http%3A%2F%2Flocalhost%3A14001%2Fsso%2Fcallback
  &scope=openid+User.Read+email
```

So the redirect URI LiteLLM will send is exactly:

```
http://localhost:14001/sso/callback
```

## What is still blocked, and why

**Historical hypothesis, now closed. The dev Entra application accepted `http://localhost:14001/sso/callback` and the prod-correct redirect completed a real browser login in guide 16.** The analysis below is not deployment status or a requirement to expose dev

Small independently proven client-security concern: prior dev client-secret material was shown during the work. Rotation is still an open cleanup item. Rotate it in Entra and the dev Kubernetes secret, keep it out of docs/history going forward, and persist the secret reference where local authorized operations can use it. No secret value belongs in this note. The app `a85030da-...` has a client secret, and LiteLLM sends it with `httpx.BasicAuth` (`litellm_http_client_sso.py:146`), which makes it a confidential client. Microsoft only permits `http://localhost` redirect URIs for **public** clients (no secret, PKCE). So even after your ticket adds the callback, Microsoft may reject the login at the authorize step with `AADSTS50011` or refuse to save a localhost URI on a web platform registration.

Three ways out, in order of least work:

1. **Register `https://litellm.ecouncil.ae/sso/callback` on the app and test on prod instead.** Prod already has that host with valid TLS, the pods reach Microsoft, and the prod overlay can set `PROXY_BASE_URL` the same way. This is the path I would take to prove it end to end.
2. **Create a separate app registration for dev that is a public client** and supports localhost. But this code path always sends a client secret, so a public client alone will not work either without code change.
3. **Expose dev behind a real HTTPS host** and register that host's `/sso/callback`, which matches what a confidential client allows.

So the honest status: the deployment is ready, the redirect is correct, and the only remaining gate is the Entra redirect URI matching a scheme Microsoft accepts for a confidential client. Localhost will very likely not be accepted.

## When the ticket lands

If the ticket registered `http://localhost:14001/sso/callback`, try it, and if Microsoft answers `AADSTS50011` that confirms the public-client restriction and option 1 is the fix. If the ticket registered an https host, change `PROXY_BASE_URL` in `deployment.yaml` to that host, re-apply `kubectl apply -k deploy/overlays/dev`, and expose dev there.

## Test command once registered

```sh
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000
# open in a browser:
open http://localhost:14001/sso/key/generate
```

The browser must be able to reach `login.microsoftonline.com` and `http://localhost:14001`.

## Files

- `oicm-litellm-layer/deploy/overlays/dev/gateway/deployment.yaml`
- `oicm-litellm-layer/deploy/overlays/dev/gateway/litellm-microsoft-sso-dev.yaml` (gitignored)
- `oicm-litellm-layer/deploy/overlays/dev/gateway/litellm-microsoft-sso-dev.yaml.example`
- `oicm-litellm-layer/tests/deploy/test_dev_prod_parity.py` (`_DEV_ONLY_ENV_VARS` extended)
- `litellm/proxy/management_endpoints/sso/litellm_http_client_sso.py` (confidential client evidence)
- `litellm/proxy/utils.py` `get_proxy_base_url` / `get_custom_url`
