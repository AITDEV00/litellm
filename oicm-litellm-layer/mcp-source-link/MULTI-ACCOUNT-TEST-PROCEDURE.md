# Multi-account connector: manual test procedure

This is the operator-facing, step-by-step procedure for proving the multi-account
`source-link` client against a live dev gateway. It complements
`MULTI-ACCOUNT-SOURCE-GAPS.md` (the requirements) and the automated tests in
`internal/state`, `internal/setup`, `internal/target` and the Python broker suite.
Those tests prove the units; this procedure proves the wiring end to end through
the real binary.

Run it top to bottom on a clean slate. Each step states what to run, what success
looks like, and what a failure means. Do not skip a step because a later one is
"the interesting one": a step that fails early usually means every step after it
is measuring the wrong thing.

## 0. Prerequisites and what each one is for

You need a dev gateway reachable over TLS, a database, and at least two ordinary
user accounts on that gateway. Everything here is dev-only; nothing in this
procedure touches production.

| Thing | Why | How to check |
|---|---|---|
| Dev proxy port-forward | The connector talks to the gateway over REST | `ps aux \| grep 'port-forward svc/litellm-proxy-dev'` |
| TLS terminator on 14443 | The client refuses plain HTTP; this fronts the forward with a cert | `curl -sk -o /dev/null -w '%{http_code}' https://localhost:14443/health/readiness` returns `200` |
| Postgres pod | Source, worker and MCP registry rows are inspected and cleaned here | `kubectl -n adeo-litellm get pods \| grep postgres-dev` |
| Two user accounts | Proves an equal target under two owners stays two sources | `SELECT user_id, user_email FROM "LiteLLM_UserTable"` |
| Built binary | The thing under test | `make -C oicm-litellm-layer mcp-sidecar-build` |

Two facts that will cost you an hour if you do not know them:

- The password login (`POST /sso/cli/password`) looks the user up by **email**, not
  by `user_id`. Enter the email as the username.
- The public source routes (`POST /mcp-sources`, `POST
  /mcp-source-link-sources/{id}/workers`) are mounted on the **proxy**
  (`litellm-proxy-dev`), not on the broker pod. The broker pod serves only the
  private control plane. If you change those routes in code, you patch the proxy.

Start the port-forward fresh, because rolling the proxy kills it:

```
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14010:4000 &
```

The TLS terminator in this environment forwards `14443` to `14010`. Confirm both:

```
curl -s  -o /dev/null -w 'direct=%{http_code}\n'   http://localhost:14010/health/readiness
curl -sk -o /dev/null -w 'tls=%{http_code}\n'      https://localhost:14443/health/readiness
```

Both must print `200`. If `tls=000`, the terminator lost its upstream; restart the
forward.

## 1. Start from a clean slate

The correctness claims here (idempotency, no duplicate rows, scoped revocation)
are only observable if the tables start empty. Delete the local state and the dev
rows first.

Local state, per test home:

```
rm -rf /tmp/multi-proof && mkdir -p /tmp/multi-proof/home
cp <built-binary> /tmp/multi-proof/source-link
```

Dev rows. The order matters because of foreign keys: workers reference
enrollments, enrollments reference sources.

```
PSQL='kubectl -n adeo-litellm exec adeo-litellm-postgres-dev-1 -- psql -U postgres -d litellm -c'
$PSQL 'DELETE FROM "LiteLLM_MCPUserEnvVars";'
$PSQL 'DELETE FROM "LiteLLM_MCPUserCredentials";'
$PSQL 'DELETE FROM "LiteLLM_MCPServerOAuthClient";'
$PSQL 'DELETE FROM "LiteLLM_MCPToolsetTable";'
$PSQL 'DELETE FROM "LiteLLM_MCPServerTable";'
$PSQL 'DELETE FROM "OicmMcpRemoteWorker";'
$PSQL 'DELETE FROM "OicmMcpEnrollment";'
$PSQL 'DELETE FROM "OicmMcpConnector";'
$PSQL 'DELETE FROM "OicmMcpSource";'
```

Success: every table reports `DELETE n` and the counts below are all `0`.

```
kubectl -n adeo-litellm exec adeo-litellm-postgres-dev-1 -- psql -U postgres -d litellm -t -c \
  'SELECT (SELECT count(*) FROM "LiteLLM_MCPServerTable") AS mcp_rows,
          (SELECT count(*) FROM "OicmMcpSource")         AS sources,
          (SELECT count(*) FROM "OicmMcpRemoteWorker")   AS workers,
          (SELECT count(*) FROM "OicmMcpEnrollment")     AS enrollments;'
```

Also clear anything already known by the live gateway's router, since a deleted DB
row does not unload an in-memory MCP server:

```
curl -sk -X POST https://localhost:14443/health/reload -H "Authorization: Bearer $MASTER_KEY"
```

## 2. Add the first account

The account step is where the old UX was worst: it demanded six fields and an
opaque public key. The new contract is three questions, once per account.

```
export HOME=/tmp/multi-proof/home
export MCP_SOURCE_LINK_INSECURE=1
./source-link account add alpha
```

Answer exactly:

1. `Gateway URL (https://...):` -> `https://localhost:14443`
2. `Username:` -> `accept-owner-1@example.invalid` (the email)
3. `Password (not echoed):` -> the dev password

Success looks like:

```
Added account "alpha" (user accept-owner-1@example.invalid at https://localhost:14443).
```

What to check beyond the message:

- Only three prompts appeared. If you saw a dataset-id, proof-key, or OpenAPI
  prompt here, the binary is stale.
- The raw password is nowhere on disk:
  ```
  grep -r "$(cat /tmp/accept.pw)" /tmp/multi-proof/home && echo LEAK || echo clean
  ```
  must print `clean`.
- The key file is owner-only:
  ```
  ls -l /tmp/multi-proof/home/.config/source-agent/accounts/
  ```
  shows `-rw-------` for `alpha.key`.

Failure modes: a `401` means the username was not the email or the password is
wrong. An `account "alpha" already exists` message means step 1 did not clear the
local home, which is fine, just use a fresh home.

## 3. Add a second account

A second account is a separate owner. This is what later proves that one target
under two owners is two sources, not one.

```
./source-link account add beta
```

Same three prompts, with `accept-owner-2@example.invalid` and its password.

```
./source-link account list
```

Success:

```
* alpha   https://localhost:14443   accept-owner-1@example.invalid
  beta    https://localhost:14443   accept-owner-2@example.invalid
```

The `*` marks the selected account (the first one added becomes selected
automatically). No password or key material appears in this listing.

## 4. Add a source under the selected account

This is the step that must not ask for credentials again. The only new question is
the target (and the spec path).

Prepare an origin OpenAPI document. Any service that serves `/openapi.json` works;
here a local mock does.

```
curl -s http://127.0.0.1:18201/openapi.json -o /tmp/multi-proof/spec-18201.json
./source-link source add --target 127.0.0.1:18201
```

The only prompt is `Path to the origin's OpenAPI document:`, answer
`/tmp/multi-proof/spec-18201.json`.

Success:

```
Added source alpha.127.0.0.1.18201 (origin 127.0.0.1:18201, worker <uuid>).
Restart the agent to serve it.
```

Check:

- No gateway URL, username or password prompt appeared. If one did, the source
  step is not reusing the account store.
- The derived name is `<account>.<host>.<port>` with dots. If it contains a
  hyphen, it will be rejected by the gateway alias grammar; that is a bug, not a
  bad input.

## 5. Add a second source under the same account

Same account, different port. Still no credentials.

```
./source-link source add --target 127.0.0.1:18202
```

Spec: `/tmp/multi-proof/spec-18202.json` (fetch it from `http://127.0.0.1:18202`).

Success: a new source `alpha.127.0.0.1.18202`, and the account store was not
touched.

## 6. Add the same target under the other account

This is the cross-owner case. `beta` registers `18202`, the same host:port `alpha`
just used. It must be a **separate** source.

```
./source-link source add --account beta --target 127.0.0.1:18202
```

Spec: `/tmp/multi-proof/spec-18202.json`.

Success: `Added source beta.127.0.0.1.18202`. The name differs from
`alpha.127.0.0.1.18202`, and both exist. Confirm in the DB that two rows share the
target but differ in tenant:

```
kubectl -n adeo-litellm exec adeo-litellm-postgres-dev-1 -- psql -U postgres -d litellm -t -c \
  "SELECT alias, tenant, target_host, target_port FROM \"OicmMcpSource\" ORDER BY alias;"
```

Failure: a single row, or a 409, means the derived name is not account-scoped.

## 7. Repeat an exact registration (idempotency)

Re-add source 1 verbatim. This must create nothing.

```
./source-link source add --target 127.0.0.1:18201
```

Success:

```
MCP source already exists as alpha.127.0.0.1.18201; nothing new was created.
```

Confirm no new rows were created by comparing counts before and after. The
stronger proof is the gateway-side one, which the automated test
`test_repeat_enroll_creates_no_new_row_upload_or_submission` pins; here, just
confirm the row count did not change:

```
kubectl -n adeo-litellm exec adeo-litellm-postgres-dev-1 -- psql -U postgres -d litellm -t -c \
  'SELECT count(*) FROM "OicmMcpSource";'
```

It still reads 3 (`alpha` x2, `beta` x1). Nothing was submitted for a new review.

## 8. Verify the local state, then restart the agent

The state is files, not memory. Inspect them, then prove the agent reloads them.

```
cat /tmp/multi-proof/home/.config/source-agent/sources.json
./source-link source list
```

`source list` shows all three sources, their account, target and `registered`
status. Then start the agent and confirm it serves every source from every account
in one process:

```
MCP_SOURCE_LINK_TUNNEL_URL=https://localhost:18094 timeout 6 ./source-link connector
```

Success: a line `serving 3 source(s) across 2 account(s)`, followed by per-source
tunnel connect attempts (they may fail to reach the tunnel endpoint, which is fine
for this check; what matters is that all three are attempted independently). Stop
it with Ctrl-C or let the timeout end it. A restart must enumerate the same three
sources with the same worker ids; that proves the identities were persisted, not
re-derived.

## 9. Target handling: what is accepted and what is refused

The target grammar is what keeps a source from becoming a URL or a public
hostname. Accepted:

- IPv4 literal: `10.34.104.99:4000`
- private IPv4: `10.0.0.1:80`
- Kubernetes service short form: `litellm-mcp-broker-dev.adeo-litellm.svc:8888`
- Kubernetes service cluster form: the same with `.cluster.local`, which
  normalizes to the short form

Refused (each must fail before any network call):

- a URL: `http://10.0.0.1:80`
- a bare public hostname: `example.com:443`
- a port out of range: `10.0.0.1:0`, `10.0.0.1:70000`
- an unbracketed IPv6: `fd00::1:4000`

These are pinned by `internal/target/target_test.go`; run that to re-verify
without a gateway:

```
cd oicm-litellm-layer/mcp-source-link && go test ./internal/target/ -v
```

To see a Kubernetes service target accepted live, add one (it enrolls; only the
later tunnel would fail to reach it):

```
./source-link source add --target litellm-mcp-broker-dev.adeo-litellm.svc:8888
```

Success: a name like `alpha.litellm.mcp.broker.dev.adeo.litellm.svc.8888`, with the
hyphens normalized to dots.

## 10. Revocation is scoped to one source

Revocation must mark exactly the named source terminal and leave its siblings
alone. Use one worker id from `source list`, revoke it at the control plane, and
confirm only that source's row changes status.

```
WORKER=<one worker id from source list>
curl -sk -X POST https://localhost:14443/v1/source-link/revoke \
  -H "Authorization: Bearer <service token>" \
  -H "X-Source-Member: $WORKER" -H "X-Source-Id: <its source id>"
```

Then:

```
kubectl -n adeo-litellm exec adeo-litellm-postgres-dev-1 -- psql -U postgres -d litellm -t -c \
  "SELECT alias, status FROM \"OicmMcpSource\" ORDER BY alias;"
```

Success: only the revoked source's status changed; the other two are unchanged.

## 11. Admin approval gate

Enrollment is not approval. A freshly enrolled source is `pending_review` and is
not dispatchable until an administrator approves it in the UI at
`http://localhost:14010/ui/mcp-servers/`. Confirm the row's approval status before
and after approval, and confirm that a tool call before approval is refused.

This is the point of the whole flow: an ordinary user can enroll and run a
connector, but a human still decides what becomes an approved, callable tool.

## 12. Clean up

Leave the environment as you found it, or the next run's idempotency checks are
measuring leftovers.

```
rm -rf /tmp/multi-proof
```

Then repeat the deletion block from step 1, and reload the gateway router.

## What each step would catch if it regressed

- Step 2/3 pass only if the password exchange works and the key is stored, never
  the password. A regression that stores the password, or drops a prompt, fails
  the `grep` and the prompt count.
- Step 4/5 pass only if the source step reads the account store instead of
  re-prompting. A regression that re-prompts fails on the missing answers.
- Step 6 passes only if the derived name is account-scoped. A global name would
  produce one row or a 409.
- Step 7 passes only if idempotency is transactional on the server. A client-only
  check would still let a second writer create a row; the gateway test covers that
  half.
- Step 8 passes only if identities are persisted files, not recomputed.
- Step 9 passes only if the target grammar is enforced before the network.
- Step 10 passes only if revocation is fenced per source.
- Step 11 passes only if approval is a separate, human gate.
