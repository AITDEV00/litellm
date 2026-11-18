# Multi-account connector: guided end-to-end procedure

This is the hands-on procedure for proving the multi-account `adma` client
against the live dev gateway. It is written as a guided session: the assistant sets
up state and hands you the exact commands, you run the interactive ones yourself,
and the assistant verifies each phase before the next begins. Run it top to bottom.

Requirements this proves: `MULTI-ACCOUNT-SOURCE-GAPS.md`. Unit and integration
tests live in `internal/state`, `internal/setup`, `internal/target` and the Python
broker suite; this document proves the wiring through the real binary and a live
gateway.

Environment facts that will cost you time if you forget them:

- The password login looks the user up by **email**, not `user_id`. Enter the email
  as the username.
- The public source routes (`POST /mcp-sources`,
  `POST /mcp-source-link-sources/{id}/workers`) are served by the **proxy**
  (`litellm-proxy-dev`), not the broker pod.
- Source targets are host:port only, and the broker registers every source as an
  HTTP/OpenAPI origin. A native MCP server is registered with a minimal OpenAPI
  document that describes its `/mcp` endpoint, because the connector forwards the
  path verbatim and the broker needs a catalog.
- Restarting the proxy kills the REST port-forward. Re-run it before the next
  phase.

Throughout, the dev gateway is reached at `https://localhost:14443` (TLS
terminator in front of the `14010` forward). The self-signed certificate is why the
binary is run with `--insecure`.

## Phase 0: provision the two accounts

The assistant does this. Two ordinary users on the dev gateway, one per person, so
later phases can prove that an equal target under two owners stays two sources.

Success: both rows exist and both can exchange their password for a key.

```sql
SELECT user_id, user_email, user_role, password_reset_required
  FROM "LiteLLM_UserTable"
 WHERE user_id IN ('accept-owner-1','accept-owner-2');
-- both rows: user_role = internal_user, password_reset_required = false
```

Hand-off: the assistant gives you the two credential pairs, then stops. Do not
continue until you have both.

| Account | Username (email) | Password |
|---|---|---|
| alpha | `accept-owner-1@example.invalid` | `/tmp/accept.pw` |
| beta | `accept-owner-2@example.invalid` | `/tmp/accept2.pw` |

## Phase 1: install the connector binary

The assistant publishes the binary the installer fetches, then hands you the
install command. The dev gateway serves the connector through its install
endpoint, so installing against dev is one `curl | sh` from the published
release.

The installer and the binary it fetches are released together and checksum
verified. It does not itself prompt: `account add` and `source add` need a
terminal, and the pipe into `sh` has none, so the installer prints those
commands instead of running them.

```
curl -fsSL http://localhost:14010/install/source-agent | sh
```

Success: `Source connector installed`, the three follow-up commands to run, and
`State directory: ...`. The binary lands at `~/.local/bin/adma`, which is on
PATH, so the command is just `adma`, and `--help` shows the `account` and
`source` command families, including `--kind openapi` and `--kind mcp`.

The installer reads its asset base from the release, which is the dev proxy the
port-forward exposes as `14001`. If that forward is down the asset download fails;
bring it up first:

```
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000
```

If the release endpoint is not published at all (the publisher is an admin
artifact upload, and the installer verifies a SHA that must match the uploaded
bytes), install the already-built binary directly instead:

```
install -m 0755 /tmp/source-link-static ~/.local/bin/adma
~/.local/bin/adma --help
```

A fresh install has no accounts and no sources. Confirm:

```
~/.local/bin/adma account list   # "no accounts configured; run: adma account add <alias>"
```

## Phase 2: log in and connect with the binary

You run this. It is interactive: three questions per account, once each.

```
export MCP_SOURCE_LINK_INSECURE=1
adma account add alpha
```

Answer: gateway `https://localhost:14443`, username
`accept-owner-1@example.invalid`, password from `/tmp/accept.pw`.

Then the second account:

```
adma account add beta
```

Answer with `accept-owner-2@example.invalid` and `/tmp/accept2.pw`.

Success: `account list` shows both, with `*` on the first (auto-selected), and no
key material in the listing:

```
* alpha   https://localhost:14443   accept-owner-1@example.invalid
  beta    https://localhost:14443   accept-owner-2@example.invalid
```

Assistant verification: the raw password is nowhere on disk, and each key file is
`-rw-------`.

```
grep -r "$(cat /tmp/accept.pw)" ~/.config/source-agent && echo LEAK || echo clean
ls -l ~/.config/source-agent/accounts/
```

## Phase 3: create the four origins (2 REST, 2 MCP)

The assistant starts them; you do not need to touch them. Each announces its port.

| Origin | Port | Kind | Endpoints / tools |
|---|---|---|---|
| inventory REST | 18201 | FastAPI, OpenAPI | `GET /inventory`, `GET /inventory/{sku}` |
| orders REST | 18202 | FastAPI, OpenAPI | `GET /orders`, `GET /orders/{order_id}` |
| inventory MCP | 18203 | MCP streamable-http | tools `lookup_stock`, `list_stock` |
| orders MCP | 18204 | MCP streamable-http | tools `lookup_order`, `list_orders` |

Success: the assistant confirms each answers. The two REST services serve
`/openapi.json`; the two MCP services answer `initialize` then `tools/list` on
`/mcp`.

A source has a kind. The REST services are `openapi` sources and need their
`/openapi.json` document. The MCP services are `mcp` sources and need no catalog:
the source points straight at the MCP path and the broker talks MCP to it. So
nothing needs to be written for the MCP pair.

## Phase 4: register the four sources

You run these. The REST sources pass `--kind openapi` with their real OpenAPI
document; the MCP sources pass `--kind mcp` and the MCP path (`/mcp` by default,
so `--path` is only needed for a different path). Two sources go under `alpha`,
two under `beta`, so a later phase can compare them. No command here re-prompts
for a password: each reuses the selected account's stored key.

```
adma source add --account alpha --target 127.0.0.1:18201 --kind openapi --spec /tmp/test/spec-18201.json
adma source add --account alpha --target 127.0.0.1:18203 --kind mcp
adma source add --account beta  --target 127.0.0.1:18202 --kind openapi --spec /tmp/test/spec-18202.json
adma source add --account beta  --target 127.0.0.1:18204 --kind mcp
```

`--kind` defaults to `openapi`, and `--path` defaults to `/mcp`, so an MCP source
on a non-standard path is `--kind mcp --path /some/other/path`.

Success, one line each:

```
Added source alpha.127.0.0.1.18201 (origin 127.0.0.1:18201, worker <uuid>).
Added source alpha.127.0.0.1.18203 (origin 127.0.0.1:18203, worker <uuid>).
Added source beta.127.0.0.1.18202  (origin 127.0.0.1:18202, worker <uuid>).
Added source beta.127.0.0.1.18204  (origin 127.0.0.1:18204, worker <uuid>).
```

Assistant verification: four `OicmMcpSource` rows, two owned by each user, the
two MCP rows carrying `kind=mcp` and `mcp_path=/mcp`, and no duplicate. Also
re-run one registration verbatim to show it is idempotent:

```
adma source add --account alpha --target 127.0.0.1:18201 --kind openapi --spec /tmp/test/spec-18201.json
# MCP source already exists as alpha.127.0.0.1.18201; nothing new was created.
```

## Phase 5: run the agent and confirm it serves every source

The registered sources are inert until the agent runs. Start it so all four
connect:

```
adma connector
```

Success: `serving 4 source(s) across 2 account(s)`, then four tunnel connections.
Leave it running for the next phase (or run it under `systemctl --user` if
installed). If a source cannot reach its origin, only that one logs a failure and
the others keep serving; that independence is the point.

## Phase 6: approve the four sources

Enrollment is not approval. Each new source is `pending_review` and is not
callable until an administrator approves it.

You do this in the UI at `http://localhost:14010/ui/mcp-servers/`, tab **Submitted
MCPs**. Approve all four. Success: each row moves to approved, and the **All
Servers** tab lists four callable tools.

Assistant verification: before approval, a tool call is refused; after, it is
allowed. That is the human gate working.

## Phase 7: call the tools through the dev gateway

You run these with a normal virtual key that has access to the sources' MCP access
groups. Each source gets its own group, `mcp-source-{source_id}`.

List what is callable:

```
curl -sk https://localhost:14443/v1/mcp/server \
  -H "Authorization: Bearer $USER_KEY"
```

Call one REST-derived tool and one MCP-derived tool. The gateway exposes each
source's tools under its server id:

```
# inventory REST (look up a SKU)
curl -sk -X POST https://localhost:14443/mcp-rest/tools/call \
  -H "Authorization: Bearer $USER_KEY" -H "Content-Type: application/json" \
  -d '{"server_id":"<alpha-18201 source_id>","name":"<tool>","arguments":{"sku":"widget"}}'

# inventory MCP (lookup_stock)
curl -sk -X POST https://localhost:14443/mcp-rest/tools/call \
  -H "Authorization: Bearer $USER_KEY" -H "Content-Type: application/json" \
  -d '{"server_id":"<alpha-18203 source_id>","name":"<lookup_stock tool>","arguments":{"sku":"widget"}}'
```

Success: real data comes back (`widget`, qty 42; the order list). A failure here
with the source approved means the connector is not connected for that source, not
that approval failed.

## Phase 8: revoke one source, confirm isolation, restore

Revocation must hit exactly one source. Revoke the alpha inventory REST source at
the control plane and call it again; it must fail, while the other three still
work.

Assistant performs the revoke; you re-run the Phase 7 call for that source and for
one other.

```
# alpha 18201 now refused
# alpha 18203, beta 18202, beta 18204 still return data
```

Then restore (re-register the revoked source; revocation is terminal by design, so
this is a fresh registration under the same derived name) and confirm all four
answer again.

Success: only the revoked source failed, and after restore all four succeed.

## Phase 9: disconnect one account

This is the interesting failure. Stop the agent so `alpha`'s sources drop, or
disconnect only alpha's tunnel. The expected behavior: beta's sources keep working,
alpha's become unavailable and their calls fail with a clear "no eligible
connector" style error, and neither account's stored key is affected. Restarting
the agent reconnects both.

You run the calls; the assistant drives the disconnect.

```
# with alpha disconnected:
# beta 18202 and beta 18204 still return data
# alpha 18201 and alpha 18203 fail, not hang
```

Success: failure is scoped to the disconnected account's sources, the other account
is unaffected, and a restart returns everything to normal.

## Phase 10: cleanup

Stop the agent, revoke or delete the four sources, remove the test state, and
clear the dev rows. The assistant runs the deletion in foreign-key order
(workers, connectors, enrollments, sources, then the MCP registry) and reloads the
gateway so the UI shows an empty list.

## What each phase would catch if it regressed

- Phase 0/2: password exchange and key storage only. A stored password or a
  dropped prompt fails the `grep` and the prompt count.
- Phase 4: the source step reading the account store rather than re-prompting, and
  account-scoped derived names. A global name gives one row or a 409.
- Phase 4 (repeat): idempotency. A client-only check still lets a second writer
  create a row; the gateway test covers that half.
- Phase 5: the agent enumerating all sources from persisted state, not memory.
- Phase 6: approval as a separate human gate.
- Phase 7: end-to-end dispatch to both a REST and an MCP origin.
- Phase 8: revocation fenced per source.
- Phase 9: one account's failure not taking down the other.
