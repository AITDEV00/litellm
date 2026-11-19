# Multi-account connector: prod acceptance procedure

This is the hands-on procedure for proving the multi-account `adma` client
against the **prod** gateway at `https://litellm.ecouncil.ae`. It follows the same
phases as `MULTI-ACCOUNT-TEST-PROCEDURE.md` (the dev procedure), but prod differs
from dev in ways that break a straight copy, so read the differences below before
starting.

It is a guided session: the assistant provisions state and hands you the exact
commands, you run the interactive ones, and each phase is verified before the
next. Run it top to bottom.

Run on 2026-11-19 against prod. All four sources served through two ordinary
accounts and both an OpenAPI-derived and a native MCP tool returned real origin
data through the public hostname.

## How prod differs from dev, and the two defects that cost a day

Read this first. Each item below produced a failure whose message pointed
somewhere other than the cause.

1. **The control plane is the public host; the data plane is not.** Prod has an
   ingress for `litellm.ecouncil.ae` that routes everything to the gateway on
   port 4000. The gRPC tunnel is a *separate* listener, the broker's
   `link-tunnel` on 8093, and **no ingress route reaches it**. The agent derives
   its tunnel target from the account's gateway URL, so left alone it dials
   `litellm.ecouncil.ae:443` (the gateway) and the tunnel never connects. You must
   set `MCP_SOURCE_LINK_TUNNEL_URL` to a route that reaches 8093 (Phase 1). This
   is the single most common prod failure and it presents as a tunnel that closes
   immediately with no server-side log.

2. **The broker and the gateway must read the same database.** They are separate
   processes with separate env. The gateway writes sources and workers through
   `litellm-db-credentials` (database `litellm`). The broker read
   `adeo-litellm-postgres-app`, which is database `oicm` with user `oicm`, so the
   workers the gateway enrolled were invisible to the broker. The broker's peer
   set was empty and it rejected every tunnel with `connector unauthorized`,
   which reads like a bad credential. Fixed 2026-11-19 by giving the broker the
   gateway's `DATABASE_URL` (the broker prefers `DATABASE_URL` over its
   individual `DATABASE_*` vars). If you see `connector unauthorized` on prod,
   check this pairing *before* you touch any secret: see Phase 1 verification.

3. **OpenAPI sources need `user_url_allowed_hosts`.** Each OpenAPI source records
   a `spec_path` of `http://127.0.0.1:4000/install/mcp-source-broker/<id>/openapi.json`
   and the gateway fetches that to build tools. The SSRF guard blocks loopback, so
   an OpenAPI source is silently dropped from the registry during reload while an
   MCP source (no spec) loads fine. The gateway log says
   `URL targets a blocked address (127.0.0.1)`. Fixed 2026-11-19 by adding
   `127.0.0.1:4000` to `user_url_allowed_hosts` in the base gateway config, which
   dev already had.

4. **The prod store is two databases.** `adeo-litellm-postgres-1` hosts both
   `oicm` (the broker's old target, and several other tenants) and `litellm` (the
   gateway). When a query returns zero rows, confirm which database you are in
   before concluding the rows are missing. The `OicmMcp*` tables exist in both;
   the live ones are in `litellm`.

5. **Accounts.** Prod has no throwaway `accept-owner` users and no self-service
   signup. You create them as an administrator and set their password, because a
   user created via `/user/new` starts with no password (Phase 0). A fresh
   password may be rejected as breached or arrive with
   `password_reset_required`; the connector login refuses such an account, and
   the fix is Phase 0's flag clear.

6. **The UI summaries and the source status are not trustworthy indicators.**
   `Submitted MCPs` shows `Transport: http` for every source including OpenAPI
   ones, because it prints the raw registry column instead of deriving the kind
   (the rest of the UI derives it correctly from `spec_path`). Read the kind from
   `OicmMcpSource.kind`, not the UI label.

## Phase 0: provision the two accounts (assistant)

Two ordinary `internal_user` accounts, one per person, so a later phase can prove
that an equal target under two owners stays two sources. Prod's user creation
rejects a `password` field, so this is create-then-set.

```bash
K="$(kubectl -n adeo-litellm get secret litellm-master-key -o jsonpath='{.data.master-key}' | base64 -d)"

for n in 1 2; do
  uid="accept-owner-$n"; em="accept-owner-$n@example.invalid"
  pw="Zq7#$(python3 -c 'import secrets;print(secrets.token_urlsafe(20))')9Xt"
  printf '%s' "$pw" > "/tmp/accept-prod$n.pw"; chmod 600 "/tmp/accept-prod$n.pw"

  curl -s -X POST https://litellm.ecouncil.ae/user/new \
    -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
    -d "{\"user_id\":\"$uid\",\"user_email\":\"$em\",\"user_role\":\"internal_user\"}"

  curl -s -X POST https://litellm.ecouncil.ae/user/update \
    -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
    -d "{\"user_id\":\"$uid\",\"password\":\"$pw\"}"
done
```

A user created this way can log in with a password but still carries
`password_reset_required`, and `/sso/cli/password` refuses it with
`password reset required before this account can be used for a connector`. The
flag is a login-time HIBP stamp and a clean password does not clear it, so clear
it explicitly:

```bash
PW="$(kubectl -n adeo-litellm get secret litellm-db-credentials -o jsonpath='{.data.DATABASE_URL}' | base64 -d | sed -E 's#.*://litellm:([^@]+)@.*#\1#')"
psql() { kubectl -n adeo-litellm exec -i adeo-litellm-postgres-1 -c postgres -- \
  env PGPASSWORD="$PW" psql -h 127.0.0.1 -U litellm -d litellm -tAc "$1"; }

for n in 1 2; do
  psql "UPDATE \"LiteLLM_UserTable\" SET password_reset_required=false, last_breach_check_at=NULL WHERE user_id='accept-owner-$n'"
done
```

**Success**: both rows exist and both can exchange their password for a key.

```bash
psql "SELECT user_id||' reset='||coalesce(password_reset_required::text,'NULL') FROM \"LiteLLM_UserTable\" WHERE user_id LIKE 'accept-%' ORDER BY user_id"
# accept-owner-1 reset=false
# accept-owner-2 reset=false

for n in 1 2; do
  curl -s -o /dev/null -w "accept-owner-$n login=%{http_code}\n" -X POST \
    https://litellm.ecouncil.ae/sso/cli/password -H 'Content-Type: application/json' \
    -d "{\"username\":\"accept-owner-$n@example.invalid\",\"password\":\"$(cat /tmp/accept-prod$n.pw)\"}"
done
# both 200
```

> Clear the flag rather than retrying the login. Each failed attempt re-runs the
> breach screen and re-stamps it, so retrying never converges.

**Hand-off**: the two credential pairs are
`accept-owner-{1,2}@example.invalid` and `/tmp/accept-prod{1,2}.pw`. Do not
continue until both logins return 200.

## Phase 1: install the connector and reach the tunnel

Install from the public hostname. The installer is checksum-verified against the
published release and prints the interactive commands to run yourself:

```bash
curl -fsSL https://litellm.ecouncil.ae/install/source-agent | sh
```

**Success**: `Source connector installed`, the binary at `~/.local/bin/adma`, and
`adma --help` listing `account`, `source`, and the agent commands. Confirm the
binary is the published release (the installer prints version and sha):

```bash
sha256sum ~/.local/bin/adma      # must equal the release's sha256
```

Now stand up the tunnel route. prod has no ingress for 8093, so forward the
broker's `link-tunnel` port and terminate TLS in front of it. The agent requires
HTTP/2 with ALPN, and a plain TLS terminator (like a dev `14443`-style proxy)
does **not** negotiate it, so the terminator must offer `h2`.

```bash
kubectl -n adeo-litellm port-forward svc/litellm-mcp-broker 18093:8093 &
# then run an h2 TLS terminator on 18094 forwarding to 127.0.0.1:18093, and:
export MCP_SOURCE_LINK_TUNNEL_URL=https://localhost:18094
```

Sanity-check that the forward speaks plain h2c gRPC before involving the agent:

```bash
curl -s -o /dev/null -w '%{http_code} %{http_version}\n' --http2-prior-knowledge \
  -X POST http://127.0.0.1:18093/grpctunnel.v1.TunnelService/OpenReverseTunnel \
  -H 'content-type: application/grpc' -H 'te: trailers' --data-binary 'x'
# 200 2
```

Before running the agent, verify the broker actually shares the gateway's
database, so a later `connector unauthorized` is not a mystery:

```bash
ST="$(kubectl -n adeo-litellm get secret litellm-mcp-source-broker -o jsonpath='{.data.service-token}' | base64 -d)"
kubectl -n adeo-litellm port-forward svc/litellm-mcp-broker 18091:9091 &
curl -s -H "Authorization: Bearer $ST" http://127.0.0.1:18091/v1/source-link/peers
```

**Success after Phase 4**: `peers` lists one record per registered source. If it
is empty while the database has workers, the broker is on the wrong database
(difference 2 above), and no amount of secret-checking will fix it.

**Caveats**:

- A broker rollout replaces the pod and kills the 18093 forward. Re-run it and
  restart the terminator; the agent otherwise logs `dial localhost:18094:
  connection not ready` forever.
- If your terminator is running against a stale forward, the agent's error reads
  `connection not ready`, which looks like a TLS or certificate problem and is
  not. Re-check the forward first.

## Phase 2: log in (you)

`account add` is interactive and refuses piped stdin. Answer three prompts per
account. Point the gateway URL at the **public host**, and do not set
`MCP_SOURCE_LINK_INSECURE`: prod has a valid certificate.

```bash
adma account add alpha
```

| Prompt | Value |
| ------ | ----- |
| Gateway URL | `https://litellm.ecouncil.ae` |
| Username | `accept-owner-1@example.invalid` (**email**, not `user_id`) |
| Password | `/tmp/accept-prod1.pw` |

Then `adma account add beta` with `accept-owner-2@example.invalid` and
`/tmp/accept-prod2.pw`.

**Success**: `account list` shows both, `*` on the first, no key material.

```bash
adma account list
# * alpha   https://litellm.ecouncil.ae   accept-owner-1@example.invalid
#   beta    https://litellm.ecouncil.ae   accept-owner-2@example.invalid
```

**Assistant verification** that no password hit disk and keys are owner-only:

```bash
grep -r "$(cat /tmp/accept-prod1.pw)" ~/.config/source-agent && echo LEAK || echo clean
ls -l ~/.config/source-agent/accounts/    # -rw------- on each .key
```

## Phase 3: the origins

The origins are ordinary local services; nothing about them is prod-specific.
The run used four, on fixed ports, two REST and two MCP:

| Origin | Port | Kind | Tools / endpoints |
| ------ | ---- | ---- | ----------------- |
| inventory REST | 18201 | openapi | `/inventory`, `/inventory/{sku}` |
| orders REST | 18202 | openapi | `/orders`, `/orders/{order_id}` |
| inventory MCP | 18203 | mcp | `lookup_stock`, `list_stock` |
| orders MCP | 18204 | mcp | `lookup_order`, `list_orders` |

**Success**: each REST origin serves `/openapi.json`, and each MCP origin answers
`initialize` then `tools/list` on `/mcp`, with
`Accept: application/json, text/event-stream`.

## Phase 4: register the four sources (you)

```bash
adma source add --account alpha --target 127.0.0.1:18201 --kind openapi
adma source add --account alpha --target 127.0.0.1:18203 --kind mcp
adma source add --account beta  --target 127.0.0.1:18202 --kind openapi
adma source add --account beta  --target 127.0.0.1:18204 --kind mcp
```

**Success**, one line each:

```
Added source alpha.127.0.0.1.18201.<hex> (origin 127.0.0.1:18201, worker <uuid>).
```

**Assistant verification**: four `OicmMcpSource` rows in the **`litellm`**
database, two owned by each user, the MCP rows carrying `kind=mcp` and
`mcp_path=/mcp`, and one worker per source:

```bash
psql "SELECT target_port, kind, mcp_path, status, tenant FROM \"OicmMcpSource\" ORDER BY target_port"
psql "SELECT count(*) FROM \"OicmMcpRemoteWorker\""
```

Re-run one registration verbatim to show idempotency:

```bash
adma source add --account alpha --target 127.0.0.1:18201 --kind openapi
# MCP source already exists as alpha.127.0.0.1.18201.<hex>; nothing new was created.
```

**Caveat**: re-registering a target whose worker credential was already delivered
is refused (anti-replay). That is the correct behaviour; see Phase 9 for the
reconnect path, and do not try to "fix" it by removing and re-adding.

## Phase 5: run the agent and confirm all four connect

```bash
export MCP_SOURCE_LINK_TUNNEL_URL=https://localhost:18094
adma connector
```

**Success**: four `serving source ...` lines and **no** `tunnel session
finished ... error=` lines. The agent logs a disconnect line for every failed
tunnel attempt, so silence is the signal:

```bash
grep -c "disconnected" /tmp/prod-connector.log     # 0
```

**Assistant verification** that the broker agrees:

```bash
curl -s -H "Authorization: Bearer $ST" http://127.0.0.1:18091/v1/source-link/peers \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); d=d if isinstance(d,list) else d.get("peers",[]); print(len(d)); [print(p["label"],p["phase"],p["healthy_at"] is not None) for p in d]'
# four records, phase=active, healthy=True
```

The two OpenAPI sources also carry a `catalog_fingerprint`. The MCP sources
correctly do not: they serve no OpenAPI catalog.

**Caveats, in order of what you will see**:

- `connector unauthorized` for every worker: the broker is on the wrong database
  or holds a different pepper. Check `peers` is non-empty (Phase 1). An empty peer
  set is the database split; a non-empty one with this error is a pepper mismatch,
  which means the gateway and broker pods were started against different secret
  contents. Restart both so they read the same secret, then re-enroll, because
  credentials minted under the old pepper no longer validate.
- `connection not ready`: the terminator or the 18093 forward is down.
- Immediate close with no error at all: `MCP_SOURCE_LINK_TUNNEL_URL` is unset and
  the agent is dialing the gateway's 443, which has no gRPC route.

## Phase 6: approve the four sources (you, in the UI)

Enrollment is not approval. Each source is `pending_review` and is not callable
until approved.

In the UI at `https://litellm.ecouncil.ae/ui/mcp-servers/`, tab **Submitted MCPs**,
approve each of the four. **Success**: `Pending Review` goes to 0 and `Active` to
4, and the **All Servers** tab lists four.

Administratively, the route the UI uses is `PUT /v1/mcp/server/{id}/approve`:

```bash
K="$(kubectl -n adeo-litellm get secret litellm-master-key -o jsonpath='{.data.master-key}' | base64 -d)"
for id in $(psql "SELECT server_id FROM \"LiteLLM_MCPServerTable\" WHERE server_name LIKE 'alpha.%' OR server_name LIKE 'beta.%'"); do
  curl -s -o /dev/null -w "$id approve=%{http_code}\n" -X PUT \
    "https://litellm.ecouncil.ae/v1/mcp/server/$id/approve" -H "Authorization: Bearer $K"
done
```

**Caveats**:

- The route is `/v1/mcp/server/{id}/approve`. Paths like
  `/mcp-servers/{id}/submit/approval` return 404.
- Approving an already-active row returns 400. That is benign in a loop; check
  the table, not the HTTP code.
- `Transport: http` in the UI is not the source kind. See difference 6; read
  `OicmMcpSource.kind` instead.
- Re-enrolling a source creates a **new** registry row in `pending_review`, so
  approval does not survive a re-enrollment.

## Phase 7: call the tools through the public hostname (you)

The tool names are **not** prefixed with the source name. A REST-derived tool is
named from the OpenAPI `operationId` or method and path, and an MCP tool keeps its
own name. List first, then call:

```bash
K="$(kubectl -n adeo-litellm get secret litellm-master-key -o jsonpath='{.data.master-key}' | base64 -d)"
SID="$(psql "SELECT server_id FROM \"LiteLLM_MCPServerTable\" WHERE server_name LIKE 'alpha.127.0.0.1.18201%'")"

curl -s "https://litellm.ecouncil.ae/mcp-rest/tools/list?server_id=$SID" -H "Authorization: Bearer $K"
# health_health_get, list_inventory_inventory_get, get_inventory_inventory__sku__get
```

Call one REST-derived and one MCP-derived tool:

```bash
curl -s -X POST https://litellm.ecouncil.ae/mcp-rest/tools/call \
  -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d '{"server_id":"'"$SID"'","name":"list_inventory_inventory_get","arguments":{}}'

SID_MCP="$(psql "SELECT server_id FROM \"LiteLLM_MCPServerTable\" WHERE server_name LIKE 'alpha.127.0.0.1.18203%'")"
curl -s -X POST https://litellm.ecouncil.ae/mcp-rest/tools/call \
  -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d '{"server_id":"'"$SID_MCP"'","name":"lookup_stock","arguments":{"sku":"widget"}}'
```

**Success**: the origin's real data, wrapped as MCP content:

```json
{"content":[{"type":"text","text":"{\"items\":[{\"sku\":\"widget\",\"qty\":42,...}]"}],
 "structuredContent":{"items":[...],"count":2},"isError":false}
```

**Caveats**:

- `server_id` is required. Omitting it returns `400 missing_parameter`.
- A wrong tool name returns `404 server_not_found` even when the `server_id` is
  correct, which is misleading. Re-list the tools rather than assuming the server
  disappeared.
- `/v1/mcp/server` returns only servers the gateway could load. An OpenAPI source
  missing from that list is the SSRF block (difference 3), not an approval
  problem.
- An empty `/v1/mcp/server` where the database has four active rows means the pod
  is serving a stale config; a rollout replaces it.

## Phase 8: revoke one source, confirm isolation, restore

Revocation must hit exactly one source. Revoke the alpha inventory REST source and
call it again; it must fail while the other three keep working.

```bash
# adma source list has no worker-id column; read the ids from local state
read -r SID WID GEN < <(python3 -c "
import json
for s in json.load(open('/tmp/prod-accept-state/sources.json'))['sources']:
    if s['name'].startswith('alpha.127.0.0.1.18201'):
        print(s['source_id'], s['worker_id'], s['generation'])
")
curl -s -X POST "https://litellm.ecouncil.ae/mcp-source-link-sources/$SID/workers/$WID/revoke" \
  -H "Authorization: Bearer $K" -H 'Content-Type: application/json' -d "{\"generation\":$GEN}"
```

**Success**: only the revoked source fails, with `Error: upstream returned HTTP
503`, and the other three still return data.

Restoring means a **fresh registration**, because revocation is terminal:

```bash
adma source remove alpha.127.0.0.1.18201.<hex>
adma source add --account alpha --target 127.0.0.1:18201 --kind openapi
# approve the new server_id, then the call works again
```

## Phase 9: disconnect one account

Stop the agent, or disconnect only alpha's tunnel. **Success**: beta's two
sources keep returning data, alpha's two fail fast with `Error: upstream returned
HTTP 503` rather than hanging, no stored key is affected, and restarting the agent
reconnects both.

The reconnect path is **restart with state intact**. `source remove` then
`source add` cannot reconnect a source whose credential was delivered, because
re-registration is refused as a replay. Do not use remove-and-add to recover from
a disconnect.

## Phase 10: cleanup (assistant)

Delete in foreign-key order and drop the accounts. Prod is shared, so do this
deliberately rather than leaving test rows behind:

```bash
psql "DELETE FROM \"OicmMcpRemoteWorker\""
psql "DELETE FROM \"OicmMcpEnrollment\""
psql "DELETE FROM \"OicmMcpSource\""
psql "DELETE FROM \"LiteLLM_MCPServerTable\" WHERE server_name LIKE 'alpha.%' OR server_name LIKE 'beta.%'"

curl -s -X POST https://litellm.ecouncil.ae/user/delete \
  -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d '{"user_ids":["accept-owner-1","accept-owner-2"]}'
```

Stop the agent, the terminator, and the port-forwards. Confirm the registry is
empty and the UI shows no rows.

## What each phase catches if it regresses

- Phase 0: prod account provisioning and the `password_reset_required` gate that
  blocks a connector login.
- Phase 1: the split between the public control plane and the private tunnel, and
  the broker/gateway database pairing. Both are invisible to a health check.
- Phase 4: account-scoped derived names, and rows landing in the database the
  broker reads.
- Phase 5: the tunnel actually authenticating, which requires matching peppers and
  a shared database.
- Phase 6: approval as a separate human gate, and that re-enrollment re-enters
  review.
- Phase 7: OpenAPI registration through the SSRF allowlist, and dispatch to both
  origin kinds through the public hostname.
- Phase 8: revocation fenced per source.
- Phase 9: one account's failure not taking down the other, and that reconnect is
  restart-not-re-add.
