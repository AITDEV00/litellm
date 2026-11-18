# Connector bootstrap: remaining defects and acceptance blockers

Recorded 2026-11-18 after an independent code, tests, installer and dev-routing review of
`eea679f7905` through `37e48036669`. Updated 2026-11-18 to require guided CLI initialization
and upload-managed RustFS artifacts, so iterative installer and binary changes need no image
rebuild. This is a follow-up work order. Do not interpret \"bootstrap is implemented\" as \"the
work here is done\" without closing each item and showing its proof.

Own source has authority about intended behavior:
`../docs/mcp-source-broker/decisions/approaches/connector-owned-enrollment.md`. Its earlier
exclusion of `/login` specifically concerned using a UI cookie as a connector credential, not
the requirement to offer human-friendly CLI authentication.
Runtime/deploy contract:
`../docs/mcp-source-broker/running-system/README.md`.
Neither this document nor tests passing alone is acceptance.

## Review evidence at the time of writing

Passed:

- 29 focused `tests/unit/proxy/mcp_source_broker` tests (later changed test set may differ).
- `make -C oicm-litellm-layer mcp-sidecar-check`.
- `make -C oicm-litellm-layer mcp-source-link-compat` (tunnel-only, with existing identity).
- Basedpyright zero errors over `litellm/proxy/mcp_source_broker`.
- Live evidence records show a fresh successful bootstrap on dev and immediate normal restart
  reuse. They do not exercise failure/resume/reissue or the complete go/no-go acceptance.

What implementation got right:

- No team-membership precondition on `POST /mcp-sources`; ownership derives from the
  authenticated user.
- Worker creation authorizes Proxy Admin or the real stored source owner, not a claimed
  request owner.
- Worker registration creates a real per-worker generation-scoped HMAC, while the gateway
  service-token and broker pepper stay off the source host.
- Connector uses ordinary user-key bearer auth for source/worker REST, not private control
  endpoints. It persists a valid issued identity and reuses it on normal restarts.
- Pending/rejected registration does not become MCP registry approval merely because a
  connector can authenticate/open a tunnel.

## Required fixes

### B1. Resume or recover a partially completed first bootstrap (HIGH)

Affected path:

- `internal/bootstrap/client.go::Enroll/IssueWorker`
- `internal/connector/connector.go::LoadIdentity/enrollOnce`
- `litellm/proxy/mcp_source_broker/gateway.py::enroll`
- `litellm/proxy/mcp_source_broker/store.py::enroll`
- `litellm/proxy/mcp_source_broker/remote_store.py::create_peer`

The operation is not atomic across independent gateway writes:

1. Source row/enrollment mutation is made.
2. Catalog artifact is uploaded.
3. BYOM review registration is created.
4. Worker row is created.
5. Worker identity (plaintext HMAC) is returned.
6. Connector persists identity to disk.

Loss or failure before step 6 can strand the source or the deterministic worker label.
Restart then hits an existing source/BYOM-submission 409, or the same-label worker 409,
and cannot re-derive the lost worker credential without server-side pepper. Increasing
bounded transport backoff does not recover any of these paths; non-transport 409 is fatal.
Writing the identity atomically prevents a truncated file, not loss of an unwritten identity.

Required behavior:

- Pick a deliberate resume/reissue contract for owner-authorized bootstrap. Don't just
  add arbitrary retry loops around existing create-only endpoints.
- Source and worker creation need safe unique identifiers/reconciliation for abandoned
  first bootstrap, without silently overwriting approved state or overwriting an existing
  live connector's generation/session.
- Separate source-origin connectivity state from gateway approval. Reuse/repair must not
  move a source out of review or silently rewrite an active approved gateway row.
- Do not let a duplicate-worker registration be misconstrued as permission to churn
  generation or reuse an active identity.
- If explicit admin-only reconciliation is the product decision, document its exact API
  and operator flow; do not call automatic self-bootstrap sufficient until first-use
  failure recovery has a defined workflow. Automatable only where security permits.

Regression tests, using real store state where possible:

- source enroll succeeds -> worker call times out/errors before any identity persisted;
  restarting the actual connector completes.
- worker response lost before received -> next bootstrap can recover.
- identity persistence fails at first install -> credentials never lost permanently; error
  and retry semantics are specified and tested.
- in-process progress is not falsely preserved by previous tests' memory; each attempt
  starts from the actual stored state so this cannot mask a permanent conflict.

### B2. Shipped guided CLI initialization, replacing all human-authored config (HIGH) (UPDATED)

Affected paths: `litellm/proxy/client_distribution/source_agent_distribution.py`,
`mcp-source-link/main.go`, `mcp-source-link/internal/{config,bootstrap,connector}`,
the gateway CLI-login flow, and their unit tests/operator documentation.

The current implementation's configuration-first workflow is not acceptable installer UX.
The download endpoint serves only a fixed shell script which leaves a binary and a systemd
unit. A new user must still create an owner-only environment file containing a personally
provisioned virtual key, then hand-author a private JSON bootstrap input containing a raw
OpenAPI document. There is no initializer, no CLI prompt, and `main()` only accepts
`worker|connector`. The requirement that the connector owns bootstrap is incomplete if the
human still has to assemble its private configuration for it.

It is not sufficient to document `MCP_SOURCE_LINK_PUBLIC_URL`, `MCP_SOURCE_LINK_USER_KEY`,
or the bootstrap JSON format for manual use. An ordinary user with a fresh host should need
no hand-edited config and no supplied key unless automation explicitly supplies one.

#### Product requirement and chosen approach

Ship the existing simple endpoint installer script plus interactive initialization implemented
in the released binary:

1. The user runs the short `curl .../install/source-agent` download and checksum-verified
   install flow. The final shell action runs the installed binary's `init` on the active
   terminal. Do not invent another parallel installer or a separately maintained Bash
   workflow. A handy shell entry point with no logon secret in the URL is necessary; the
   stateful setup logic should be in Go, rather than spread over a shell script and the
   connector.
2. `source-link init` interactively asks for the gateway HTTPS URL and the details of the
   source being exposed: alias, dataset identifier, concrete numeric-loopback target, OpenAPI
   spec file, and the existing Ed25519 proof public key. The executable reads the chosen spec
   file, validates it before submitting it, and internally produces the private bootstrap
   input. A plain-language prompt such as "path to your service's OpenAPI document" is needed;
   pasting an entire OpenAPI JSON document into a terminal or receiving opaque spec JSON is
   not reasonable. The proof private key stays on the originating service/host; do not ask
   for or upload it as if the connector can conjure an origin signature.
3. Initial authentication interactively prompts for the ordinary gateway user's username and
   password, without echoing the password. Authentication creates/obtains the user's own
   user-scoped API credential through an explicit, documented gateway login contract. The
   source enrollment and worker requests still go through the existing ordinary-user routes.
   The server derives owner from authentication, not a username field or an owner supplied by
   the CLI.
4. The CLI stores only the returned user credential as an owner-only file, and stores the
   issued worker identity separately at mode 0600. Zero the password variable where practical
   after the request, do not pass it as a command argument or environment value, and never log
   or persist the raw password, UI cookie, admin credential, gateway service token, or broker
   pepper. Output must not echo the issued virtual key or worker HMAC.
5. For subsequent authentication methods, use the existing CLI authorization
   (`/sso/cli/start`, verification code/browser page, `/sso/cli/poll/{id}`) rather than create
   a new browser-session/cookie protocol. SSO-capable login must not silently land on an
   unclassified dashboard JWT. CPU-only Go binary can read the terminal portably using pinned
   `golang.org/x/term` (with reproducible-module validation); tests use a fake terminal and
   HTTP gateway, not baked-in password fixtures derived from production state.
6. `source-link connector` with no initialized state and an interactive TTY offers/runs the
   exact same initialization. Noninteractive invocations (especially the systemd unit) must
   fail clearly with `run source-link init` instructions instead of reading stdin forever or
   creating partial auth state. `source-link init` must never overwrite or discard an existing
   valid identity merely because the user runs it again; use the same no-reboot/restart
   semantics and require confirmation before a deliberate reset.
7. Optional env flags may remain for CI/noninteractive/deployment use, with a documented
   precedence (valid persisted identity first, existing config otherwise, prompts only when
   needed on a terminal). They are an override, not another manual installation path an
   ordinary user must learn. Do not bypass owner-only mode, target-loopback, proof or approval
   checks when using them.
8. The installer must keep the verified binary path, use it in the unit `ExecStart`, create
   configuration securely with umask/0600, and only install/enable/start after valid
   initialization. Starting the real connector is the final step. It may show a final
   short path/status summary, not instructions for hand-assembling `connector.env`. Ensure
   reload/restart leaves files intact and resumes with the issued identity.

#### Authentication contract: reuse what exists, do not mislabel UI credentials

There is an existing browser-assisted CLI handshake which transfers an identity and returns
real CLI bearer authentication (`get_cli_jwt_auth_token` in `auth_checks.py`, restricted by
allowed routes). There is also username/password `authenticate_user`, used by `/login`, but
that path returns a UI session cookie and puts its duration-limited, team/dashboard-tagged key
inside its JWT. Verifying this explicitly for credentials is required; do not store that
dashboard cookie or paste it as `MCP_SOURCE_LINK_USER_KEY` and claim the auth requirement
is met.

The preferred implementation is one canonical CLI login protocol (the existing handshake),
with its user-facing sign-in page supporting password and future SSO through the same flow.
For the requested interactive username/password option, either integrate secure username +
password submission with this existing state/user-code handshake so its completion issues
the same user-scoped restricted CLI credential, or implement a narrow dedicated password
for user API credential endpoint. It must be bound to the initiating client (one-time request
state, rate limiting, and short expiration), enforce the current password-reset requirement,
and have a bounded lifetime/refresh/relogin story. Extending OIDC's `client_credentials` model
is not applicable to a human account: use authorization-code/user-code semantics or a
purpose-specific password exchange, not just a Basic Auth shortcut with no sessions or
account-controls.

A short-lived session response alone is not sufficient for a daemon expected to run for days.
Issue an appropriately scoped ordinary user key during login (recommended), or deliver signed
refresh with server-enforced revocation/expiry; explicitly test expired session reauth rather
than leaving a connector silently enrolled until the next gateway restart. The connector's
ordinary credential must never be a team requirement, be synthesized from the gateway
service-token/pepper, or have additional admin powers.

11. Validate the route restrictions on the returned CLI credential for exactly
    `/mcp-sources` and `/mcp-source-link-sources/{source_id}/workers` (and the required
    read/identity introspection, if any). Do not weaken global route exemptions or make
    `/login` a reusable HMAC tunnel identity. Credential scopes distinguish user self-service
    from the private `/v1/source-link/*` control plane.
12. No prompt should print authentication or OpenAPI payloads by default. On incorrect login,
    offer an actionable retry bounded by the same gateway controls; on source metadata
    failure, fix that value and continue without a new login if the credential remains valid.

#### Deliverables and required tests

- Update `main.go` for `init`; add a focused `internal/init` (or similarly narrowly-scoped)
  package to own prompts, terminal detection/pwd entry, state management, spec-file loading
  and gateway authentication. Reuse config file writer/readers and bootstrap client rather
  than fork identity/persistence logic. No new hand-written crypto.
- Update `source_agent_distribution.py` to render an actually working fully-guided download
  script whose final action invokes the initializer and then starts the daemon; don't leave
  gated service start waiting for an environment file no one creates.
- Update the authoritative implementation record with the actual commands and prompts,
  including at least the URL, ordinary username/password, source alias, loopback port and
  OpenAPI path. Installer docs must explicitly explain that the gateway user account is
  initially provisioned by an administrator; no hidden privileged signup inside this CLI.
- Run the real installer against checked-in, checksummed binaries and HTTP/TLS fake gateway
  fixtures: complete CLI-only initialization with no pre-existing config; invalid/then-valid
  password; password marker never printed/stored; wrong spec/target/proof-input errors; second
  run does not duplicate enrollment or discard identity; no-TTY errors are meaningful.
- Test generated files are mode 0600, program versions/OS arches are supported appropriately,
  and malicious HTTP responses cannot create a service-file execution injection or weaken the
  existing checksum gate. No remote-file interpolations into executable shell.
- An actual shell invocation on a clean HOME with the released artifact succeeds after
  completing prompts, without human creation of env/JSON/identity files or any broker pepper.
  Demonstrate a normal restart and reauthentication/expiry as well as the initial happy path.

### B6. Host all client release artifacts in RustFS, with no image rebuilds (HIGH) (UPDATED)

The scope is the client installer and `source-link` executable. Do not confuse this with the
gRPC broker sidecar, which remains a separate server image release when its own code changes.

**Already implemented, not a gap:**

- `source_agent_distribution.py` looks up `source-agent/manifest.json` on each installer
  request and streams `source-agent/<version>/{source-link,release.json}` from the configured
  RustFS bucket on each asset request. Neither is baked into the gateway image.
- The existing admin release API can publish a new binary version without a gateway rebuild or
  rollout. Dev and prod already use separate buckets: `litellm-client-mirror-dev` and
  `litellm-client-mirror`, respectively, with their store credentials in the Deployment env.
- Existing versions are immutable and reuse returns 409. Decoupled pointers must not become
  replacements-within-the-same-version. Updates use a new directory/version and move the
  active manifest after all referenced objects have been uploaded and validated.

**Still missing, and required:**

1. The install script is currently synthesized by `_render_source_agent_installer` in the
   gateway Python image, despite the assets being in RustFS. Move installer bytes into a
   stored, validated versioned release object. `/install/source-agent` fetches/serves that
   stored installer (or an inert minimal launcher that fetches/verifies it), not a Python
   generated operational script. Keep gateway/forwarded host data shell-safe.
2. Allow the ordinary iteration of changing the shipped installer, rebuilding `source-link`,
   uploading them together or separately as compatible complete versions, and updating the
   manifest, all without a gateway image build/deploy. Any invocation of the shaped script
   and B2 CLI initialization then picks up that current release's bytes.
3. Add a checked-in release-directory publisher command or Make target that accepts local
   installer, binary and release metadata. It validates metadata/checksums/files, uploads via
   RustFS's existing authenticated S3 environment, and updates the active manifest. It should
   not require hand-wrapping a binary in one base64 JSON payload and reading environment
   secrets itself does not authorize logs/output to expose them. Provide explicit dev/prod
   selection and safe dry-run; an omitted destination must not silently imply prod.
4. Publish only under `source-agent/<new-version>/`, restricting object names and sizes.
   Upload/verify a complete immutable release first, activate it last, and preserve
   no-overwrite. Use a server-checked conditional pointer or narrow serialized publish path
   so concurrent publishers cannot lose an update or repoint to bad/incomplete assets. An
   interrupted upload must leave the previously active release working. Rollback points the
   manifest to an existing complete old version instead of editing any binary.
5. No Docker/gateway/sidecar rollout is part of this tool. Its command, dry run and proof do
   not substitute client-asset deployment for updating a server's sidecar code. Separate
   protocol-relevant client and sidecar versions only through tested compatibility metadata.
6. Record actual update and rollback commands and artifact layout in the owning source-agent
   docs. `HEAD`-or-fallback on an inert gateway route may be useful for diagnostics, but is
   not a general folder write surface. Admin/S3 authentication must remain required. Never
   accept unrestricted public writes, include credentials in download URLs, or use an
   unprotected generic path upload route to implement convenience.

**Proof and tests:**

- On dev, publish a fresh release with the new command, without a gateway image build or
  pod roll. Show unchanged gateway image/pod identity, then a fresh download's installer,
  `release.json` and new binary matching the release's SHA-256.
- Separately prove modifying only the stored installer changes the actual served installer
  without rebuilding the gateway or falling back to Python-rendered fallback content.
- Wrong checksum, incomplete release, unauthorized writer, file-size/path violation and
  concurrent-pointer races cannot change the active successful release. A failed or
  cancelled upload cannot expose half-artifacts as current. Restoring an old pointer works.
- The real B2 installer completed with the artifact-only freshly published build, across
  supported architecture metadata. Run `client_distribution`, Go and relevant gateway tests
  for code/path changes; compatibility checks cover the selected sidecar/server counterpart.

### B3. Administrative revocation and lost/replaced worker recovery (HIGH)

`RemoteStore.lifecycle()` revokes and advances the generation; `create_peer()` refuses
an existing deterministic label; revoked workers intentionally cannot `recover()`.
Deleting a local identity therefore cannot replace a revoked worker through the current
automatic connector flow. Normal startup after revocation correctly fences the old
identity, but cannot return the source host to service.

Required behavior:

- Retain revocation as an admin governance action; do not expose arbitrary revoke/recover
  privileges to source hosts.
- Specify and implement the intentional owner/admin replacement flow: choose fresh
  membership (with nonconflicting/unique-label behavior) or explicit limited reissue,
  not modified `revoke` semantics or an accidental reissue of an active identity.
- If replacement is explicitly an admin-only runbook for now, document the exact
  supported steps, including when removal of local identity is needed and how
  approval/health reacquisition works. Do not call product support complete on a
  mechanism that is merely possible for a developer with pepper.
- A terminal key/generation error should not look like transient networking infinite
  retry, so logs/behavior differentiate operator action required from gateway offline.

Proof: administratively revoke the first connector, repeat using the supported replacement
flow, demonstrate previous generation cannot be admitted, and a newly supported worker
probe/dispatch cycle works. No unrelated production state should be used in this test.

### B4. Reachable, controlled REST and tunnel topology for dev acceptance (HIGH)

A fresh connector defaults to a single `MCP_SOURCE_LINK_PUBLIC_URL` for REST bootstrap
and later tunnel. Current public host pod mapping exposes gRPC
`/grpctunnel.v1.TunnelService/` to dev broker, but ordinary `/mcp-sources`, worker
calls, and installer endpoints default to prod (not the dev gateway). Therefore,
even with the code exactly as committed, that single URL cannot support a full dev
flow. The owning runtime record specifically says the dev gateway has no normal
public ingress; use port-forward for dev gateway REST proof.

Required behavior:

- Do not add a broad route on the prod-shared hostname that accidentally exposes dev
  enrollment/install APIs through normal prod traffic. Do not change production to
  enable dev feature flags/workloads.
- Use a deliberate isolated dev host/route topology for connector development: either
  a dedicated dev endpoint serving exactly the required dev gateway bootstrap REST
  and dev broker tunnel, or explicitly different verified HTTPS endpoints (with the
  current URL limitations documented obviously). The gRPC path transports HTTP/2+h2c
  to the sidecar; plain HTTP REST to dev needs a separate correct listener routing.
- Reconcile install/public bootstrap URLs with actual configured dev deployment; a
  simple TLS-terminating reverse proxy does not automatically bridge REST and gRPC.
- Add tests that every endpoint needed by the bootstrap path reaches its intended
  dev backend with approved TLS settings, and that no REST/tunnel call accidentally
  lands on the other environment.
  Also ensure control-only `/v1/source-link/{peers,report,revoke}` is not being
  intentionally published as user-facing REST.

Proof: fresh source connector is configured without developer-only pepper/cluster
access and does registration, worker issuance, and opens the tunnel along the same
intended dev transport path. Then the approved read succeeds end to end.

### B5. Tests must cover real state and failure transitions, not only happy paths (HIGH)

Current Go acceptance test uses an injected gateway that always returns successful
complete responses; it demonstrates ordinary success/reuse only. Python worker tests
replace `RemoteStore` with a functional `IssueRemote` substitute and source rows created
artificially, so real ID/generation/SQL uniqueness exposure is elsewhere rather than in
the worker authorization path. No test proves restart after partial bootstrap,
revocation replacement, or API ingress reachability.

Required tests:

- Real `RemoteStore.create_peer` issuance + adapter digest/generation agreement,
  combined router authorization with actual source ownership. Existing separate
  real-store digest test helpful; do not substitute a deterministic remote fake when
  the claim being tested is identity issuance.
- Gate retry/failure transitions described in B1/B3. Require failure-before-fix or
  meaningful mutant-killing assertions rather than logs/success-only assertions.
- Malformed identity response: validate target shape and hex credential before
  `WriteConnectorIdentity`; `bootstrap.Client.IssueWorker` currently checks only ID,
  source ID, positive generation, and length (64), not hex nor source/target coherence.
  A complete malformed field must not persist an identity rejected on next restart,
  causing a loop of registrations. Test source IDs that disagree across steps and
  non-hex credentials of matching length.
- Match actual enrollment grant semantics: take IDs from response, not untrusted body,
  while ensuring `IssueWorker` operates on the newly enrolled source ID.
- Distinct negative authorization: nonowner / missing owner = no row mutation; Proxy
  Admin preserves the existing workflow. Do not accidentally broaden global route
  exclusions to non-worker lifecycle paths (`/workers/<id>/<action>` remains admin).

## Pre-existing adjacent defect not silently treated as fixed

A re-enroll with changed catalog/target advances broker source state and uploads a new
spec, before create-only BYOM `submit_for_review` may conflict. This can leave an
active/approved gateway row with refreshed source connectivity even though approval is
not re-pended. This is expressly called out as an approval hazard in the owning
requirement. Reconciliation in B1 must not adopt that behavior accidentally. If B1
extends to recovery beyond new sources, lock down and test re-enroll semantics rather
than declaring a fresh-source acceptance proof sufficient. Addressing this wider
cross-state problem requires a deliberate transaction/recovery contract and owner
approval of scope.

## Non-findings from the review (do not waste work on these)

- Worker-label truncation collisions are not proven substantive. Worker uniqueness is
  `(source_id,label)` and `worker_id` also includes source ID. A claim that truncating
  long alias labels collides across unrelated sources has not been demonstrated.
  Label pattern validity and deterministic naming still deserve tests.
- `mcp-source-link-compat` tests existing tunnel identity and old/new broker/connector
  compatibility; it does not validate bootstrap REST API interoperability against
  old gateways. Do not present it as key issuance compatibility proof.
- `OicmMcpSource.status = pending` remains connectivity state promoted by successful
  proof; `approval_status` on the gateway MCP registration is the review state and
  must not be conflated with it.
- The type-default fault `route_source(... = "reverse-h2")` is already fixed to
  `"tunnel"`; basedpyright is clean. Do not reintroduce reverse-h2 to repair bootstrap.

## Go/no-go acceptance required after fixes

Demonstrate independently and with redacted output:

1. A fresh user runs the supplied shell installer, completes `source-link init` with ordinary
   username/password and source prompts, and the issued virtual key persists mode 0600.
   No manually created env/bootstrap JSON, enrollment/worker calls, UI-cookie steering,
   supplied personal API key, key derivation, pepper, or cluster access is needed.
2. First-run process terminates after source and/or worker registration before the
   identity file exists; restart follows a supported recovery route rather than a dead
   orphan/conflicting worker.
3. Normal immediate restart reuses identity, and actual revoked-worker replacement
   follows the documented supported flow with previous generation fenced.
4. New no-team pending/rejected source stays absent from registry/tool dispatch even
   when connector is admitted/healthy.
5. After ADMIN approval, a real scoped MCP tool read succeeds through intended DEV
   transport, origin confirms gateway credentials/cookies are stripped.
6. A complete new installer/script release is uploaded to RustFS using existing admin auth
   and becomes active atomically at a new version without a gateway image build or pod roll.
   A fresh download uses those changed bytes; the unchanged gateway/pod identity is proven.
7. Phase separation in logging: gateway access log names the user for registration
   and bootstrap; tunnel calls are the connector/worker credential; neither displays
   pepper/service-token.

Run `mcp-sidecar-check`, `mcp-source-link-compat`, all broker/client-distribution tests,
basedpyright for touched Python, and meaningful new regression tests. Then update both
`BOOTSTRAP-GAPS.md` and the owning status record to actual proven status; not every
closed bug means the full independent acceptance succeeded. Do not mark SHIPPED on
partial fresh-start evidence.
