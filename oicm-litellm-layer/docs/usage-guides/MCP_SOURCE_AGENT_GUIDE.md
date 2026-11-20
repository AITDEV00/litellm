# MCP Source Agent: Usage Guide

> **Scope**: How to use the `adma` agent client binary to expose local HTTP and
> MCP services to a LiteLLM gateway, end to end: install, log in, register
> sources, run the agent, get sources approved, call tools, and revoke or
> disconnect. Written from two verified runs on 2026-11-19: all four sources
> served through two ordinary accounts against the dev gateway, and the same four
> served through the **prod** gateway at `https://litellm.ecouncil.ae` with an
> agent on a workstation.
>
> **Audience**: An operator or engineer who has a service running on a machine
> that can reach a gateway and wants its REST endpoints or MCP tools to be
> callable through LiteLLM.
>
> **Related docs**:
> `oicm-litellm-layer/mcp-source-link/MULTI-ACCOUNT-TEST-PROCEDURE.md`
> (the dev acceptance procedure),
> `oicm-litellm-layer/mcp-source-link/MULTI-ACCOUNT-TEST-PROCEDURE-PROD.md`
> (the prod acceptance procedure, and the source of the prod-specific notes
> here),
> [running-system/README.md](../mcp-source-broker/running-system/README.md)
> (what the broker and connector are),
> [GATEWAY_GUIDE.md](GATEWAY_GUIDE.md) (calling the gateway generally)

---

## The model in one picture

The agent is a **reverse connector**. You do not open a port on the gateway to
reach your service; you run a small binary next to your service that dials **out**
to the gateway and serves requests back through that outbound connection. Nothing
in your network needs an inbound rule.

```
   your machine                              cluster (adeo-litellm)
  ┌──────────────────────────┐             ┌───────────────────────────┐
  │  origin service          │             │  litellm-proxy            │
  │  (REST 127.0.0.1:18201,  │◀── local ──▶│   • public routes         │
  │   MCP  127.0.0.1:18203)  │   HTTP      │   • /v1/mcp/... dispatch  │
  │                          │             │   • approval gate         │
  │  adma connector  ────────┼── outbound ─┤                           │
  │   (one process,          │   gRPC      │  litellm-mcp-broker       │
  │    serves all sources)   │   tunnel    │   (catalog, health,       │
  └──────────────────────────┘             │    route selection)       │
                                           └───────────────────────────┘
                                                        ▲
                                          MCP client ───┘  calls tools through
                                                           the gateway
```

Key consequences:

- The agent talks to **two different endpoints**. Control plane (login, register,
  approve, health) goes to the **gateway**. The gRPC data plane (dispatch) goes to
  the **broker's tunnel listener**, which is a separate port. They are not the
  same host:port, even on prod.
- A source is inert until the agent runs. Registration persists a row; the
  **agent** is what actually connects the origin and serves requests.
- Enrollment is not approval. A newly registered source is callable only after an
  administrator approves it.

### How the two endpoints are reached

This is the single biggest difference between environments, and the cause of most
confusing failures.

| | Dev | Prod |
| --- | --- | --- |
| Gateway (control plane) | `kubectl port-forward` of `svc/litellm-proxy-dev :4000`, plus a local TLS terminator on `14443` | the ingress at `https://litellm.ecouncil.ae`, which routes everything to the gateway's port 4000 |
| Broker tunnel (data plane) | port-forward of `svc/litellm-mcp-broker-dev :8093`, plus an **HTTP/2** TLS terminator on `18094` | the same public host on 443: a separate Ingress (`litellm-mcp-tunnel`) routes `/grpctunnel.v1.TunnelService/` to the broker with `backend-protocol: GRPC` |
| How the agent picks the tunnel | `MCP_SOURCE_LINK_TUNNEL_URL` (required on dev) | the account's gateway URL already works: set `MCP_SOURCE_LINK_TUNNEL_URL=https://litellm.ecouncil.ae`, no port |
| Extra port needed | none (forward is local only) | **none**: 443 is the only port a client needs |
| Certificate | self-signed, needs `MCP_SOURCE_LINK_INSECURE=1` | valid, do **not** set `INSECURE` |

The agent derives its tunnel target from the account's gateway URL, defaulting to
port 443. On prod that same host and port carry both endpoints, so the account URL
is already correct and `MCP_SOURCE_LINK_TUNNEL_URL` is not needed. On dev the
gateway forward has no gRPC route, so the override is required. See appendix A.

---

## Prerequisites

| Need | Detail |
| ---- | ------ |
| The binary | `adma`, installed to `~/.local/bin/adma` (on `PATH`) |
| A gateway account | An ordinary user (an `internal_user`, **not** an admin), with its **email** and password |
| A reachable origin | The service you want to expose, listening on a `host:port` you can reach from the agent's machine |
| Gateway reachability | The control plane and the tunnel. Prod: both on 443 through the ingress, nothing to set up. Dev: forwards plus terminators for each. See appendix A. |

The agent stores state under `~/.config/source-agent/`:

```
~/.config/source-agent/
├── accounts.json        # account list and the selected account
├── accounts/<alias>.key # one gateway key per account, mode 0600
├── sources.json         # registered sources and their worker credentials
└── selected             # convenience copy of the selected account alias
```

Everything in that directory is secret except the alias names. The key files must
stay `-rw-------`; the agent writes them that way and you should never loosen it.

---

## Step 1: Install the binary

Each gateway serves the connector through its own install endpoint, which renders
the active release and checksum-verifies the asset before running it. Install from
whichever gateway you are pointing at:

```sh
# prod: straight from the public hostname
curl -fsSL https://litellm.ecouncil.ae/install/source-agent | sh

# dev: through the port-forward
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000
# in a second shell:
curl -fsSL http://localhost:14001/install/source-agent | sh
```

Either way the installer prints the commands to run next and the state directory.
It does not itself prompt, because `account add` and `source add` need a terminal
and the pipe into `sh` has none.

If the release endpoint is not published (the publisher is an admin artifact
upload that verifies a checksum), install the already-built binary directly:

```sh
install -m 0755 /tmp/source-link-static ~/.local/bin/adma
```

**Verify**: `adma --help` shows the `account`, `source`, and agent command
families, including `--kind openapi` and `--kind mcp`. A fresh install has no
state:

```sh
adma account list   # no accounts configured; run: adma account add <alias>
adma source list    # no sources configured; run: adma source add
```

Confirm you have the published release rather than an older build:

```sh
sha256sum ~/.local/bin/adma
# prod run 2026-11-19: 0f37d785de21a7ed9246e53d6a4a4a67dc9bdb796868170df095fd261788e49a (0.5.5)
```

**Caveat**: the binary must match what the gateway expects. A stale
`~/.local/bin/adma` from an earlier build can fail registration in ways that look
like gateway bugs; when in doubt, reinstall before debugging. The size is a quick
tell: the 0.5.4 build on a dev box was 13,844,640 bytes and the 0.5.5 release is
13,877,408.

---

## Step 2: Log in

`account add` is interactive: three prompts (gateway URL, username, password),
once per account. It **refuses piped stdin**, so drive it from a real terminal (or
a pseudo-terminal with `script`).

```sh
# prod: no --insecure. The certificate is valid.
adma account add alpha

# dev only: self-signed cert. Never set this against prod.
export MCP_SOURCE_LINK_INSECURE=1
adma account add alpha
```

Answer:

| Prompt | Prod | Dev |
| ------ | ---- | --- |
| Gateway URL (https://...) | `https://litellm.ecouncil.ae` | `https://localhost:14443` |
| Username | your **email**, e.g. `accept-owner-1@example.invalid` | same |
| Password (not echoed) | your gateway password | same |

Repeat for a second account if you want to test multi-account isolation:

```sh
adma account add beta
```

The first account you add is auto-selected (marked `*`). `account select <alias>`
changes the default for `source add` without `--account`.

**Verify**:

```sh
adma account list
# prod:
# * alpha   https://litellm.ecouncil.ae   accept-owner-1@example.invalid
#   beta    https://litellm.ecouncil.ae   accept-owner-2@example.invalid
# dev:
# * alpha   https://localhost:14443       accept-owner-1@example.invalid
#   beta    https://localhost:14443       accept-owner-2@example.invalid
ls -l ~/.config/source-agent/accounts/   # keys must be -rw-------
grep -r "$(cat /tmp/accept-prod1.pw)" ~/.config/source-agent && echo LEAK || echo clean
```

**Caveats**:

- The login looks the user up by **email**, not `user_id`. Typing a user id
  fails with invalid credentials.
- The raw password must never land on disk. The agent writes only the exchanged
  key; the `grep` above proves it. If you scripted the prompts, do not leave the
  password in a shell history or a world-readable temp file.
- `MCP_SOURCE_LINK_INSECURE=1` disables certificate verification. It exists for
  dev gateways fronted by a self-signed certificate and must never be set against
  production, because it accepts any certificate.
- **Getting an account on prod** is an administrator step, and neither obvious
  route works on its own. `POST /user/new` rejects a `password` field outright
  ("Users set their own password through an invitation link"), so create the user
  and then set the password with `POST /user/update`. The account still carries
  `password_reset_required`, and the connector login refuses it with
  `password reset required before this account can be used for a connector`.
  Clear that flag server-side:

  ```bash
  PW="$(kubectl -n adeo-litellm get secret litellm-db-credentials -o jsonpath='{.data.DATABASE_URL}' | base64 -d | sed -E 's#.*://litellm:([^@]+)@.*#\1#')"
  kubectl -n adeo-litellm exec -i adeo-litellm-postgres-1 -c postgres -- \
    env PGPASSWORD="$PW" psql -h 127.0.0.1 -U litellm -d litellm -tAc \
    "UPDATE \"LiteLLM_UserTable\" SET password_reset_required=false, last_breach_check_at=NULL WHERE user_id='<user_id>'"
  ```

  Do not instead retry the login: each failed attempt re-runs the breach screen
  and re-stamps the flag, so retrying never converges.
- An **admin** account cannot be used here. The gateway refuses to mint a
  connector key for a `proxy_admin`, by design, so the account must be an
  ordinary `internal_user`.

---

## Step 3: Confirm your origins answer

Before registering, make sure each service you want to expose responds the way
the agent expects. The agent forwards requests verbatim; it does not translate.

**REST (OpenAPI) origins** serve a spec and real endpoints:

```sh
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18201/openapi.json   # 200
curl -s http://127.0.0.1:18201/inventory                                       # real data
```

**MCP origins** answer `initialize` and then `tools/list` on their path (default
`/mcp`):

```sh
SID=$(curl -s -X POST http://127.0.0.1:18203/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
  -D- -o /dev/null | grep -i mcp-session-id | awk '{print $2}')

curl -s -X POST http://127.0.0.1:18203/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H "mcp-session-id: $SID" -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

curl -s -X POST http://127.0.0.1:18203/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H "mcp-session-id: $SID" -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

**Caveats**:

- An MCP server must send `Accept: application/json, text/event-stream` on its
  JSON-RPC responses; the agent's health probe and dispatch rely on it. A server
  that returns only `text/plain` fails health.
- The target is `host:port` only. No scheme, no path. The path for an MCP source
  is a separate `--path` (default `/mcp`).

---

## Step 4: Register sources

Registration records a source under an account and mints a worker credential. It
does **not** connect anything yet. Run `source add` once per service:

```sh
adma source add --account alpha --target 127.0.0.1:18201 --kind openapi
adma source add --account alpha --target 127.0.0.1:18203 --kind mcp
adma source add --account beta  --target 127.0.0.1:18202 --kind openapi
adma source add --account beta  --target 127.0.0.1:18204 --kind mcp
```

Flags:

| Flag | Meaning | Default |
| ---- | ------- | ------- |
| `--account <alias>` | register under this account | the selected account |
| `--target <host:port>` | the origin address | required |
| `--kind openapi\|mcp` | origin type | `openapi` |
| `--spec <path>` | pin a specific OpenAPI document | fetch the origin's own `/openapi.json` |
| `--path <path>` | MCP endpoint path | `/mcp` |

Each success prints one line:

```
Added source alpha.127.0.0.1.18201 (origin 127.0.0.1:18201, worker <uuid>).
```

**Derived names**: the source name is derived from account and target
(`alpha.127.0.0.1.18201`). Two accounts registering the same target get two
independent sources with distinct derived names; that is how multi-account
isolation is proven.

**Idempotency**: re-running the same `source add` is a no-op:

```
MCP source already exists as alpha.127.0.0.1.18201; nothing new was created.
```

**Caveats**:

- Registration is idempotent but **not** a way to refresh a delivered worker.
  If a worker's credential was already delivered to a running agent, re-adding
  409s with "gateway refused registration". See Step 8 and Step 9.
- `--spec` is only for pinning a reviewed document. Without it the connector
  fetches the origin's own `/openapi.json` at registration time; if that endpoint
  is down, registration fails.

**Verify**: `adma source list` shows all sources with account, kind, target, and
status `registered`.

---

## Step 5: Run the agent

One process serves every registered source across every account:

```sh
export MCP_SOURCE_LINK_INSECURE=1
adma connector
```

Success looks like one serving line per source and no disconnect lines:

```
serving source alpha.127.0.0.1.18201 (127.0.0.1:18201) for account alpha
serving source alpha.127.0.0.1.18203 (127.0.0.1:18203) for account alpha
serving source beta.127.0.0.1.18202 (127.0.0.1:18202) for account beta
serving source beta.127.0.0.1.18204 (127.0.0.1:18204) for account beta
```

Leave it running. A source whose origin is unreachable logs its own failure and
the others keep serving; that independence is the point.

**Caveats**:

- The agent must dial the **h2 tunnel endpoint**, never the control-plane
  endpoint, and on **both** environments this needs an explicit
  `MCP_SOURCE_LINK_TUNNEL_URL`. The tunnel listener is the broker's port 8093; the
  agent otherwise derives the tunnel target from the account's gateway URL
  (default port 443), which on prod is the gateway with no gRPC route and on dev
  is a terminator that does not negotiate HTTP/2. The failure differs but the
  cause is the same:

  | Environment | Set | Symptom if unset |
  | ----------- | --- | ---------------- |
  | dev | `MCP_SOURCE_LINK_TUNNEL_URL=https://localhost:18094` | `dial localhost:14443: connection not ready` forever |
  | prod | `MCP_SOURCE_LINK_TUNNEL_URL=https://litellm.ecouncil.ae` | nothing to configure: this is the default the account URL already gives |

  The override is **dev only**. Prod needs no tunnel URL because the ingress routes
  `/grpctunnel.v1.TunnelService/` on the same 443 the control plane uses. See
  appendix A.
- **Dev only**: a broker rollout replaces the pod and kills the port-forward behind
  your terminator. Re-run the forward and restart the terminator, then restart the
  agent; otherwise it logs `connection not ready` indefinitely. On prod the tunnel
  goes through the Ingress, so a broker rollout drops live tunnels briefly but no
  forward breaks and the agent reconnects on its own.
- The agent reloads when the source set changes (`source add`/`source remove`
  tell it to reload), so you do not restart it for a new source. It does not
  reload on credential changes from a failed re-enrollment (Step 9).
- Run it under a supervisor (`systemctl --user`) for anything long-lived so it
  reconnects after a reboot.

---

## Step 6: Get sources approved

Enrollment is not approval. Each new source lands in the MCP registry as
`pending_review` and is **not callable** until an administrator approves it. This
is a deliberate human gate.

Before approval, the callable list is empty:

```sh
# prod
curl -s https://litellm.ecouncil.ae/v1/mcp/server -H "Authorization: Bearer $MASTER_KEY"
# dev
curl -sk https://localhost:14443/v1/mcp/server -H "Authorization: Bearer $MASTER_KEY"
# []
```

Approve in the UI, tab **Submitted MCPs**, one row per source: prod
`https://litellm.ecouncil.ae/ui/mcp-servers/`, dev
`http://localhost:14010/ui/mcp-servers/`. Or, as an administrator, with the
master key:

```sh
# prod
curl -s -X PUT "https://litellm.ecouncil.ae/v1/mcp/server/<server_id>/approve" \
  -H "Authorization: Bearer $MASTER_KEY"
# dev
curl -sk -X PUT "https://localhost:14443/v1/mcp/server/<server_id>/approve" \
  -H "Authorization: Bearer $MASTER_KEY"
```

**Verify**: the same `/v1/mcp/server` call now lists the approved servers.

**Caveats**:

- The approval route is `PUT /v1/mcp/server/{id}/approve`. There is no
  `/mcp/server/...` form (405) and no `/mcp-servers/{id}/submit/approval` form
  (404); both were tried against prod on 2026-11-19.
- Approving an **already active** row returns 400 rather than being a no-op. In a
  loop over several ids, check the table afterwards rather than trusting the
  HTTP codes.
- Approval is per source. A re-enrolled source (new `server_id`) is `pending`
  again and needs its own approval. Approval does not survive a re-enrollment.
- The server id is the source id; find it in `adma source list` output or the UI.
- **The UI's `Transport: http` label is not the source kind.** The Submitted MCPs
  tab prints the raw registry `transport` column, which the source broker sets to
  `http` for every source, so an OpenAPI source and an MCP source look identical
  there. The rest of the UI derives the display kind from `spec_path` via
  `handleTransport` in `ui/litellm-dashboard/src/components/mcp_tools/types.tsx`.
  When you need the real kind, read `OicmMcpSource.kind`:

  ```bash
  PW="$(kubectl -n adeo-litellm get secret litellm-db-credentials -o jsonpath='{.data.DATABASE_URL}' | base64 -d | sed -E 's#.*://litellm:([^@]+)@.*#\1#')"
  kubectl -n adeo-litellm exec -i adeo-litellm-postgres-1 -c postgres -- \
    env PGPASSWORD="$PW" psql -h 127.0.0.1 -U litellm -d litellm -tAc \
    'SELECT alias, kind, target_host, target_port, status FROM "OicmMcpSource" ORDER BY target_port'
  ```

---

## Step 7: Call the tools

Approved sources expose their endpoints as MCP tools through the gateway.

**Tool names are not prefixed with the source name.** A REST-derived tool is named
from the OpenAPI `operationId`, or from the method and path when the document has
none, and an MCP tool keeps the name its server declares. Always list before you
call; the names are not predictable from the target.

List the tools for one source:

```sh
curl -s "https://litellm.ecouncil.ae/mcp-rest/tools/list?server_id=$SID" \
  -H "Authorization: Bearer $KEY"
```

Names observed on prod 2026-11-19, for a REST origin whose spec had no
`operationId` and a MCP origin declaring its own:

| Source kind | Tools |
| ----------- | ----- |
| openapi, `/inventory` and `/inventory/{sku}` | `health_health_get`, `list_inventory_inventory_get`, `get_inventory_inventory__sku__get` |
| openapi, `/orders` and `/orders/{order_id}` | `health_health_get`, `list_orders_orders_get`, `get_order_orders__order_id__get` |
| mcp, declaring `lookup_stock`/`list_stock` | `lookup_stock`, `list_stock` |
| mcp, declaring `lookup_order`/`list_orders` | `lookup_order`, `list_orders` |

Note that an OpenAPI source also exposes a `health_health_get` derived from the
origin's `/health` endpoint if its spec includes one.

Call a tool. The request needs `server_id`, the tool `name`, and `arguments`:

```sh
# REST-derived tool
curl -s -X POST https://litellm.ecouncil.ae/mcp-rest/tools/call \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"server_id":"<alpha-18201 source_id>",
       "name":"list_inventory_inventory_get","arguments":{}}'

# MCP-derived tool
curl -s -X POST https://litellm.ecouncil.ae/mcp-rest/tools/call \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"server_id":"<alpha-18203 source_id>",
       "name":"lookup_stock","arguments":{"sku":"widget"}}'
```

Both return the origin's real data wrapped as MCP content, for example:

```json
{"content":[{"type":"text","text":"{\"found\":true,\"sku\":\"widget\",\"qty\":42,\"location\":\"aisle-3\"}"}],
 "structuredContent":{"found":true,"sku":"widget","qty":42,"location":"aisle-3"},
 "isError":false,"resultType":"complete"}
```

**Caveats**:

- `server_id` is **required** in the call body. Omitting it returns
  `400 missing_parameter`. It also scopes dispatch: two sources with the same
  tool name are disambiguated by `server_id`.
- A tool name that does not exist returns `404 server_not_found` naming the
  **server**, not the tool, even when the `server_id` is correct. That message is
  misleading; re-list the tools rather than concluding the server vanished.
- `/v1/mcp/tools?server_id=<id>` currently **ignores** the filter and returns the
  union of every source's tools. Do not rely on it to scope a listing; the
  `server_id` in the call body is what routes correctly.
- **An OpenAPI source can be approved and still be missing from
  `/v1/mcp/server`.** The gateway fetches each OpenAPI source's spec from
  `http://127.0.0.1:4000/install/mcp-source-broker/<id>/openapi.json`, and the
  SSRF guard blocks loopback unless the host is allowlisted. Without
  `user_url_allowed_hosts: [127.0.0.1:4000]` in `general_settings` the row is
  skipped during reload with `URL targets a blocked address (127.0.0.1)` in the
  gateway log, while MCP sources (no spec) load normally. The base config now
  carries this and dev always did; if you see only the MCP sources listed, check
  this setting first.
- When a tool fails because the agent for that source is not connected, the
  response is an `isError` text like `Error: upstream returned HTTP 503`, not a
  non-200. Check the agent, not the gateway, when a previously working source
  starts erroring.
- Keys must be allowed to reach these routes. Use a key whose `allowed_routes`
  covers the MCP routes, or the master key for verification.

---

## Step 8: Revoke a source

Revoking tears the source down at the gateway: the agent's tunnel for it stops
dispatching and calls fail. Use the lifecycle action with the worker id and its
current generation.

Find the ids from local state. `adma source list` shows name, account, kind and
target but **not** the worker id, so read them from `sources.json`:

```sh
python3 -c "
import json
for s in json.load(open('$HOME/.config/source-agent/sources.json'))['sources']:
    print(s['name'], s['source_id'], s['worker_id'], s['generation'])
"
```

Then revoke (prod shown; use the dev base and `-sk` on dev):

```sh
curl -s -X POST \
  "https://litellm.ecouncil.ae/mcp-source-link-sources/<source_id>/workers/<worker_id>/revoke" \
  -H "Authorization: Bearer $MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"generation":<current_generation>}'
# {"worker_id":"...","source_id":"...","phase":"revoked","generation":<n+1>}
```

Revocation is scoped to exactly that source. Confirm: the revoked source returns
`Error: upstream returned HTTP 503` while every other source still returns data.

**Caveats**:

- **Revocation is terminal.** There is no `revoked → active` transition. A
  `recover` on a revoked worker returns `409 stale generation or invalid
  lifecycle transition`. To bring the source back you must enroll it fresh under a
  **new source id**; re-registering the same target fails because the revoked
  worker still holds the derived label. Plan revocation as permanent.
- Revoke is an **administrator** action (master key or admin role).

---

## Step 9: Disconnect and reconnect (losing the agent)

If the agent stops, every source it serves becomes unavailable. Calls do not hang;
they fail fast (well under a second) with `Error: upstream returned HTTP 503`.

What is scoped and what is not:

- **Failure is scoped per source.** Stopping one account's tunnels does not
  affect the other account's sources; only the disconnected sources error.
- **Stored keys are unaffected.** Disconnecting does not touch the account keys
  or the origin services.
- **Restart reconnects.** Start the agent again with its persisted state intact;
  it re-dials every source and all of them serve again.

**Caveat, and the reason this needs its own section**: a client-side
`source remove` followed by `source add` **cannot** reconnect a source. Once a
worker's credential has been delivered to a running agent, re-registration is
refused (anti-replay), so the local record goes away but the source stays
unserved. The reliable reconnect path is to **restart the agent with its state
intact**, not to remove and re-add. If you truly removed a local source and need
it back, you must reset its worker to undelivered server-side (an operator action)
before re-adding, or enroll it under a new source id.

---

## Step 10: Clean up

When you are done:

1. Stop the agent (`pkill -f "adma connector"`, or your supervisor's stop).
2. Remove sources you no longer want served (`adma source remove <name>` for
   local state; the gateway registration is separate).
3. As administrator, delete the gateway-side rows in foreign-key order: workers,
   connectors, enrollments, sources, then the MCP registry entry for each source.
4. Reload the gateway so the UI shows the current list.

Leaving sources registered but the agent stopped is safe: they simply error until
an agent serves them again.

---

## Environment variables reference

| Variable | Effect | When to set |
| -------- | ------ | ----------- |
| `MCP_SOURCE_LINK_TUNNEL_URL=https://host:port` | Dial the gRPC tunnel at this URL instead of the account's gateway URL | **Dev only**. On prod the account URL already works over 443 and this can be left unset. |
| `MCP_SOURCE_LINK_INSECURE=1` | Skip TLS certificate verification | Dev gateway with a self-signed cert only. Never on prod. |
| `MCP_SOURCE_LINK_CA_FILE=<path>` | CA bundle to verify the gateway certificate | Environments where you can verify instead of skipping |
| `MCP_SOURCE_LINK_STATE_DIR=<path>` | Override the state directory | Isolated test runs, multiple identities on one machine |

---

## Appendix A: reaching the gateway and the tunnel

Both environments split the agent's two endpoints onto two backends, but prod
presents both on **one host and port**, so a prod client needs nothing but 443.
Dev cannot, because its tunnel sits behind a local forward. This section is the
plumbing for each.

### The tunnel listener

The gRPC tunnel is served by the broker, on the service port named `link-tunnel`:

```sh
kubectl -n adeo-litellm get svc litellm-mcp-broker -o jsonpath='{range .spec.ports[*]}{.name}={.port}{"\n"}{end}'
# data=8888   link-tunnel=8093   remote-control=9091
```

It speaks **plain h2c**; it does not terminate TLS itself. Anything in front of
it must speak HTTP/2 to it and terminate TLS for the client.

### Prod

Prod routes the tunnel through the ingress, so the agent needs no port-forward and
no extra port. Two Ingress objects share `litellm.ecouncil.ae`:

```
https://litellm.ecouncil.ae  ─┬─▶ /                              ─▶ svc/litellm-proxy:4000      (HTTP)
                              └─▶ /grpctunnel.v1.TunnelService/  ─▶ svc/litellm-mcp-broker:link-tunnel (GRPC)
```

The tunnel route is its own Ingress (`deploy/prod/mcp-tunnel-ingress.yaml`)
because `nginx.ingress.kubernetes.io/backend-protocol: GRPC` is a **per-Ingress**
annotation, not per-path. Marking the gateway's Ingress GRPC would force the
gateway's own HTTP traffic to gRPC, so the tunnel prefix must live in a separate
object. Its backend is the broker's `link-tunnel` port; nginx terminates TLS and
speaks h2c to the sidecar.

```sh
export MCP_SOURCE_LINK_TUNNEL_URL=https://litellm.ecouncil.ae   # no port
adma connector
```

Verify the route from a workstation, with no forwards running:

```sh
curl -s -o /dev/null -w '%{http_code} %{http_version} %{content_type}\n' --http2 \
  -X POST https://litellm.ecouncil.ae/grpctunnel.v1.TunnelService/OpenReverseTunnel \
  -H 'content-type: application/grpc' -H 'te: trailers' --data-binary 'x'
# 200 2 application/grpc
```

A `404` with `content-type: application/json` means the request fell through to
the gateway, so the tunnel Ingress is missing or not programmed. That is the
signal to look at the Ingress, not at the agent.

The tunnel Ingress sets `proxy-read-timeout: 3600` because nginx would otherwise
cut an idle upstream stream at 60s, which the agent sees as a reconnect loop.

### Dev

Dev has no ingress at all, so both endpoints need forwards and terminators:

```
14443 (TLS)          ─▶ tls_proxy.py      ─▶ 14010 ─▶ svc/litellm-proxy-dev:4000
14001                ─▶                     svc/litellm-proxy-dev:4000   (install)
18094 (TLS, ALPN h2) ─▶ grpc_tls_proxy.py ─▶ 18093 ─▶ svc/litellm-mcp-broker-dev:8093
```

```sh
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14010:4000 &
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000 &
kubectl -n adeo-litellm port-forward svc/litellm-mcp-broker-dev 18093:8093 &
# tls_proxy.py: TLS on 14443, forwards plain HTTP to 14010 (no ALPN, so no gRPC)
# grpc_tls_proxy.py: TLS with ALPN h2 on 18094, forwards to the h2c forward on 18093
export MCP_SOURCE_LINK_TUNNEL_URL=https://localhost:18094
export MCP_SOURCE_LINK_INSECURE=1
```

**Why the split**: `14443` is a plain TLS terminator without ALPN, so gRPC (which
requires HTTP/2) cannot run over it. If you point the agent's tunnel at `14443`
you get `connection not ready` on every attempt even though control-plane calls
work fine.

**Rebuild whichever forward owns the pod you rolled**: a gateway rollout replaces
the proxy pod and kills the 14010/14001 forwards; a broker rollout kills the 18093
forward. Re-run the affected forward and restart the terminators if their upstream
went away.

---

## Appendix B: common failures and their cause

| Symptom | Cause | Fix |
| ------- | ----- | --- |
| `dial localhost:14443: connection not ready` on every tunnel (dev) | Tunnel dialing the non-h2 terminator | Set `MCP_SOURCE_LINK_TUNNEL_URL=https://localhost:18094` and run `grpc_tls_proxy.py` + the 18093 forward |
| Every tunnel closes at once, no server-side log (prod) | The gRPC tunnel Ingress is missing, so `/grpctunnel.v1.TunnelService/` falls through to the gateway | Apply `deploy/prod/mcp-tunnel-ingress.yaml`; the probe should answer `200 application/grpc`, not a JSON `404` |
| `connector unauthorized` for every worker, broker `peers` empty | Broker on a different database than the gateway | Give the broker the gateway's `DATABASE_URL` (`litellm-db-credentials`), not `adeo-litellm-postgres-app` |
| `connector unauthorized` with a non-empty `peers` | Gateway and broker pods started against different pepper contents | Restart both so they read the same secret, then re-enroll (old credentials will not validate) |
| `password reset required before this account can be used for a connector` | `password_reset_required` set on the account | Clear the flag server-side (Step 2); retrying the login re-stamps it |
| `a connection must use an ordinary user account, not an admin` | The account is a `proxy_admin` | Create an `internal_user` |
| Only MCP sources listed; OpenAPI sources missing from `/v1/mcp/server` | SSRF guard blocking the loopback `spec_path` | Add `127.0.0.1:4000` to `user_url_allowed_hosts` (Step 7) |
| UI shows `Transport: http` for an OpenAPI source | The tab prints the raw registry column, not the derived kind | Read `OicmMcpSource.kind`; cosmetic only |
| `account add needs a terminal` | Piped stdin | Run it in a real TTY (or `script -qec`) |
| Login returns 401 with correct password | Password hash no longer matches (rotated/reset) | Reset the account password; the agent needs a valid exchange |
| `gateway refused registration ... 409` on `source add` | The worker was already delivered (anti-replay) | Do not remove+re-add; restart the agent with state, or reset the worker to undelivered server-side |
| `400 missing_parameter: server_id is required` | Call body omitted `server_id` | Add it to the tool call |
| `404 server_not_found` with a valid `server_id` | The tool `name` is wrong | Re-list tools; names are not source-prefixed |
| Tool returns `Error: upstream returned HTTP 503` | The agent for that source is not connected | Start/restart the agent; check its log for that source |
| `recover` returns `409 stale generation or invalid lifecycle transition` | Revoked is terminal | Enroll a fresh source id; revocation cannot be undone |
| `/v1/mcp/tools?server_id=` returns every source's tools | Filter is not scoped (known) | Rely on `server_id` in the call body for scoping |
| New source callable list still empty after `source add` | Not yet approved | Approve the source (Step 6) |
