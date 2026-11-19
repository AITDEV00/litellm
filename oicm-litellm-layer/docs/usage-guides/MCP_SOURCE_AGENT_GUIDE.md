# MCP Source Agent: Usage Guide

> **Scope**: How to use the `adma` agent client binary to expose local HTTP and
> MCP services to the dev gateway, end to end: install, log in, register sources,
> run the agent, get sources approved, call tools, and revoke or disconnect. This
> guide is written from a verified run against the dev gateway on 2026-11-19,
> where all four sources served through two ordinary accounts.
>
> **Audience**: An operator or engineer who has a service running on a machine
> that can reach the dev gateway and wants its REST endpoints or MCP tools to be
> callable through LiteLLM.
>
> **Related docs**:
> `oicm-litellm-layer/mcp-source-link/MULTI-ACCOUNT-TEST-PROCEDURE.md`
> (the step-by-step acceptance procedure this guide is distilled from),
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
   your machine                              dev cluster (adeo-litellm)
  ┌──────────────────────────┐             ┌───────────────────────────┐
  │  origin service          │             │  litellm-proxy-dev        │
  │  (REST 127.0.0.1:18201,  │◀── local ──▶│   • public routes         │
  │   MCP  127.0.0.1:18203)  │   HTTP      │   • /v1/mcp/... dispatch  │
  │                          │             │   • approval gate         │
  │  adma connector  ────────┼── outbound ─┤                           │
  │   (one process,          │   gRPC      │  litellm-mcp-broker-dev   │
  │    serves all sources)   │   tunnel    │   (catalog, health,       │
  └──────────────────────────┘             │    route selection)       │
                                           └───────────────────────────┘
                                                        ▲
                                          MCP client ───┘  calls tools through
                                                           the gateway
```

Key consequences:

- The agent dials the **gateway** for the control plane (register, approve,
  health) and the **broker** for the gRPC data plane (dispatch). On dev both are
  reached through port-forwards and local TLS terminators (see the dev harness
  appendix), not a public ingress.
- A source is inert until the agent runs. Registration persists a row; the
  **agent** is what actually connects the origin and serves requests.
- Enrollment is not approval. A newly registered source is callable only after an
  administrator approves it.

---

## Prerequisites

| Need | Detail |
| ---- | ------ |
| The binary | `adma`, installed to `~/.local/bin/adma` (on `PATH`) |
| A gateway account | An ordinary user on the gateway (an `internal_user`), with its **email** and password |
| A reachable origin | The service you want to expose, listening on a `host:port` you can reach from the agent's machine |
| Gateway reachability | For dev, the control-plane forward and TLS terminator (appendix A) |

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

The dev gateway serves the connector through its install endpoint, so installing
against dev is one `curl | sh` from the port-forward:

```sh
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000
# in a second shell:
curl -fsSL http://localhost:14001/install/source-agent | sh
```

The installer prints the commands to run next and the state directory. It does not
itself prompt, because `account add` and `source add` need a terminal and the pipe
into `sh` has none.

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

**Caveat**: the binary must match what the gateway expects. A stale
`~/.local/bin/adma` from an earlier build can fail registration in ways that look
like gateway bugs; when in doubt, reinstall before debugging.

---

## Step 2: Log in

`account add` is interactive: three prompts (gateway URL, username, password),
once per account. It **refuses piped stdin**, so drive it from a real terminal (or
a pseudo-terminal with `script`).

```sh
export MCP_SOURCE_LINK_INSECURE=1     # dev only: self-signed cert. Never on prod.
adma account add alpha
```

Answer:

| Prompt | Value |
| ------ | ----- |
| Gateway URL (https://...) | `https://localhost:14443` (dev, via the TLS terminator) |
| Username | your **email**, e.g. `accept-owner-1@example.invalid` |
| Password (not echoed) | your gateway password |

Repeat for a second account if you want to test multi-account isolation:

```sh
adma account add beta
```

The first account you add is auto-selected (marked `*`). `account select <alias>`
changes the default for `source add` without `--account`.

**Verify**:

```sh
adma account list
# * alpha   https://localhost:14443   accept-owner-1@example.invalid
#   beta    https://localhost:14443   accept-owner-2@example.invalid
ls -l ~/.config/source-agent/accounts/   # keys must be -rw-------
grep -r "$(cat /tmp/accept.pw)" ~/.config/source-agent && echo LEAK || echo clean
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

- On dev the agent must dial the **h2 tunnel endpoint**, not the control-plane
  terminator. Set `MCP_SOURCE_LINK_TUNNEL_URL=https://localhost:18094` or every
  tunnel logs `dial localhost:14443: connection not ready` forever (14443 does not
  negotiate HTTP/2). See appendix A. In a cluster deployment the tunnel URL is
  the public host and this override is not needed.
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
curl -sk https://localhost:14443/v1/mcp/server -H "Authorization: Bearer $MASTER_KEY"
# []
```

Approve in the UI at `http://localhost:14010/ui/mcp-servers/`, tab **Submitted
MCPs**, one row per source. Or, as an administrator, with the master key:

```sh
curl -sk -X PUT "https://localhost:14443/v1/mcp/server/<server_id>/approve" \
  -H "Authorization: Bearer $MASTER_KEY"
```

**Verify**: the same `/v1/mcp/server` call now lists the approved servers.

**Caveats**:

- The approval route is under `/v1/mcp/server/{id}/approve`. There is no
  `/mcp/server/...` form; that returns 405.
- Approval is per source. A re-enrolled source (new `server_id`) is `pending`
  again and needs its own approval.
- The server id is the source id; find it in `adma source list` output or the UI.

---

## Step 7: Call the tools

Approved sources expose their endpoints as MCP tools through the gateway. Every
source gets its own tool namespace prefixed by the derived source name.

List the tools:

```sh
curl -sk "https://localhost:14443/v1/mcp/tools" -H "Authorization: Bearer $KEY"
```

Call a tool. The request needs `server_id`, the tool `name`, and `arguments`:

```sh
# REST-derived tool (inventory lookup)
curl -sk -X POST https://localhost:14443/mcp-rest/tools/call \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"server_id":"<alpha-18201 source_id>",
       "name":"alpha.127.0.0.1.18201-get_inventory_inventory__sku__get",
       "arguments":{"sku":"widget"}}'

# MCP-derived tool (lookup_stock)
curl -sk -X POST https://localhost:14443/mcp-rest/tools/call \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"server_id":"<alpha-18203 source_id>",
       "name":"alpha.127.0.0.1.18203-lookup_stock",
       "arguments":{"sku":"widget"}}'
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
- `/v1/mcp/tools?server_id=<id>` currently **ignores** the filter and returns the
  union of every source's tools. Do not rely on it to scope a listing; the
  `server_id` in the call body is what routes correctly.
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

Find the worker id and generation (`adma source list` prints the worker id; the
generation is in the broker state), then:

```sh
curl -sk -X POST \
  "https://localhost:14443/mcp-source-link-sources/<source_id>/workers/<worker_id>/revoke" \
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
| `MCP_SOURCE_LINK_INSECURE=1` | Skip TLS certificate verification | Dev gateway with a self-signed cert only. Never on prod. |
| `MCP_SOURCE_LINK_TUNNEL_URL=https://host:port` | Dial the gRPC tunnel at this URL instead of the account's gateway URL | Dev, when the control-plane terminator does not speak HTTP/2 (see appendix A) |
| `MCP_SOURCE_LINK_CA_FILE=<path>` | CA bundle to verify the gateway certificate | Environments where you can verify instead of skipping |
| `MCP_SOURCE_LINK_STATE_DIR=<path>` | Override the state directory | Isolated test runs, multiple identities on one machine |

---

## Appendix A: the dev harness (port-forwards and TLS terminators)

The dev gateway has no ingress and its broker tunnel path is not exposed publicly.
Reaching it from a workstation needs forwards and local TLS terminators. This is
test-harness plumbing, not part of the product, but forgetting it is the most
common source of confusing failures.

**Control plane** (register, approve, health): a plain-TLS terminator in front of
the proxy forward.

```
14443 (TLS)  ──▶ tls_proxy.py ──▶ 14010 ──▶ svc/litellm-proxy-dev:4000
14001        ──▶ svc/litellm-proxy-dev:4000      (install endpoint)
```

```sh
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14010:4000 &
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000 &
# tls_proxy.py: TLS on 14443, forwards plain HTTP to 14010
```

**Data plane** (gRPC tunnel): the broker's tunnel listener, fronted by an
HTTP/2-capable TLS terminator. This is the piece people miss.

```
18094 (TLS, ALPN h2) ──▶ grpc_tls_proxy.py ──▶ 18093 ──▶ svc/litellm-mcp-broker-dev:8093
```

```sh
kubectl -n adeo-litellm port-forward svc/litellm-mcp-broker-dev 18093:8093 &
# grpc_tls_proxy.py: TLS with ALPN h2 on 18094, forwards to the h2c forward on 18093
export MCP_SOURCE_LINK_TUNNEL_URL=https://localhost:18094
```

Rebuild whichever forward owns the pod you rolled: a dev gateway rollout replaces
the proxy pod and kills the 14010/14001 forwards; a broker rollout kills the 18093
forward. Re-run the affected forward and restart the terminators if their upstream
went away.

**Why the split**: `14443` is a plain TLS terminator without ALPN, so gRPC (which
requires HTTP/2) cannot run over it. If you point the agent's tunnel at `14443`
you get `connection not ready` on every attempt even though control-plane calls
work fine.

---

## Appendix B: common failures and their cause

| Symptom | Cause | Fix |
| ------- | ----- | --- |
| `dial localhost:14443: connection not ready` on every tunnel | Tunnel dialing the non-h2 terminator | Set `MCP_SOURCE_LINK_TUNNEL_URL=https://localhost:18094` and run `grpc_tls_proxy.py` + the 18093 forward |
| `account add needs a terminal` | Piped stdin | Run it in a real TTY (or `script -qec`) |
| Login returns 401 with correct password | Password hash no longer matches (rotated/reset) | Reset the account password; the agent needs a valid exchange |
| `gateway refused registration ... 409` on `source add` | The worker was already delivered (anti-replay) | Do not remove+re-add; restart the agent with state, or reset the worker to undelivered server-side |
| `400 missing_parameter: server_id is required` | Call body omitted `server_id` | Add it to the tool call |
| Tool returns `Error: upstream returned HTTP 503` | The agent for that source is not connected | Start/restart the agent; check its log for that source |
| `recover` returns `409 stale generation or invalid lifecycle transition` | Revoked is terminal | Enroll a fresh source id; revocation cannot be undone |
| `/v1/mcp/tools?server_id=` returns every source's tools | Filter is not scoped (known) | Rely on `server_id` in the call body for scoping |
| New source callable list still empty after `source add` | Not yet approved | Approve the source (Step 6) |
