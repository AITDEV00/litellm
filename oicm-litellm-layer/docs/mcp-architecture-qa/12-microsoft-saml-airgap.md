# Microsoft SSO on an airgapped gateway: what actually happens, verified

Verification of an airgap alternative, read against source on 2026-11-23. This is **not the current deployment recommendation**: dev and prod use Microsoft OIDC with browser SSO, and guide 16 records the successful login. OIDC needs pod egress. Preserve SAML under this design only if egress is intentionally unavailable later

## The short answer

**SAML with inline `SAML_IDP_METADATA_XML` genuinely removes the gateway's need to talk to Microsoft for login.** That specific claim is true and I confirmed it in code. The gateway validates a locally-signed assertion against a certificate embedded in the metadata you ship, so no token endpoint, no Graph, no JWKS fetch.

One correction up front: **native Microsoft OIDC needs the pod to reach Microsoft, and mirroring JWKS does not remove that.** The research is right about this, and I can show the exact lines.

## 1. Why native Microsoft OIDC cannot work airgapped

`litellm/proxy/management_endpoints/sso/litellm_http_client_sso.py`, `process_login`:

1. `await self.token_endpoint` then `client.post(token_url, ...)` with `httpx.BasicAuth(client_id, client_secret)`. That is the gateway exchanging the authorization code at `login.microsoftonline.com/{tenant}/oauth2/v2.0/token`.
2. Then `client.get(userinfo_url, ...)` where `custom_microsoft_sso.py` sets `userinfo_endpoint = https://graph.microsoft.com/{version}/me`.

So every login does **two outbound calls from the pod** to Microsoft, plus a discovery document for the generic provider. Mirroring the JWKS would only fix signature verification; the token exchange and the Graph `/me` call remain. If the pod cannot reach `login.microsoftonline.com` and `graph.microsoft.com`, OIDC login fails closed.

This is why SAML is the right choice for your airgap, and the research's conclusion holds.

## 2. Why SAML with inline metadata works airgapped

`litellm/proxy/management_endpoints/sso/saml_sso.py`, `_load_idp_settings`:

```python
if metadata_url is not None:
    parsed = await asyncio.to_thread(
        OneLogin_Saml2_IdPMetadataParser.parse_remote, metadata_url, ...)
else:
    parsed = OneLogin_Saml2_IdPMetadataParser.parse(metadata_xml)
```

Two distinct paths:

| Config | Behavior | Airgap safe |
|---|---|---|
| `SAML_IDP_METADATA_URL` | `parse_remote`, fetches the metadata over the network from the pod | **no**, needs egress to the IdP metadata URL |
| `SAML_IDP_METADATA_XML` | `parse`, pure local parse | **yes**, no network |

So the research is precise on the condition, and it matters: **use `SAML_IDP_METADATA_XML`, not `SAML_IDP_METADATA_URL`.** The latter would reintroduce exactly the egress you are trying to avoid.

Validation is then local, via python3-saml (`OneLogin_Saml2_Auth`), which checks the signature, audience, and time conditions against the certificate in that XML. No callback to Microsoft.

## 3. What the browser still needs

The user's browser must reach two things:

1. `login.microsoftonline.com` to authenticate with Entra.
2. Your public ingress to POST the assertion to `/sso/saml/callback`.

Entra does not call the gateway. The pod does not call Entra. The assertion travels browser-to-gateway. This part of the research is correct and is the essential property.

The callback URL is built from `_base_url` which uses `get_custom_url`, which prefers `PROXY_BASE_URL`. So set `PROXY_BASE_URL=https://litellm.ecouncil.ae` and the ACS URL is stable regardless of internal routing. Confirmed.

## 4. Corrections to the research

I checked each specific claim. Four need correcting.

### 4a. `SAML_ALLOW_UNSOLICITED` is real, but its default is already the safe value

The research says keep `SAML_ALLOW_UNSOLICITED=false`. True, and it is the default: `_bool_env("SAML_ALLOW_UNSOLICITED", False)`. Also, if someone flips it to `true`, the code refuses unless a shared Redis cache exists, because the replay guard must span workers. That is a good detail the research did not mention.

### 4b. `rejectUnsolicitedResponsesWithInResponseTo` is hardcoded, not configurable

The research implies you control this. You do not. In `_build_settings` it is a literal:

```python
"rejectUnsolicitedResponsesWithInResponseTo": False,
```

The real unsolicited gate is the separate `_enforce_response_binding` check with the `SAML_ALLOW_UNSOLICITED` env var. So the behavior is safe, but the mechanism is not the field the research named. Do not try to set the python3-saml field; set the env var.

### 4c. CLI login does support SAML now

The research says "SAML does not currently share the OIDC CLI callback selection" and "self-service connector login through CLI SSO needs a separate compatibility check". The first login route does branch to SAML: `ui_sso.py` around line 1174 checks `microsoft_client_id is not None or google_client_id is not None or generic_client_id is not None or SAMLAuthHandler.is_saml_configured()` and routes to `SAMLAuthHandler.build_login_redirect`. So the login redirect side is SAML-aware.

What I could not confirm is the CLI **completion** path (`/sso/cli/poll`, `/sso/cli/complete`) fully handling a SAML assertion end to end. My grep of the CLI region found no `saml` references. So the safe statement is narrower than the research's: **the browser-Microsoft-SSO Admin UI path is the verified one; treat the CLI connector login through SAML as unproven in this build.** That affects how your users run `adma account add`, which currently uses `/sso/cli/password` (password), not SSO. So practically, keep password login available for connector enrollment.

### 4d. The CLI login mechanism for connectors does not use SSO at all today

Worth stating plainly because it changes your rollout: `adma account add` calls `/sso/cli/password`, a username/password exchange, not SSO. So Microsoft SSO changes how humans log into the **Admin UI**; it does not change how the connector enrolls. That is a distinction the research did not draw.

## 5. Invariants you must preserve, stated from code

| Requirement | Where enforced | Why |
|---|---|---|
| Inline XML, not URL | `_load_idp_settings` | URL path fetches over network |
| `SAML_STRICT` true | `_build_settings`, default true | rejects malformed/unsigned |
| `SAML_WANT_ASSERTIONS_SIGNED` true | `_build_settings`, default true | the whole security property |
| `SAML_ALLOW_UNSOLICITED` false | `_enforce_response_binding`, default false | SP-initiated only, and requires Redis if true |
| Shared Redis present | `_enforce_response_binding` + replay guard | assertion replay detection across the 4 worker processes and 2 replicas |
| `PROXY_BASE_URL` set to the public host | `get_custom_url` | ACS and metadata URLs must be the externally visible ones |

That Redis point matters more than it looks. The replay guard uses `cache.async_increment_cache` on the assertion ID, with a TTL spanning the assertion validity window (3600s default, 86400 max). Your gateway runs `--num_workers 4` with 2 replicas, so without shared Redis the guard is per-process and an assertion could be replayed into a different worker. You already run Redis, so this is satisfied, but it is a real dependency rather than an optional one.

## 6. The certificate rollover caveat, made concrete

The research mentions Microsoft's signing-certificate rollover. Confirmed and it is the one ongoing airgap chore:

- `_SAML_IDP_METADATA_TTL_SECONDS = 3600`, so the parsed settings are cached an hour; after that the inline XML is re-parsed. Since it is inline, there is no fetch to fail.
- But the certificate inside your inline XML is whatever you shipped. Entra rotates signing certificates on its own schedule and publishes the new one in the federation metadata. When the old certificate expires, assertions signed with the new cert will fail `wantAssertionsSigned` validation and logins break until you update the Secret.
- So the operational requirement is: re-download the Entra federation metadata on a schedule (before the announced rollover date), update the Kubernetes Secret holding `SAML_IDP_METADATA_XML`, and roll the gateway. Since the value is in a Secret, this is a config change and a pod restart, not an image change.
- Entra publishes rollover notifications and can use overlapping certificates. Configure Entra to keep the old signing cert valid through your update window so there is no dead zone.

## 7. How this interacts with the rest of what you set up

- **Model and MCP traffic are unaffected.** SAML is login only, on `/sso/*` routes. Tool dispatch, the tunnel, and provider calls do not touch it.
- **The gateway still needs Postgres and Redis in-cluster**: session storage, the replay guard, user upsert. Already true.
- **Outbound for other features stays as it is.** Provider calls, the RustFS install mirror, and the MCP tunnel are separate paths with their own requirements. SAML neither adds nor removes them.
- **Users mint keys the same way after login.** The SAML session feeds the same post-login path, so `POST /key/generate` from the UI session works exactly as in `11-sso-temporary-key-pattern.md`, with the same delegation ceiling.

## 8. The rollout sequence I would actually use

1. Create the Entra **enterprise application** (not app registration), single tenant, ACS `https://litellm.ecouncil.ae/sso/saml/callback`, Entity ID `https://litellm.ecouncil.ae/sso/saml/metadata`, sign-on URL `/sso/saml/login`.
2. Emit `email`, and `role` with values exactly `proxy_admin`, `internal_user`, `internal_user_viewer`. Keep `groups` separate, do not put group names in `role`.
3. Download the federation metadata XML, store it in a Secret, mount as the `SAML_IDP_METADATA_XML` env var.
4. Set `PROXY_BASE_URL`, `SAML_SP_ENTITY_ID`, and nothing else that is not a default.
5. Do not set any `MICROSOFT_*` variable; those select the OIDC implementation and would reintroduce the egress.
6. Test on dev through a real HTTPS host Entra can POST to. A port-forward cannot be an ACS.
7. Keep password login enabled until the SAML browser flow is proven, then decide whether to disable it. Keep the master key for recovery.
8. Keep password login for connector enrollment regardless, since `adma` uses `/sso/cli/password`.

## 9. What will not work, so nobody wastes a day on it

- `SAML_IDP_METADATA_URL` with no egress to the IdP: the metadata fetch fails.
- Microsoft OIDC with only the pod blocked: every login fails at the token exchange.
- Mirroring JWKS to fix OIDC: fixes signature only, not token exchange or Graph `/me`.
- Disabling the shared Redis cache: SAML still works for a single worker but the replay guard and the two-replica login state stop being sound.
- Assuming SSO changes connector enrollment: it does not, `/sso/cli/password` is separate.

## Source files

- `litellm/proxy/management_endpoints/sso/saml_sso.py` `_load_idp_settings`, `_build_settings`, `_enforce_response_binding`, replay guard, `is_saml_configured`, `_extract_user_attributes`
- `litellm/proxy/management_endpoints/sso/litellm_http_client_sso.py` `process_login` token and userinfo calls
- `litellm/proxy/management_endpoints/sso/custom_microsoft_sso.py` Graph `/me` userinfo endpoint
- `litellm/proxy/management_endpoints/ui_sso.py` login branch, `add_missing_team_member`, `get_litellm_user_role`
- `litellm/proxy/management_endpoints/types.py` `get_litellm_user_role`, `CustomOpenID`
- `litellm/proxy/utils.py` `get_custom_url`
- `litellm/proxy/management_endpoints/sso_helper_utils.py` session targets
