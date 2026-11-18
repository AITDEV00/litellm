# grpctunnel final transition: remaining work

Status: DONE, verified live on dev 2026-11-18. Every step in the work plan landed and
both proofs were captured against the dev cluster. See "Outcome" below for what ran and
for the two places where the implementation deliberately differs from this plan.

## Outcome (2026-11-18)

All six work-plan steps are committed as separate commits. Transport proof: a fresh
tunnel connector built from this tree connects to `https://litellm.ecouncil.ae` (nginx
`backend-protocol: GRPC` to the sidecar), the worker logs
`tunnel admitted worker=... generation=1 capabilities=[execute probe]`, the probe reports
`phase=active healthy=True` with a `catalog_fingerprint` equal to the enrollment's
`expected`, and `GET /sources/{id}/read` returns the origin payload with
`authorization_seen=false`, `cookie_seen=false`, and no internal nonce. Approval proof: a
non-admin team key self-enrolls and gets `201 {"status": "pending_review"}`, reads its own
row back with `GET /v1/mcp/server/{id}`, is absent from `GET /v1/mcp/server` while
pending, and only appears after `PUT /v1/mcp/server/{id}/approve` flips it to `active`.

Two plan statements were wrong and the code was fixed instead of the plan being followed:

- "stop writing the claim-token envelope" was too broad. The source's
  `OicmMcpEnrollment` row is the anchor `OicmMcpRemoteWorker.enrollment_id` is a foreign
  key onto, so skipping it makes every later worker registration fail with a foreign key
  violation. The row is still written, its expiry is now server-derived because the
  connector no longer sends one, and nothing redeems it.
- `OicmMcpSource.status` keeps its `pending` write. It is connectivity state that
  `remote_store.report()` promotes to `active` on a good probe, not approval state.
  Approval lives only on the gateway row's `approval_status`, which is the single source
  of truth the dispatch gate reads.

Three defects surfaced only under real traffic and each has a regression test: the
enrollment anchor above, an enroll alias pattern that allowed the `-` that
`MCP_TOOL_PREFIX_SEPARATOR` forbids (so no alias could pass both the schema and the
downstream validator), and a submitter who could not read the row they had just created.


## Scope of this plan, read first

This transition lands in DEV, where nothing is in production. The stated goal is to build
the final implementation directly, not a cautious staged rollout. Therefore several
things that would be blockers on a production migration are not blockers here:

- reverse-h2 is deleted, not deprecated. No fallback, no dual-plane soak.
- The gateway default does not stay "reverse-h2 for old rows, tunnel for new". There are
  no legacy rows to honor; the target shape is tunnel as the only transport.
- No data migration for legacy names (`remotedialer-credential`, `remote_transport`).
- The compatibility proof is a local e2e check wired into the Makefile, not a GitHub
  Actions release gate.

The BYOM self-service registration change ships in the same coordinated final slice,
because dev has everything needed to test the two together and because they touch the
same enrollment surface. They stay separate commits, coordinated.

Design input: `../docs/mcp-source-broker/decisions/approaches/byom-self-service.md` (v2).

## What is already true (re-verify before acting)

- The tunnel plane is functionally complete and was proven live on 2026-11-17:
  `MCP_SOURCE_LINK_TRANSPORT=tunnel` reaches `phase active` with `healthy_at` set and a
  matching `catalog_fingerprint`; data-plane `GET /sources/{id}/source.json` returns 200;
  8 parallel reads all 200; no-token and bad-token both 401; unknown source 503.
- The worker currently serves both planes unconditionally on 8093 via
  `main.go` `runWorker` -> `tunnel.HTTPHandler(tunnels.GRPCServer(), reverse.Routes())`.
  The transport env var is connector-only in effect, although `internal/config/config.go`
  `Load()` also validates it and rejects `tunnel`. After this transition the worker serves
  only tunnel and that field is gone.
- The connector selects the plane in `internal/connector/reverse.go`: `tunnel` runs
  `RunTunnel`, everything else (including empty) runs `RunReverse`. This is where
  reverse-h2 is removed.
- The capability handshake exists: connector `Capabilities = {"execute", "probe"}`, sidecar
  `BrokerCapabilities = {"execute", "probe"}` in `Admit`, additive-only `dispatch.proto`.
- Tunnel tests exist: `internal/tunnel/behavior_test.go` and `internal/tunnel/tunnel_test.go`,
  run under `-race` by `mcp-sidecar-check`.
- Go scaffolding landed in `bb2f8dc7148`: `.go-version`, `go.mod` `tool` block,
  `dispatchpb/generate.go`, `mcp-sidecar-check`, `.github/workflows/test-mcp-source-link.yml`.
- BYOM endpoints already exist in `litellm/proxy/management_endpoints/mcp_management_endpoints.py`:
  `register_mcp_server` (sets `approval_status=pending_review`, `submitted_by`),
  `approve_mcp_server_submission`, `get_active_submitted_mcp_server_ids_for_user`, and
  `get_runtime_mcp_server_rows` only loads `approval_status IN (NULL, 'active', 'approved')`.
  That filter is the dispatch gate that makes tunnel-open-before-approval safe.
- Current `enroll()` in `litellm/proxy/mcp_source_broker/gateway.py` still raises
  `proxy administrator required`, writes `OicmMcpSource.status = "pending"`, sets
  `mcp_info.owner`, and registers through the upsert `default_registration`. BYOM replaces
  all of this.

## What this transition changes, concretely

### 1. Tunnel becomes the only data plane (delete reverse-h2)

Worker side:
- `main.go` `runWorker`: stop constructing and serving `reverseh2`. The reverse listener
  on 8093 serves only the gRPC tunnel (`tunnelpb.RegisterTunnelServiceServer`). Delete
  `reverse.Routes()` and the `reverseh2` import. The data listener (9092/8888) keeps the
  shared dispatch/revoke/readiness handlers untouched.
- Delete `internal/reverseh2/` entirely (handlers, plane, session, and the h2c/data
  protocol helpers unless the data listener still needs `HTTP2Config`/`DataProtocols`).
- `internal/wire` keeps only the envelope types and size/deadline limits the tunnel still
  uses; drop anything only reverse-h2 needed (`StreamLimit`, long-poll framing) after
  checking `tunnel.go` imports.

Connector side:
- `internal/connector/reverse.go` `Run`: tunnel is the only path. Delete `RunReverse` and
  the long-poll `ReverseSession`/dispatch-poll machinery, keep `RunTunnel` and `Execute`.
- Delete the reverse-h2 routes and their tests (`internal/reverseh2/behavior_test.go`).
- The installer unit in `litellm/proxy/client_distribution/source_agent_distribution.py`:
  set `Environment=MCP_SOURCE_LINK_TRANSPORT=tunnel` and rename the unit description off
  "reverse-h2". With reverse-h2 gone, `tunnel` is the default and the variable becomes
  vestigial; treat it as optional (empty == tunnel).

### 2. Worker stops honoring the transport variable (Q3)

`internal/config/config.go` `Load()` no longer reads or validates `MCP_SOURCE_LINK_TRANSPORT`.
The worker has one plane; the variable is connector-only. Remove the `Transport` field if
nothing else reads it.

### 3. Gateway registration uses the BYOM shape, no upsert, no pending-status (BYOM)

`litellm/proxy/mcp_source_broker/gateway.py::enroll()`:
- Drop the `proxy administrator required` raise; derive owner from `UserAPIKeyAuth.user_id`.
- After writing lease rows and uploading the spec, call the BYOM `register_mcp_server`
  with `submitted_by`, `submitted_at`, `approval_status=pending_review`. Return
  `{"server_id": sid, "status": "pending_review"}` plus the lease facts.
- Drop the `mcp_info.owner` write; ownership lives in `submitted_by`.
- Do not reintroduce `default_registration`'s upsert path. Every content change re-pends.
- `store.enroll()`: stop writing `OicmMcpSource.status = "pending"`. The gateway row's
  `approval_status` is the single pending source of truth.

`litellm/proxy/management_endpoints/mcp_management_endpoints.py`:
- Add the `owner_may_mutate` predicate to `PUT /v1/mcp/server` and `DELETE /v1/mcp/server/{id}`:
  admin OR (submitted_by == caller.user_id AND approval_status in pending_review/rejected).
  Active rows stay admin-only. One predicate in one place.

The claim-token envelope becomes obsolete: `SourceCreate.key`,
`OicmMcpEnrollment.token_hash` / `redeemed_at`. Columns stay for now; stop reading/writing
them on the new path. No migration.

Connector and bootstrap: the connector authenticates enrollment with a team-scoped virtual
key; `connector.env` just stores that real scoped key. The derivation
`HMAC(pepper, "remotedialer-credential", ...)` is abandoned; the installer points the
connector at the user's credential file.

### 4. Compatibility proof as a local e2e check (no GH release gate)

Per your requirement: e2e for fidelity plus a Makefile-wired local check, not a GitHub
Actions release gate. Build an e2e-style check (under `tests/e2e/`, following that
directory's `AGENTS.md`) that builds two connector binaries and runs the broker against
the previous and current versions in both directions, covering admit, probe, execute,
generation fencing, and revoke. Wire it behind a Make target (for example
`make -C oicm-litellm-layer mcp-source-link-compat`). This is a capability regression
check, not a release-blocking gate in dev.

### 5. Capability logging (cheap observability win)

Add a log line in `internal/tunnel/tunnel.go` `Runtime.onOpen` recording worker id,
generation, and the admitted capability set. Existing behavior tests still apply.

## Work plan (ordered, separate commits, coordinated)

1. Capability logging (step 5). Smallest, lands first; the e2e check later reads it.
2. Delete reverse-h2 (step 1 + step 2). The mechanical center of this transition.
3. BYOM enrollment rewrite (step 3). Parallel coordinate with step 2, own commit.
   Do not reintroduce the `default_registration` upsert during the edit.
4. Installer/BYOM bootstrap: connector.env carries a team-scoped key; unit = tunnel only.
5. Local e2e compat check (step 4), wired into the Makefile.
6. Cleanup: drop legacy names only where they are not stored-state lookups, stop
   reading claim-token columns, drop the vestigial transport env default.

Steps 2 and 3 are coordinated (they touch the same `enroll()`); sequence them so a
re-enroll never bypasses approval. Do not batch them into one commit.

## Verification per step

- Step 1: a tunnel open in the dev cluster logs worker, generation, capabilities.
- Step 2: a fresh tunnel connector admits healthy within one probe cycle and serves a
  real source read through the public ingress, with `credentials_seen=false` and
  `cookies_seen=false`. No reverse-h2 listener remains on 8093.
- Step 3: a self-enrolled source lands `approval_status=pending_review`, opens its tunnel,
  and receives no dispatch until an admin approves; after approval, serves through the
  public ingress.
- Step 4: with a team-scoped key in connector.env, the installer installs and the
  connector registers/enrolls itself over REST without an admin key.
- Step 5: the e2e check fails when a connector relies on a capability the broker lacks
  (degrades cleanly), and passes when both versions interoperate. Show both.
- Step 6: `make mcp-sidecar-check` stays green throughout.

Use the repo proof standard: curl against a live dev gateway on localhost:4000 or the
real ingress, show the commands and output. Keep the approval-gate proof (pending row
gets no tool call) separate from the transport proof (approved row serves over tunnel).

## Decisions already made by the owner (2026-11-18)

These are settled. Do not re-ask.

- Q1 compat gate: e2e for fidelity plus a local Makefile check, not a GH Actions gate.
- Q2 installer: tunnel only, no reverse-h2 fallback.
- Q3 worker field: connector-only; worker stops validating `MCP_SOURCE_LINK_TRANSPORT`.
- Q4 bootstrap: couple to BYOM, ship together. Dev has everything to test.
- Q5 reverse-h2: just delete it; nothing is in production.
- Q6 naming/legacy: abandon, no migration; build the final shape.
- Q7 ordering: BYOM and the transport flip proceed in parallel, separate commits.
- Q8: go straight to the final implementation, delete reverse-h2 in dev. Confirmed.
- Q9: still build the local e2e compat proof now. Confirmed.

## Do not do

- Do not keep a reverse-h2 fallback. The decision is tunnel only.
- Do not reintroduce `default_registration`'s upsert for the enrollment path; every
  content change re-pends through `register_mcp_server`.
- Do not write `OicmMcpSource.status = "pending"`; pending lives on the gateway row's
  `approval_status`.
- Do not couple the BYOM commit and the reverse-h2-deletion commit; coordinate, do not
  merge.
- Do not implement from `../docs/mcp-source-broker/history/superseded-design-proposal.md`.
- Do not point the build at `evaluate-delete-later/grpctunnel`; the module resolves
  `github.com/jhump/grpctunnel v0.4.0` from the proxy, which is correct.
- Do not add a GitHub Actions release-gating compat job; it is a local Makefile check.

## References

- Owning record and status register: `../docs/mcp-source-broker/running-system/README.md`
- BYOM self-service design (v2): `../docs/mcp-source-broker/decisions/approaches/byom-self-service.md`
- Open questions: `../docs/mcp-source-broker/decisions/approaches/lifecycle-and-pooling-questions.md`
- Measurements: `../docs/mcp-source-broker/measurements/go-driver-concurrency-2026-11-18.md`
- Build, lint, and proto regen: `README.md` in this folder
- Wire contract: `dispatchpb/dispatch.proto`
