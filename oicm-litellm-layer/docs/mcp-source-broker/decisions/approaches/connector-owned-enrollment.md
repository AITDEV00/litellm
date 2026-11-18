# Connector-owned enrollment, recorded 2026-11-18

> **The dev LiteLLM gateway has no ingress. `kubectl port-forward` is how you test it.**
>
> `litellm-proxy-dev` is unreachable from outside the cluster. `litellm.ecouncil.ae` fronts prod
> (`litellm-proxy`) plus the dev broker's gRPC tunnel path, so a dev gateway REST call over that
> host lands on prod. That is why every dev proof runs through a forward:
> `kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000`, re-run after any dev
> rollout because it replaces the pod, and test against that forward. This is how the acceptance
> run for the connector bootstrap reached dev.

Status: IMPLEMENTED, partially accepted. Both requirements are built and covered by tests. The
user-without-team half and the connector-owns-bootstrap half are proven live against the dev
cluster. The one remaining acceptance step is an approved real read, which runs over the dev
gateway port-forward; see "Running the last acceptance step over a forward" at the end. Nothing
here is marked SHIPPED until it runs

> **Open work required before SHIPPED:** [BOOTSTRAP-GAPS.md](../../../../mcp-source-link/BOOTSTRAP-GAPS.md)
> records recovery, revocation and dev-routing defects. Its B2 supersedes the exclusion of login
> below: the shipped binary must provide interactive `source-link init`, invoked by the supplied
> shell installer, with ordinary username/password creating a restricted user credential. The
> excluded mechanism is persisting a UI cookie as a connector credential, not CLI authentication.
> B6 separates RustFS artifacts from gateway code: binaries and manifests already fetch from
> RustFS, while the installer script must also become an upload-managed artifact. Each update
> uses a new immutable version and moves the active manifest after validating its artifacts

## Why this file exists

A walkthrough of the broker was started on 2026-11-18 against the dev cluster, intending to
demonstrate a source host exposing a private service to the gateway. The walkthrough stalled
because the flow it needed does not exist. Two requirements surfaced that the shipped
implementation does not meet, and both were mis-stated in earlier documentation, including
`running-system/README.md` and `byom-self-service.md`. This file is the owning record for the
correction. Where it disagrees with those files, this file is the requirement and the code is
the current, deficient reality.

## Who the accounts are, and where they come from

There is one administrator account. It is the `admin` account the gateway is deployed with, and it
is the only account that creates other accounts. Every gateway user account that a source is
exposed under is created by that admin account.

The accounts it creates are ordinary user accounts, one per person who will expose a source. They
are not admins, they are not team-scoped, and they need no team membership. Each one is used by
that person's client binary: the connector is configured with that user's own virtual key, and
every gateway call it makes carries that key, so the gateway sees the calls arriving as that user
and the source owner is that user.

So there are exactly two roles here. The admin account creates accounts and approves what users
submit. The created user accounts are what the client binaries authenticate with. The admin
account is never handed to a source host, and a connector must never hold, receive, or be
configured with it.

## Requirement 1: the identity is the user account, not a team-scoped key

A source is exposed by a **person** acting as a gateway user. The connector authenticates with
that person's own account key. Team membership is incidental and is never a precondition for
exposing a source.

What the code does today:

- `litellm/proxy/client_distribution/source_agent_distribution.py` tells the operator to place
  "a team-scoped virtual key" in `connector.env`. That wording is wrong.
- `gateway.py::enroll` rejects a caller with no `team_id`
  (`raise HTTPException(400, "enrollment requires a team-scoped key")`), so a user-scoped key
  belonging to no team cannot enroll at all. Deployed user accounts are not team members, which
  makes this the exact case that breaks. The owner is already
  derived from `user.user_id`, which is the correct half; the `team_id` gate is the defect.
- Install-time configuration and origin signing are prerequisites, not an alternative
  human gateway workflow. `internal/config/config.go` `ReadConnector` requires an
  owner-only JSON identity file with `worker_id`, `source_id`, numeric-loopback
  `target`, `generation`, and a 64-character hex `credential`. The connector does not
  generate that identity. `RemotePeer` additionally requires `label`, `dataset_id`, a
  loopback `target`, and an Ed25519 `proof_key`; the origin must sign the catalog probe
  with the corresponding private key. The binary cannot infer a dataset identity or
  make an existing arbitrary origin produce its signature automatically (see unresolved
  decisions below).
- `route_checks.py` comments its `/mcp-sources` exemption with "the team-scope requirement is
  enforced in the handler". There is no such requirement, and the comment teaches the next
  reader something false.

What is required:

- Enrollment is accepted for any authenticated user account. The owner is `user.user_id`.
  Remove the `team_id` gate; do not replace it with a team check.
- Documentation, installer text, code comments and error messages must say "user account" and
  "the user's own key". A reader must never be told to create or use a team-scoped key.

`/login` is not the mechanism here. It exists to mint a UI session JWT for the dashboard and
returns a cookie, not an API credential, so it is unusable by a connector. The user's account
key is the credential the binary sends.

## Requirement 2: the connector owns the gateway paths, not the human

The connector binary is the only supported client of the broker's registration surface. Every
path that registers, enrolls, or wires a source belongs to the binary. A human invoking those
paths directly is the admin case only.

What the code does today:

- `mcp-source-link/main.go` accepts exactly two arguments, `worker` and `connector`
  (`usage: mcp-source-link worker|connector`). There is no register, enroll, or attach
  subcommand.
- The connector dials the tunnel and nothing else. It makes no call to any gateway REST route;
  its only outbound requests are to the private origin (`internal/connector/tunnel.go`).
- Consequently every step that today precedes a working tunnel is performed by hand:
  `POST /mcp-sources` to enroll, `POST /mcp-source-link-sources/{id}/workers` to register a
  worker, and then deriving `MCP_SOURCE_LINK_CONNECTOR_CONFIG` by computing
  `HMAC(pepper, "remotedialer-credential", enrollment_id, worker_id, generation)` from the
  broker-side pepper. That derivation is impossible for anyone without the pepper, so in
  practice the identity is minted by an operator with cluster access.

What is required:

- The binary performs the registration and enrollment calls itself, on behalf of the user whose
  key it was configured with. A user on a source host runs the connector, and the connector
  talks to the gateway; the user does not curl gateway endpoints.
- The binary obtains its own connector identity. A human must never need the broker pepper, and
  must never hand-assemble `connector.env` or the connector identity file.
- The gateway registration routes stop being a human-facing surface. Direct invocation is an
  admin action, and should be authorised as one rather than being reachable by any user with a
  key. This is a product-UX policy, not a claim that HTTP can identify the client process;
  actual non-admin connector requests are explicitly owner-authorized under D1 below.

### Correction: HTTP cannot identify a process

That last sentence is not implementable as written. The gateway sees a bearer key and an
HTTP request, not whether the caller is this binary or a person using curl with the same
key. The existing worker, selection and lifecycle routes in `remote_gateway.py` already
require a Proxy Admin. Any ordinary-user connector authorization must name **which**
requests the same user key is allowed to make (owner/source/action), not rely on a client
User-Agent header, a private route suffix, an additional client secret, or a claimed
process identity. Those are not authentication.

The owning authorization rule is therefore:

- Direct source enrollment and worker bootstrap on existing routes may be
  Proxy-Admin-only. A Proxy Admin may enroll for a user.
- The user's key permits only requests on sources owned by that user
  (`OicmMcpSource.tenant == user.user_id`), and only on resources tied to that source.
  Worker registration cannot rely solely on the generic pending-row edit permission.
- The connector and a person using its user key pass exactly the same authorization
  predicate. "A user may self-enroll but curl must be admin-only" is excluded.
- Selection and lifecycle actions on other users' sources stay admin-only. Revocation is
  governance, not a substitute for self-renewal.

See unresolved decisions below for whether the same existing worker route is opened to
the source owner, under this predicate, rather than adding an alternate route.

The two requirements fit together like this. The admin account creates the user account once, and
that account's virtual key reaches the source host as the connector's configuration, which is the
one thing an operator does hand over. Everything after
that is the connector's job: it enrolls with the user's key, registers its worker, obtains its
connector identity from the gateway rather than from the pepper, and dials the tunnel. The user
never calls a route, never sees the pepper, and never learns a second credential.

Explicitly not a requirement: adding a new bespoke registration protocol. The existing gateway
routes are the right mechanism. What changes is who calls them, and under whose credential.

## What this supersedes

These statements, written earlier the same day, were wrong. The documentation ones are already
corrected in place as of this record; they are listed so a reader of an older revision knows why:

- `byom-self-service.md`: "the connector authenticates enrollment with a team-scoped virtual
  key" and the claim that the bootstrap question is closed by that. Corrected.
- `running-system/README.md`: the Status register row for bootstrap credential exchange marked
  DONE, and the paragraph describing the team-scoped key as the answer. Corrected, and the
  enrollment section now names the admin-created user account as the identity.
- `lifecycle-and-pooling-questions.md`: §2 declared closed on a team-scoped key. Corrected to
  partly answered and still open.
- `source_agent_distribution.py` module docstring, which stated the team-scoped key shape.
  Corrected; it now says the single admin account creates the user accounts the binaries use.
- `route_checks.py`, whose `/mcp-sources` comment named a team-scope requirement that does not
  and must not exist. Corrected.

The code defects those statements described are not corrected and are what this file is for: the
`team_id` gate in `enroll()`, the missing connector registration subcommand, and the pepper-only
path to a connector identity.

The flow sketched in `byom-self-service.md` (step 2, "connector calls `POST /mcp-sources` with
`Authorization: Bearer <team-key>`") was directionally right about the connector making the
call and wrong about the credential being team-scoped. Requirement 2 keeps the call site and
changes the credential.

## What remains true and is not in scope here

## Scope: exactly two requirements, nothing else moves

These are the only two things discovered in the 2026-11-18 walkthrough that need building. Who
creates the account, and who makes the registration call. Everything else in the flow stays
exactly as it is today, and in particular the approval gate does not change.

**The gated approval of newly registered MCP entries stays the same.** A source that the
connector enrolls still lands in `pending_review` and still serves nothing until an
administrator approves it. The connector enrolls under the user's key and opens its tunnel, and
the tunnel opening does not grant access: pending and rejected rows are structurally absent from
the MCP registry, so no session exists against them and no dispatch can reach the source.
Approval, guardrail editing, and rejection remain the gateway administrator's actions through
`GET /v1/mcp/server/submissions`, `PUT /v1/mcp/server/{id}/approve`, and the existing admin
surface. An approval-free path is not wanted and must not be introduced as a side effect of
either requirement above.

The same holds for the rest of the surface: the loopback-only target, the catalog-fingerprint
probe, generation fencing, revocation, and header stripping are implemented, were verified live,
and are not reopened. Requirement 2 changes which process calls the registration routes and under
whose credential. It does not change what those routes decide, what they return, or what review
they submit to.

The two FastAPI services and two MCP servers stood up on the source host for the acceptance run
are fixtures, not part of the product. Only the OpenAPI-shaped services can be carried by the
broker, because `canonical_spec` requires an `openapi: 3.x` document with `paths`; a native MCP
server has no such document and is rejected. That asymmetry is expected and unrelated to these
requirements.

## Acceptance for these requirements

A user account the admin created, on a host the cluster cannot reach and with no cluster access
and no team membership, has its own key present in an owner-only installer/runtime
configuration file. The binary reads it; a human never derives a connector credential, never
manually assembles a worker identity, and does not need the broker pepper or a cluster
credential. `connector.env` remains a **file the installer owns**, not a forbidden persistence
location: the intended prohibition is hand-authoring identity/HMAC fields. The user's long-lived
key is distinct from the per-worker HMAC tunnel identity and the gateway-to-sidecar service
token. The latter two must never confuse the installer docs or leakage boundary.

The connector then enrolls and registers its worker over existing gateway routes, persists the
issued mode-0600 identity across restarts, and redials after a broker restart without a new
source or manual renewal. Its logs must authenticate as the issuing user, not an admin. While
its source is pending/rejected, its tunnel being open gives it no tool access. After approval a
real read succeeds with gateway credentials/cookies stripped. The same direct REST call under
the same user's key undergoes the same owner/source authz check (no process-identity fiction).

The same run must also show the approval gate unchanged: after the connector enrolls and its
tunnel is up, the source is still absent from the MCP registry and a tool call against it still
fails, and only an administrator approving it makes the read succeed. A run that serves before
approval has regressed the gate and does not count as passing.

Unless the decisions below say otherwise, the minimum implementation contract is as
follows. It changes **who calls existing routes and what those routes return**, rather
than inventing a second registration protocol.

1. Accept an authenticated user's key for `POST /mcp-sources` regardless of `team_id`.
   The server determines `owner`; the source request still has the existing strict
   alias, loopback target and OpenAPI catalog contract.
2. Remove **the broker-specific** team-scope check only. Do not silently weaken
   authorization on the shared BYOM `/v1/mcp/server` surface,
   which has its own account and key checks.
   User-id lookup and worker control are for the binary, not interactive operator
   endpoints. It must not need the owner ID from the user (an opaque key does not expose
   it). `POST /mcp-sources` returns the authenticated `owner` together with the
   deterministic server-side `source_id` already returned today. The connector must not
   compute ownership by decoding an opaque credential or trust a caller-provided owner.
3. The existing service path performs two separate steps:
   - `POST /mcp-sources` registers the source with the review system and returns an
     `owner` determined by the gateway, not the caller. Its `source_id` remains
     `uuid5(NAMESPACE, "source:{owner}:{alias}")`, already computed server-side.
   - `POST /mcp-source-link-sources/{source_id}/workers` creates an identity record for
     one connector instance. Full proof input still comes from the source host; this
     step does not create the user or grant approval.3a. Authorize `OicmMcpSource.tenant == user.user_id` for a non-admin worker
    creation (or Proxy Admin for admin workflows). The response must include the
    connector runtime identity: `worker_id`, `source_id`, target, generation, and
    the plaintext HMAC from
    `Keys.remote_credential(enrollment_id,worker_id,generation)`. Compute
    `enrollment_id = Keys.enrollment_id(source_id)` on the gateway. No pepper,
    gateway-to-sidecar service token, `token_hash`, gateway auth ciphertext or
    unrelated peer state leaves the gateway. `RemotePeerResponse` today lacks the
    credential, target and generation and therefore cannot bootstrap the binary.

4. Add the connector registration flow and execute it before tunnel dialing. It must
   use HTTP(S) REST only: a user enrollment and worker credential issuance, not
   `MCP_SOURCE_BROKER_SERVICE_TOKEN` or a private control endpoint. Use
   `MCP_SOURCE_LINK_PUBLIC_URL` as the fixed gateway base, allow HTTPS plus the
   existing CA override, reject other schemes, and join paths without introducing
   `//`. The existing public base is HTTPS and the tunnel's `DialTarget` is gRPC's
   different endpoint contract; do not reuse it for REST. Atomic install writing of
   an owner-only identity is required for persistence.
   The `--config` installer flag/environment should accept an ordinary user-key
   configuration, not make a private HMAC grant a manual precondition.
5. The long-running `connector` command bootstraps lazily on absent identity. An admin/user
   is not expected to invoke a separate registration command before starting it, and
   the installer need not teach such a command. Maintain the implementation internally as
   one registration step, loaded first only when persisted identity needs creating.
   If a diagnostics/admin `register` is added it must be a thin wrapper on the same path,
   not a separate uncompared security behavior.
6. Requests and one-time worker identity responses must not be logged. Do not log HTTP
   Authorization, the user key, or the plaintext connector credential. A response that
   issues identity is carried over TLS and consumed by this registered device only.
   Restricting subsequent API reads other than the connector's own worker results
   remains necessary.
7. Persist enough registration input to make restart and reconnect unambiguous, while
   keeping the source alias/owner immutable. Existing worker registration is
   deterministic per `source_id, label`. A repeat call currently 409s. Reuse of an
   existing label cannot be silently transformed into re-minting the same active
   identity, because that destroys duplicate-session fencing. Re-enrollment requires a
   deliberate explicit-generation/reissue path or a fresh label. Boot daemon restart
   must reuse the saved identity, not create another worker. This requirement does
   not expose worker revocation to a source host.
8. Do not blur `MCP_SOURCE_BROKER_SERVICE_TOKEN` with the connector credential. The
   service token is for the internal sidecar/control app (`Authorization: Bearer`),
   whereas the connector presents raw per-worker credential metadata over the
   Issues per-worker credentials only when source and worker state is finalized. Do not
   or report endpoints. `Keys.enrollment_id(source_id)` is server-only computation.

## Approval and re-enrollment hazards, not new scope

These are not additional product requirements. The two above requirements are to be
implemented minimally without accidentally breaking review isolation.

Real code mutates the broker source (and uploads a new artifact) before the existing
create-only `submit_for_review` may 409 for an already-known identifier. `OicmMcpSource`
connectivity could therefore become active while the known gateway row is still
approved. Do not self-re-enroll into that behavior while adding the client flow. If implementation touches re-enrollment, choose whether to prevent this
conflict now or record a separate issue. Do not silently claim every content change
re-pends when that proof has not been run. A new-source acceptance journey alone does
not verify it.

`owner_may_mutate` permits owner deletion of a pending/rejected gateway row, but does
not delete broker lease rows or revoke an existing worker. This bootstrap change is
not license to implement cleanup, owner-cancellation of approved live connectivity,
or a new recovery UI.

Targeted negative tests:
- User identity with `team_id=None` can self-enroll and bootstrap. An explicit `owner`
  field in the POST body cannot redirect ownership away from the authenticated user.
  No response contains pepper, the gateway-to-sidecar service token, or secret admin
  state.
- A user cannot create or mutate a worker on an unowned source, access arbitrary source
  IDs, rename identity through the registration response, register a non-loopback target,
  inject invalid proof catalogs, or revoke arbitrary workers.
- Connector startup without a hand-assembled static identity now succeeds through the
  real API (in-process fake TLS gateway); persisted identity survives restart.
- Pending/rejected source has a healthy, admitted tunnel yet gets no registry/dispatch
  session; approval alone opens this already-connected data path.
- The compat `mcp-source-link-compat` check remains green and continues pinning tunnel
  semantics across broker/connector versions.
- Users without a team can still only modify their own submissions under the existing
  pending/rejected-only owner predicate. Active rows stay admin-only, and approval
  stays with the administrator.

## Decisions confirmed after clarifying terminology

These are straightforward semantics of the owner model; they do not authorize new
registration protocols or repositories. Use them instead of inventing a process-identity
check or a new signer.

- **D1, user authorization:** The ordinary user key authorizes self-enrollment and
  worker bootstrap, but only when `OicmMcpSource.tenant == user.user_id`. There is no
  route that tests whether a client is a binary instead of a person. Such a test would
  falsely claim to identify a process from identical HTTP requests. "Direct calls are
  admin actions" is equivalent to meaningful role/source authorization here, not
  rejecting the user's own key for that user's enrollment.
- **D2, origin proof:** Existing acceptance fixtures already supply the source-origin
  Ed25519 proof key and signing response. On enrollment the connector gets/passes the
  public half, and the source origin independently holds the corresponding private key.
  No automatic arbitrary key generation or local signing proxy is part of these two
  requirements. Document this input as the source host's trusted proof configuration,
  fixture-compatible with the existing signed-catalog contract. If there is no signing
  implementation on an origin yet, that is a separate host prerequisite, not a license to
  skip the catalog proof.
- **D3, route expansion:** Reuse `POST /mcp-source-link-sources/{id}/workers` under a
  Proxy-Admin-or-real-owner predicate, not a second client-specific endpoint. "Worker"
  is the DB identity of one connector instance; it is not the source-host user account or
  a new authorization principal. Registration uses the ordinary user's own key, the
  resulting tunnel identity is a separate per-connector credential, and the source
  ownership is checked by the gateway before creation.
- **D4, single initialization:** `source-link connector` receives the ordinary user's
  key **once** through the installer's locally persisted mode-0600 configuration and
  performs enrollment/worker issuance automatically. It then persists the issued
  connector identity locally and reuses all of that on subsequent starts/reconnects.
  The user does not re-login on every tunnel establishment, does not call `/login`, and
  does not need HTTP dashboard session/password cookies. No normal extra `register`
  command is required.

## Required go/no-go acceptance

The phrase "user never sees the pepper, never writes `connector.env` by hand,
never derives a credential" refers to not crafting the HMAC worker identity, not
an absence of secure credential persistence. The intended state is the
admin-provided user key being read by the connector while it registers and
persists issued identity. No manual HMAC or enrollment-ID calculation is needed.

Acceptance must include proof of key permissions (non-team user's key), issuance (no
human pepper derivation), restart (persisted identity), and approval isolation; also
confirm target/proof/test fixtures do not stand in for product features by manual fixture
administration.

Until that is demonstrated, developer acceptance testing of this broker is blocked.

## Implementation and acceptance record, 2026-11-18

### What was built

Python, `litellm/proxy/mcp_source_broker`:

- `gateway.py::enroll` no longer requires `team_id`. Ownership is still `user.user_id`, never the
  request body. The response is a typed `SourceEnrolled` carrying `source_id`, `owner` and
  `status`, so the connector never needs a pasted owner UUID: a virtual key is opaque.
- `remote_gateway.py` authorizes worker bootstrap as Proxy Admin or the real owner, reading
  `tenant` from the stored source row via `store.source(source_id)`. A non-owner is 403 and the
  store is never reached.
- `remote_store.py::create_peer` returns the complete identity: `worker_id`, `source_id`,
  `target`, `generation`, and the plaintext `credential` from
  `Keys.remote_credential(enrollment_id, worker_id, generation)`. The generation in the INSERT
  and the one in the response now come from a single constant, so the credential can never
  describe a generation that was not stored.
- `route_checks.py` exempts only `.../workers` for the owner path. `selection` and `lifecycle`
  stay Proxy-Admin-only.
- `route_source`'s default transport is `tunnel`, matching `TransportName`.

Go, `oicm-litellm-layer/mcp-source-link`:

- `internal/bootstrap` is a narrow REST client. It enrolls with `POST /mcp-sources` and registers
  with `POST /mcp-source-link-sources/{id}/workers`, both over the HTTPS base with the ordinary
  user's key as bearer. It never sends an owner field, never touches the service token or the
  pepper, and never logs the key or the credential.
- `connector.LoadIdentity` prefers a persisted identity and otherwise bootstraps with bounded
  backoff (500ms doubling to 30s) until the gateway and the user key are available, then persists
  atomically at mode 0600 (`config.WriteConnectorIdentity`: temp file, fsync, rename).
- `WorkerLabel` derives a gateway-valid label from the alias. The label pattern is
  `^[a-z][a-z0-9-]{1,31}$`, which forbids the underscores an alias may contain; a verbatim alias
  was rejected with 422. Aliases that already fit are unchanged.
- `main.go` connector mode bootstraps, then hands off to the unchanged `connector.Run` tunnel loop.

### Credentials stay separate

The user key is only a bearer for user-authorized REST. The per-worker HMAC is the tunnel
credential, sent with member/generation metadata, and the sidecar sees only its digest. The
service token and pepper never reach a source host. An owner-authorized worker response contains
no pepper, service token, `token_hash`, `credential_digest` or gateway ciphertext, and a test
asserts that.

### Proven live on the dev cluster

1. A no-team ordinary account, created by the admin account with the admin credential, enrolled
   its own source: `POST /mcp-sources` with that user's key returned
   `201 {"source_id": ..., "owner": "connector-owner-acc2", "status": "pending_review"}`.
2. The connector, given only that user key plus an owner-only bootstrap input file, enrolled and
   registered itself with no human calling either route and no pepper: identity persisted at
   `-rw-------` with `generation: 1` and a 64-character credential, and the tunnel loop started.
3. Restart reused the persisted identity byte-for-byte, with zero re-registration (no 201, no 409).
4. The pending source stayed absent from `GET /v1/mcp/server`. Approval isolation held: being
   enrolled, with a worker registered, did not put the source in the registry.

### Independent implementation review, 2026-11-19

[REVIEW-2026-11-19.md](../../../../mcp-source-link/REVIEW-2026-11-19.md) supersedes claims
that this work was completed merely because `init` and resume tests pass. All four installer
placeholders are published unreplaced, the service still needs a gateway env value which the
new startup never writes, and a fresh username/password call is blocked by normal API-key
auth before its handler. It also documents unintended duplicate key issuance, source approval
and resume consistency hazards, missing transient retry, and undone operational acceptance.
The removed bounded `LoadIdentity` description above is historical, not current runtime code

### Running the last acceptance step over a forward

Step 5 (an approved real read) and reaching `active`/`healthy` were not run in that
sitting. They are not blocked. REST rides the dev gateway forward
(`kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000`), and the
tunnel path is already served for dev by the public host's
`/grpctunnel.v1.TunnelService/` rule. The forward exists precisely because the dev
gateway has no ingress: `/mcp-sources` over the public host lands on prod, so REST goes
to the forward while the tunnel may use the public host. Running the last step means
restarting that forward and repeating the approved read against it. Nothing here is
marked SHIPPED until it runs.

### Re-enrollment

A first bootstrap is not atomic across the gateway writes, so a failure after the
worker row exists but before the identity is persisted used to strand the install
permanently: the retry collided at `submit_for_review` and again on the worker
label, and never saw the credential it had lost.

Two resume paths now close that, and neither re-mints an active identity:

- `gateway.py::enroll`. A repeat enroll that collides is resumed only when the
  existing row is the same owner's and still `pending_review`
  (`pending_source_resume`). An approved, rejected, or foreign row stays a hard
  409, so nothing is moved out of review and no approved row is rewritten.
- `remote_store.py::resume_peer`, reached from the worker route only after
  `create_peer` declines. It re-derives the credential for the worker already
  stored under that label, at that worker's stored generation, without inserting
  a row, advancing the generation, or changing status. It refuses when the target
  differs or the worker is revoked or draining, because those need operator
  action rather than a retry.

Resuming returns the same `worker_id` and the same `generation`, so duplicate
session fencing is unchanged: there is still exactly one worker for that label,
and a second connector cannot appear under it.

### Lost or revoked identity, and how it is reported

A revoked worker is terminal by design
(`running-system/README.md`, decision 2026-11-17: terminal revoke stays manual).
`resume_peer` refuses it and the connector replacement flow is a fresh label
through the existing admin call, not modified `revoke` semantics.

So an operator does not have to read two different failures as the same one:
`bootstrap.Client` classifies the gateway's refusal. A 401 or 429 stays
`ErrNotYetAvailable` and is retried with backoff, since those are transient or
fixable by writing the right key. Any other 4xx is `ErrTerminalRegistration` and
is not retried, because a conflicting identifier or a revoked worker will never
succeed on its own and must be surfaced as operator action required instead of
looking like a gateway outage.

