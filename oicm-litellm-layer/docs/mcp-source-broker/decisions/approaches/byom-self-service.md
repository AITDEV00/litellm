# BYOM self-service registration with admin governance

Status: DESIGNED, ITERATED 2026-11-18 (v2), and since SHIPPED the same day. The
behavior now belongs to [`../../running-system/README.md`](../../running-system/README.md);
this file keeps the reasoning and records where the code departed from the design.
Where the two disagree the code wins, and the departures are listed under
"Where the implementation departed" below

## Intent

Source hosts enroll their own private MCP servers using an ordinary LiteLLM **user account**, not an
admin key and not a team-scoped virtual key. The user/team distinction was stated wrongly here when this
was first written; see [`connector-owned-enrollment.md`](connector-owned-enrollment.md). The gateway admin keeps full review / edit / guardrail / delete control over anything a user has
added. The connector binary performs enrollment on the user's behalf. The reverse data plane is independent
of approval.

## The two insights that removed most of the complexity

The broker already derives source identity deterministically:

```
source_id = uuid5(NAMESPACE, f"source:{owner}:{alias}")
```

Both connector and gateway can compute that locally from the authenticated user id and the alias. Nothing
about a source id needs to round-trip through a broker response or an admin callback. This single fact is
what makes self-enrollment idiomatic instead of a new protocol.

The second fact, from `mcp_source_broker/store.py`: the broker's tables (`OicmMcpSource`,
`OicmMcpEnrollment`, `OicmMcpRemoteWorker`) and the gateway's `LiteLLM_MCPServerTable` are **different
concerns sharing a key**, not one registration holding two pending states. Splitting their responsibilities
is what collapses the redundant steps below.

## What each store is for now

| Store | Owns | Must not carry |
|---|---|---|
| `LiteLLM_MCPServerTable` | The BYOM record: pending/active via `approval_status`, owner via `submitted_by`, admin controls (`allowed_tools`, `mcp_access_groups`, pinned tools, edit/delete), the gateway-visible endpoint shape (`transport`, `url`, `spec_path`, `auth_type`, `credentials`). Global identifier uniqueness via `create_mcp_server_if_identifier_free`. | Tunnel lease facts (worker credential digest, generation, proof key, health). |
| `OicmMcpSource` + `OicmMcpEnrollment` + `OicmMcpRemoteWorker` | Connectivity: source_id (equal to the MCP row's primary key), owner, alias, catalog fingerprint, loopback target, worker lease (worker_id, generation, proof_key, credential_digest, healthy_at). | Any "pending" / "approval" status; that lives only on the gateway row. |

Consequence: pending is expressed once, on the gateway row's `approval_status`. The
`OicmMcpSource.status` literal that `store.enroll()` writes is not approval state. It is
connectivity state, and `remote_store.report()` promotes it from `pending` to `active` on a
good probe, so it keeps its `pending` write. Lease/revision fields stay.

## Where new MCP entries are entered (the only flow now)

The connector reuses the existing gateway endpoints. No new public registration endpoint exists.

```
0. Gateway user accounts exist already, created from the user and key entries in the kube
   manifests when the gateway was deployed. Each person who exposes a source uses their own such
   account, and that account's own virtual key is the only credential the source host ever needs.
1. The source host has the connector installed and configured with that user account's own key.
2. source-link register --alias reports --spec ./openapi.json
     connector computes owner=user_id, alias, and sid locally (uuid5 above).
     connector calls POST /mcp-sources with {alias, target, spec}, sending the user's key.
     This call is the connector's job, not the human's; see connector-owned-enrollment.md. No
     team membership is required, and the admin key never reaches the source host.
3. The existing gateway route `POST /mcp-sources` is re-purposed: drop the PROXY_ADMIN gate, and inside
   `enroll()`:
     a. Write the broker lease rows idempotently (existing advisory lock + upsert path).
     b. Build the `NewMCPServerRequest` exactly as today, but call the BYOM function
        `register_mcp_server` directly instead of `default_registration`, passing submitted_by,
        submitted_at, approval_status=pending_review through.
   Return {"server_id": sid, "status": "pending_review"} and the lease facts.
4. Admin reviews via GET /v1/mcp/server/submissions, approves via PUT /v1/mcp/server/{id}/approve,
   edits guardrails, or rejects.
5. Connector opens / holds the reverse tunnel immediately. No polling needed; when status is wanted,
   GET /v1/mcp/server/{id} is sufficient. Holding a tunnel is not access: the row is still pending,
   so nothing dispatches to it until step 6.
6. Approval fires the existing BYOM cache invalidation in `approve_mcp_server_submission`; then
   `get_active_submitted_mcp_server_ids_for_user` already grants the creator runtime visibility.
   Nothing else has to fire.
```

`register_mcp_server` was chosen over `default_registration` on purpose: `default_registration` is
update-or-create, so a repeat POST against an existing row would silently PUT the row back into whatever
shape the connector sent and bypass pending review. Routing through the BYOM function preserves that
**every content change re-pends**. That also fixes a correctness bug v1 missed: re-enrolling an already
registered source could otherwise silently reactivate a rejected or superseded row without admin review.

## Approval gates dispatch, never the tunnel

`get_runtime_mcp_server_rows` only loads rows with `approval_status IN (NULL, 'active', 'approved')`, so
pending or rejected rows are structurally absent from the registry. No MCP session can exist against an
inactive row, so the gateway never issues an outbound call into `/sources/{sid}/...` for it. The connector
can open its reverse tunnel and hold it; no tool request reaches the source before approval. Connection,
catalog probing, and MCP authorization evolve independently.

## What this design made inert, and what it turned out not to

Each is inert on the self-service path, but three of them could not simply be deleted,
and discovering that is the useful part of this record:

- `SourceCreate.key` (the "admin-generated at least 16 chars" claim secret): inert. The
  field is now optional and accepted-but-ignored so an older connector that still sends
  it is not rejected; nothing reads it.
- `OicmMcpEnrollment.token_hash` / `redeemed_at`: the columns remain and the row is still
  written, but nothing redeems it. This one was originally listed as "stop writing", which
  was wrong. `OicmMcpRemoteWorker.enrollment_id` is a foreign key onto
  `OicmMcpEnrollment(id)`, so skipping the row makes every later worker registration fail
  with a foreign key violation. The row is the anchor, not a token. Its `expires_at` is now
  server-derived because the connector no longer supplies one.
- `OicmMcpSource.status` = `'pending'`: listed here as "stop writing", also wrong. It is
  connectivity lifecycle that `remote_store.report()` promotes on a good probe, independent
  of approval. It keeps its write; only approval moved.
- `mcp_info.owner` inside the gateway row: dropped as designed. Ownership is the real
  `submitted_by` column.
- `default_registration` for this path: dropped as designed. It still exists for callers
  that want it; the broker stops using it.
- The bootstrap gap in `lifecycle-and-pooling-questions.md` §2: closed by replacing claim
  tokens with an authenticated call, not by deriving `connector.env` out of band.

## Rejected v1 deltas (and why)

v1 listed four "minimal deltas to complete self-service". On iteration, three are just as good served by
existing machinery, and one stays:

1. Owner-edit relaxation on `PUT /v1/mcp/server`. Rejected. Owner self-correction is `DELETE` own pending
   row + re-`register`, which needs **no new code path and no authorization change** beyond the owner-delete
   guard that stays below. An owner-edit relaxation would only widen the auth surface for zero new
   capability.
2. Approve-time sharing hook (`mcp_access_groups` on `/approve`). Deferred. Sharing after approval works
   through existing object-permission endpoints; coupling the grant write into `/approve` is optional UX
   sugar, not a design change.
3. Dashboard submission form. Deferred. The connector path is REST-native; the existing
   `/guardrails/register` UI is the nearest analogue if an SSO user ever needs it.
4. Device-authorization bootstrap. Not needed, and not done. The user's own virtual key already *is*
   the bootstrap credential: the admin creates the account and hands that one key over. What is
   still missing is on the connector side, which neither registers nor obtains its connector
   identity by itself yet; see [`connector-owned-enrollment.md`](connector-owned-enrollment.md).

The one genuinely new authorization predicate is a single helper used by the existing edit and delete
routes:

```
owner_may_mutate(row, caller) =
    caller.is_proxy_admin
    or (row.submitted_by == caller.user_id and row.approval_status in (pending_review, rejected))
```

`PUT /v1/mcp/server` and `DELETE /v1/mcp/server/{id}` accept `owner_may_mutate` as an alternative to the
admin gate. Active rows stay admin-only. One predicate in one place, not a family of special cases.

## What stays unchanged

- `POST /v1/mcp/server/register`, `GET /v1/mcp/server/submissions`, `PUT /v1/mcp/server/{id}/approve` and
  `/reject`: untouched.
- `get_runtime_mcp_server_rows`: untouched, that filter is the load-bearing dispatch gate.
- `get_active_submitted_mcp_server_ids_for_user`: untouched, already creator-grant-aware.
- The sidecar pool (`pool.pick`), reverse-h2 / grpctunnel, `keys.py` credential derivation: untouched.
- The release installer (checksum-verified binary + systemd unit): untouched; it now points the connector
  at the user's credential file rather than a claim token.

## Smallest concrete diff

1. `mcp_source_broker/gateway.py::enroll()` — drop the `PROXY_ADMIN` raise; derive owner from
   `UserAPIKeyAuth.user_id`; after writing lease rows and uploading the spec, call the BYOM submit function
   with `submitted_by`, `submitted_at`, and `approval_status=pending_review`, returning
   `{"server_id": sid, "status": "pending_review"}`. Reuse the existing `NewMCPServerRequest` builder
   unchanged (drop `mcp_info.owner`).
2. `mcp_management_endpoints.py` — add the `owner_may_mutate` predicate to `PUT /v1/mcp/server` and
   `DELETE /v1/mcp/server/{id}`. No change to `/register`, `/approve`, `/reject`, `/submissions`.
3. `SourceStore.enroll()` — keep the `pending` status (connectivity, not approval) and stop
   depending on `SourceCreate.key` for identity: the gateway-auth ciphertext is keyed on
   `owner:alias`.

That's it. No new tables, no new endpoints, no new authorization model.

## Where the implementation departed

Four things the design above did not anticipate, each found by exercising the real
endpoint rather than the unit tests:

- The alias could not be hyphenated. The schema allowed `-` and
  `MCP_TOOL_PREFIX_SEPARATOR` (default `-`) forbids it downstream, because the alias
  becomes the MCP server name that prefixes every tool name. The pattern is now
  `^[a-z][a-z0-9_.]{2,127}$`.
- A non-admin could not reach `POST /mcp-sources` at all: the route-level admin gate in
  `route_checks.py` fired before the handler. `/mcp-sources` is now exempt from that gate
  and does its own authz.
- A submitter could not read back the row enrollment had just handed them.
  `authorize_mcp_server` only knew about admin views, so the enrolling user got 403 on
  `GET /v1/mcp/server/{id}`. It now admits the submitter of a row; everyone else still
  goes through the normal access checks.
- The enrollment anchor row and the `OicmMcpSource.status` write, both covered above.

## Residual items, deliberately out of v2

- Catalog-artifact cleanup on delete: `delete_mcp_server` knows nothing about the RustFS catalog object the
  broker uploaded. A periodic orphan sweep over the `mcp-source-broker/` prefix is cleaner than a
  synchronous delete inside the gateway write path. Implement only if storage pressure shows.
- Multi-source membership proof (Server A and Server B serving the same dataset) stays in
  `multi-replica-scaling.md`. BYOM does not merge them: each owner's source is an independent pending record
  with independent approval.
- Native-MCP-over-tunnel, catalog self-description, `registry.json` advertisement are orthogonal transports
  and concerns, not registration. Do not couple them to this slice.

## Related

- `../../running-system/README.md`: broker behaviour today, including the bootstrap gap this closes by
  replacing claim tokens with an authenticated call.
- `approaches/lifecycle-and-pooling-questions.md` §2: the open question this resolves.
- `approaches/multi-replica-scaling.md`: the independent membership debate.
- `litellm/proxy/mcp_source_broker/gateway.py`: home of `enroll()` and the `NewMCPServerRequest` builder.
- `litellm/proxy/mcp_source_broker/store.py`: the two-table layout this design clarifies.
- `litellm/proxy/management_endpoints/mcp_management_endpoints.py`: the BYOM endpoint family it reuses.
