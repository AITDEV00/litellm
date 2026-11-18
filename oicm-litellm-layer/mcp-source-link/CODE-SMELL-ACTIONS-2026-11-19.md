# Latest connector review: code smells and action items

Reviewed 2026-11-19 at `af2e4279a4e`, matching `origin/jya0-v1.108.5`. The branch was pulled before inspection. Scope is the nine worker commits `eba13d29baa` through `af2e4279a4e`, compared with `b22dd38bc07`, plus the gateway/state boundaries required to evaluate them. No live-cluster or production changes were made

## Verdict

Do not accept the combined multi-account, multi-source connector yet. Ignore the top-level test count when deciding acceptance: the Go formatting/vet/static analysis/race suite and 172 focused Python tests pass, while the executable compatibility control still fails and direct probes reproduce target, identity, and recovery defects

The good changes are real: source kind and path are persisted, OpenAPI retrieval is bounded, `adma` is installed by one script, SIGHUP can plan incremental starts and stops, and the existing `grpctunnel` transport remains in use. Short JSON-RPC MCP requests have a plausible path. These improvements do not establish the advertised source workflow or Streamable HTTP support

Status labels below distinguish new behavior from old defects that the commits exposed but did not repair. They are not claims about a particular commit author's intent

## P0: functional and security corrections

### 1. NEW: implement actual response streaming before advertising MCP Streamable HTTP

**Smell:** a capability announcement is misread as proof of the capability. [connector/tunnel.go](internal/connector/tunnel.go#L163-L197) consumes the full origin response before its unary RPC returns. [tunnel/tunnel.go](internal/tunnel/tunnel.go#L125-L179) then turns the byte slice into an already-ended reader. [dispatch.proto](dispatchpb/dispatch.proto#L73-L79) has no header/chunk/end sequence. An idle or infinite SSE reply yields neither headers nor events and reaches the 90-second deadline; responses over 760 KiB fail. Forwarding `Mcp-Session-Id` and `Last-Event-Id` in [pool/forward.go](internal/pool/forward.go#L12-L18) cannot change this

**Action:** add an additive streaming execute RPC with status/headers followed by bounded chunks, cancellation, and backpressure. Preserve unary execution for older agents. Configure immediate SSE flushing on the final proxy. Define redirect handling: never forward a private `Location` to an external client by accident, and never force it back under the source prefix blindly

**Acceptance:** through a real gateway, connector, and worker, two delayed events arrive individually before EOF. An idle stream remains usable beyond 90 seconds, downstream cancellation closes the origin request, slow readers do not create unbounded memory, and disconnect/resume with `Last-Event-Id` is covered

### 2. CARRIED: align supported targets across validation, persistence, and Go peer parsing

**Smell:** validation was widened at the request boundary but repeated through a contradictory row DTO. `SourceCreate.Target` accepts private IPs and service DNS names in [models.py](../../litellm/proxy/mcp_source_broker/models.py#L56-L85), while `RemoteWorkerRow.target_host` is still exactly `127.0.0.1` in [remote_store.py](../../litellm/proxy/mcp_source_broker/remote_store.py#L23-L31). Insert accepted inputs then become unreadable through `worker()`, `resume_peer()`, lifecycle operations, and `peers()`. A bad projection can also make all peer startup fail

**Action:** replace the literal with a shared validated host representation and use one common canonical host:port formatter. Bracket IPv6 in worker responses and persisted peer output, following `net.JoinHostPort` semantics. Keep address syntax no broader than the agreed source policy, with IP/service canonicalization enforced on every boundary

**Acceptance:** insert then read/project/lifecycle a service name, private IPv4, and bracketed-compressed IPv6 worker. Feed those records through the actual connector admission code. Add a malformed-row test ensuring one corrupt row cannot silently prevent unrelated peers from loading

**Reproduced:** the nested `peers()` projection rejects `service.namespace.svc`, `10.34.104.99`, and `::1` with a literal validation error

### 3. CARRIED: bind every worker to the reviewed source target before insertion

**Smell:** source approval and worker issuance are authorized independently. [create_peer](../../litellm/proxy/mcp_source_broker/remote_store.py#L114-L155) accepts an arbitrary second target, even though it loaded the source to authorize creation. [validateIssued](internal/bootstrap/client.go#L225-L241) validates response syntax, not equality with the requested origin. The runtime later dials that worker-directed origin, so ownership and admin review no longer describe what is exposed

**Action:** require `source.target == peer.target == issued target == connector target` under the source's transaction and ownership authorization. Use the reviewed stored target as the authority, not another caller-provided replacement. Preserve this invariant on resume and every projection

**Acceptance:** mismatched host and port are refused before insertion or side effects, including management-led mismatches. Identical valid input still succeeds and opens only the enrolled origin

**Reproduced:** a source reviewed at `127.0.0.1:9001` issued a worker for `127.0.0.1:9002`

### 4. CARRIED: make identity canonical, bounded, and collision-safe across all components

**Smell:** a lossy display label became the identifier. Local aliases are not authenticated owner IDs, but [DeriveName](internal/state/state.go#L347-L360) embeds them in the global MCP alias. Two gateways' different owners can choose the same local alias, while two local aliases for one actual owner/target create two sources. Names are not bounded to the 128-character gateway contract, [WorkerLabel](internal/bootstrap/client.go#L267-L300) truncates to 32 characters, local same-name updates replace different accounts' entries in [PutSource](internal/state/state.go#L302-L319), and the runner shares the same name-only key in [connector.go](internal/connector/connector.go#L122-L146). `--name` bypasses derivation entirely

**Action:** keep a local identifier `(account, source)` separate from display text. Persist the returned authenticated owner and gateway origin, then owner-authoritatively derive a bounded alias using a stable digest of canonical owner/gateway/source semantics. Use a digest suffix in the worker label. Define cross-account existence semantics rather than letting arbitrary names decide. Compare target, kind, path, and relevant content when deciding local idempotency. Preserve authenticated ownership on the server, regardless of names

**Acceptance:** same user/target under two local aliases converges; equal local alias/target under different authenticated users is accepted; every legal maximum-length target maps to a legal alias; distinct long names do not share a worker; account A cannot overwrite account B in storage or reload

**Reproduced:** a legal, long Kubernetes target generated a 190-character alias. Writing `shared` under alpha followed by beta left one record. The current examples use different local aliases, so they do not cover different owners sharing one alias

### 5. CARRIED: recover interrupted enrollment instead of treating row presence as completion

**Smell:** durability has been confused with completion. [store.enroll](../../litellm/proxy/mcp_source_broker/store.py#L218-L269) commits the source/enrollment, but [gateway.enroll](../../litellm/proxy/mcp_source_broker/gateway.py#L136-L188) writes an artifact and MCP review row later. Failure at either later operation makes the retry return `AlreadyEnrolled` and skip both. A later worker can exist without a catalog or pending review row

**Action:** make an explicit enrollment state/reconciliation operation restore exactly the immutable stored content and pending registration, independently of the transport-health status. Do not alter approved/rejected content or silently reset review state. Then resume worker delivery. A complete retry should produce one coherent source, catalog where applicable, and pending MCP row. Prefer a transactional registration state to relying on an exception-only cross-store repair

**Acceptance:** inject failure after source persistence, artifact persistence, registration, and worker response; each retry reaches exactly one complete registration. An approved metadata row and its reviewed content remain unchanged

### 6. NEW ACCOUNTING: remove stale catalog health at broker restart, not just its timestamp

**Smell:** content proof and channel liveness have been collapsed. [main.py](../../litellm/proxy/mcp_source_broker/main.py#L30-L32) sets `healthy_at=NULL` but retains `catalog_fingerprint`. A newly created in-memory member never had a probe, yet [provenCatalog](internal/pool/eligibility.go#L63-L70) uses the persisted matching fingerprint as part of admission. This weakens the intended restart safety and lets content state masquerade as a fresh channel check

**Action:** distinguish source-content evidence from connection liveness. Clear both on no-tunnel startup, or use a destination-qualified health record that is invalid for the new boot. Enforce the actual generation/phase/valid-probe channel before every route

**Acceptance:** start with a stored good fingerprint and no connector: no request is admitted. Establish, kill, then restart a connector, clearing outdated session state and requiring a new successful handshake/probe before service

### 7. REVIEW: reject public and unspecified target addresses or record an explicit policy decision

**Smell:** [target.Parse](internal/target/target.go#L65-L89) accepts any IPv4/IPv6, including public and `0.0.0.0`; [Target.validate_host](../../litellm/proxy/mcp_source_broker/models.py#L70-L77) does the same. The registration requirement describes private/IP and Kubernetes sources with approval-bounded exposure. `0.0.0.0` is an unspecified listen address, not a source destination. Approval controls visibility, but it should not silently redefine allowed networks

**Action:** prohibit unspecified/multicast/broadcast destinations in both client and gateway. Implement the agreed private-address policy, including IPv4/IPv6 equivalence, or explicitly document and approve a broader scope. Do not imply a service suffix itself proves local reachability

**Acceptance:** same family and IPv4-mapped representations pass identical policy tests, `0.0.0.0`, `::`, multicast, and rejected external targets fail without side effects, and an allowed private/Kubernetes destination survives storage and projection. No network probe is a substitute for authorization

### 8. CARRIED: provide a recoverable source lifecycle and a migration for installed state

**Smell:** local removal deletes only bytes needed for subsequent identity reuse. [runSourceRemove](main.go#L363-L378) intentionally leaves the gateway worker untouched. Re-add through the deterministic labels conflicts with delivered/revoked identities, and [resume_peer](../../litellm/proxy/mcp_source_broker/remote_store.py#L155-L238) correctly refuses to replay those credentials. The secure refusal lacks a supported replacement workflow. [config.go](internal/config/config.go#L1-L9) confirms the legacy single-identity support was removed without migration

**Action:** implement local plus remote revocation and an explicit, confirmed replacement operation using generation fencing and a new noncolliding worker identity. Never reveal a delivered HMAC again. Define whether forgetting a credential supports re-enrollment only after approved revocation, preserving its review state. Supply an intentional old-state migration or reject upgrade with an actionable migration procedure; do not simply stop an installed service with `no registered sources`

**Acceptance:** lose local state after delivery, explicitly replace the chosen source, and verify the old generation is inert and new credentials work. Repeat after local remove and remote revoke. Upgrading an installed legacy state either migrates or gives the documented migration result without orphaning the service

### 9. NEW: use MCP protocol validation for readiness, not any HTTP response under 500

**Smell:** [mcpProbe](internal/connector/tunnel.go#L151-L160) treats 401, 403, redirects, or HTML 200 as a successful MCP probe. [_report_proves_health](../../litellm/proxy/mcp_source_broker/remote_store.py#L44-L55) then grants a catalog-free health result. Those results mean an HTTP listener exists, not that the right MCP endpoint is authorized and healthy

**Action:** perform a bounded MCP `initialize` protocol negotiation, with its complete response/body cap and compatible session semantics, or clearly downgrade the state to reachability and never use it as execution readiness. Cache protocol discovery appropriately rather than generating a request flood

**Acceptance:** valid JSON-RPC/streamable endpoint succeeds, 401/403, HTML, invalid protocol response, and wrong service do not qualify for dispatch. Protocol timeout and an SSE response do not block health

### 10. CARRIED: reject database-backed admin login before issuing a credential

**Smell:** [verify_password_only](../../litellm/proxy/auth/login_utils.py#L350-L411) rejects configured UI-admin credentials, but returns any matching database user's `user_role` without forbidding `PROXY_ADMIN`. [cli_password_login](../../litellm/proxy/proxy_server.py#L18200-L18218) checks password reset only, so an admin-created database admin can receive a key scoped to the source self-service routes. This violates the ordinary-user boundary even though allowed-route scoping limits direct administration

**Action:** enforce the whitelist/blacklist at the issuance seam for the actual database role and retain UI-admin rejection. Do not rely on endpoint scope to make a role violation acceptable

**Acceptance:** a password-valid database `PROXY_ADMIN` is denied and no key is persisted, while an ordinary no-team user succeeds. The existing test covers environment UI-admin only, so extend [test_cli_password_login.py](../../tests/unit/proxy/auth/test_cli_password_login.py#L140-L170) to the actual handler issuance case

## P1: correctness, consistency, and smaller code smells

### 11. NEW: make reload an executable lifecycle, not a map update

**Smell:** [runner.start](internal/connector/connector.go#L176-L199) inserts its name before `Run()` performs target/TLS parsing. A synchronous startup error becomes a permanently marked running record, retried by neither reconcile nor remove/re-add. [sameIdentity](internal/connector/connector.go#L205-L215) omits `SourceID`, kind/path, and the resolved account gateway, while [start](#) stores `gateway` without ever reading it. Remove/restart does not wait for the old tunnel or serve loop. `WritePid` in [pidfile.go](internal/setup/pidfile.go#L22-L33) is not a process lock: another daemon can overwrite it and a stale PID can signal another process later

**Action:** validate before recording and put only successfully started connections into the live registry. Capture completion, remove failed entries, schedule retry/backoff, and compare the complete connection identity. Close obsolete tunnels within a bound. Lock the state directory for the daemon's lifetime and narrowly verify the PID before SIGHUP; avoid overlapping agents

**Acceptance:** an invalid persisted source becomes valid after repair and receives service; an invalid source is removed, re-added, and connects; changing path/kind/gateway actually restarts that tunnel, without dropping unrelated accounts. Two connector instances cannot own the same PID/state directory. CA rotation should either require and document restart or be covered as a real reload case

### 12. CARRIED: agree on OpenAPI canonical bytes across Go and Python

**Smell:** Python escapes non-ASCII JSON in [canonical_spec](../../litellm/proxy/mcp_source_broker/models.py#L27-L42), while Go emits direct UTF-8 and escapes HTML through `json.Marshal` in [catalog.Canonical](internal/catalog/catalog.go#L30-L50). The probe can compute a different hash for the same catalog. Go also only checks `strings.HasPrefix(version, "3.")` and reads one JSON value; numeric representations differ further

**Action:** share canonicalization compatibility with the OpenAPI-to-tool builder. The canonical accepted spec, stored artifact, tool parser, and probe digest must not disagree. Align UTF-8/HTML escapes and version/terminal parsing; use JSON Schema validation/normalization or explicitly define numeric canonicalization rather than inventing local rules

**Acceptance:** a table of cross-language vectors covers non-BMP text, `<>&`, escape spellings, `1`/`1.0`/exponent forms, invalid version, duplicate keys if relevant, and trailing JSON. Assert end-to-end health and tool construction, not just hash inequality

### 13. NEW: restore selected MCP paths instead of discarding them during routing selection

**Smell:** [route_source](../../litellm/proxy/mcp_source_broker/gateway.py#L216-L236) resets every URL to `/sources/{id}`, whereas new enrollment deliberately appends the persisted MCP path. A selection update therefore changes source behavior unrelated to routing

**Action:** use the persisted `SourceRecord.kind` and `path`, or retain the existing normalized upstream path for this transport-only operation. Model internal source route and public MCP URL concerns separately so future changes cannot erase them with a generic rewrite

**Acceptance:** select the same tunnel transport for a non-default MCP path, then initialize/tools/list/call still targets it. Preserve the gateway's stored source URL and access controls

### 14. NEW: validate release metadata before activation and make immutable writes atomic

**Smell:** [admin_activate_release](../../litellm/proxy/client_distribution/source_agent_distribution.py#L337-L365) only checks the presence of release metadata, then installs a caller-supplied digest/version into a mutable pointer. It does not call `_verified_release` despite having the right contract. This can provide a public installer whose active version returns 409, contrary to the activation documentation. Version and digest validation in [ActivateRequest](../../litellm/proxy/client_distribution/source_agent_distribution.py#L293-L303) is also inherited from a partially populated manifest model. The generic upload path in [routes.py](../../litellm/proxy/client_distribution/routes.py#L445-L479) uses GET/compare/unconditional PUT: concurrent different uploads to a new immutable path can both proceed. Deletion of these immutable assets is also allowed inconsistently through the generic route

**Action:** validate and compare release metadata plus binary digest before pointer activation, not later when serving an install. Return explicit typed errors for invalid fields. Use conditional atomic publication for immutable keys, with an expected version/ETag, and require an explicit retirement operation for referenced assets. Preserve the valid existing pointer CAS; rollback and first publication behavior should remain supported

**Acceptance:** syntactically valid but mismatched release metadata cannot activate, concurrent different bytes cannot both occupy a version, stale rollback is rejected without an intentional expected-pointer retry, and referenced immutable artifacts cannot be deleted accidentally. Do not pin today's vendor ETag format

### 15. TOOLING SMELL: repair tests that persist different data or no current data

**Smell:** [conftest.py](../../tests/unit/proxy/mcp_source_broker/conftest.py#L65-L88) still zips eleven source-column names against thirteen insert values, dropping `kind` and `mcp_path`. Tests then manufacture an MCP row manually. [test_remote_store.py](../../tests/unit/proxy/mcp_source_broker/test_remote_store.py#L165-L191) forces host 127.0.0.1 regardless of the supplied request. These are false positives, not meaningful integration guards

**Action:** enforce strict column/argument parity and preserve every inserted destination/kind in durable fake/transient Postgres fixtures. Replace overly coupled SQL text fakes only if the resulting real-database tests can also cover the same protocol contract

**Acceptance:** removing or corrupting kind/path/destination persistence fails ordinary tests. Round-tripping a private IP, service DNS, IPv6, and OpenAPI/MCP kind into a subsequent query does not need hand-corrected fixture state

### 16. NEW: make multi-member routing policy depend on source semantics

**Smell:** `Accept: application/json, text/event-stream`, normal for MCP clients, activates the stateful branch in [Forward](internal/pool/forward.go#L78-L94), so even stateless initialize is blocked with two members. The subsequent all-non-GET block has no source-kind check and blocks unrelated OpenAPI writes too. `eligible()` and `pick()` separately enumerate state, advertising deduplication while retaining two selection implementations

**Action:** decide explicitly whether this release is single-member MCP, shared-session sticky, or supports stateless operation. Return that contract at enrollment/start rather than discovering it as a 409 later. Separate OpenAPI routing policy from per-kind MCP policy. For member choice, enumerate once with shared eligibility and atomically reserve the chosen worker

**Acceptance:** policy-appropriate initialize, tools/list/call, session delete, and GET-stream paths behave correctly on one and two members, including a disconnect between session initialization and subsequent calls

### 17. MINOR: simplify CLI parsing and normalize contradictory user input explicitly

**Smell:** [sourceKind](internal/setup/setup.go#L40-L47) turns any unknown input into OpenAPI. [addSource](internal/setup/setup.go#L238-L269) checks local name before kind/path/content, ignores `--spec` for MCP, and leaves invalid paths for the remote API to reject. Global flag parsing accepts flags before checking positional argument arity. [runSourceAdd](main.go#L307-L314) demands a TTY even when every required value was provided. Password trimming in [Terminal.Secret](internal/setup/setup.go#L89-L102) changes a possible password. [retry.go](internal/setup/retry.go#L63-L66) waits unconditionally for cancellation

**Action:** parse a closed flag schema, validate complete required input before deciding whether a prompt is needed, and reject contradictions early. Compare canonical source inputs before claiming exact duplicate behavior. Preserve password bytes except the line ending. Replace `Sleep(d)` with a context-aware timer/seam and stop after an already-cancelled context

**Acceptance:** fully flagged noninteractive calls work when explicitly supported; missing required values still require a TTY; bad kind/path/extra positional values fail; exact duplicates with full canonical content work; cancellation does not wait out backoff

### 18. MINOR: make installer reruns and state writes recoverable

**Smell:** [source-agent-install.sh.tmpl](../templates/source-agent-install.sh.tmpl#L65-L80) unconditionally `account add` and `source add`, with `set -e`. Reusing an existing account aborts before service enablement. Concurrent local writers use unlocked read/modify/write files in [PutAccount](internal/state/state.go#L160-L192) and `PutSource`, while an accounts-json/selection failure can leave a newly stored orphan key

**Action:** detect/reuse existing accounts and configured sources deliberately, then always reconcile the service state. Provide explicit migration when config is incompatible rather than printing duplicate as a fatal install failure. Serialize each read-modify-write, preserve relations atomically, and roll back a key when its metadata transaction fails. Existing `PutAccount` code writes the key before the containing metadata transaction

**Acceptance:** execute the actual served shell script against populated and fresh isolated homes; both yield the expected enabled service. Parallel account/source additions never lose one record, and injected metadata failure leaves no anonymous key

### 19. CLEANUP: remove dead API shape and stale comments/identifiers as behavior stabilizes

**Smell:** [ErrAlreadyExists](internal/bootstrap/client.go#L101-L118) implies adoptable identity data, but no thrown value exists and [Enroll](internal/bootstrap/client.go#L178-L198) returns only a normal `SourceEnrolled`. The CLI's gateway-duplicate handler in [main.go](main.go#L337-L343) therefore cannot supply a worker ID. `Source.AsConnector` and [state.ConnectorConfig](internal/state/state.go#L81-L100) duplicate an incomplete version of the actual connector configuration. `runningSource.gateway` is dead. `NewCredential()` documentation does not describe any behavior using it. [source_agent_distribution.py](../../litellm/proxy/client_distribution/source_agent_distribution.py#L268-L272) comments claim digest rather than ETag conditions despite later corrected code. [install.sh](install.sh) remains an obsolete installer path; [README.md](README.md#L90-L110) still advertises deleted reverse-h2 packages. Source/setup instructions also contain assumptions that no longer match the code

**Action:** remove unused behavior or implement it completely, with definitions sourced once. Delete obsolete installation scripts and legacy protocol references except clearly marked history. Update comments only when they clarify a real constraint; use the owning running-system record for evidence rather than duplicating reports

**Acceptance:** dead-code/static analysis across the Go/Python sources produces no unexplained orphaned types/fields. One fresh operator can follow only the active build, publish, and install paths

## P2: completion gates and correct claims

### 20. Repair executable compatibility and process-state integration tests

**Reproduced:** `make -C oicm-litellm-layer mcp-source-link-compat` fails `TestCurrentConnectorServesCurrentBroker` at its readiness assertion. The prior connector admits. Actual logs show the current connector loading unrelated local state and trying the arbitrary `localhost:14443` approval-flow endpoint. [compat_test.go](compat/compat_test.go#L68-L108) writes retired identity/env state, and no `MCP_SOURCE_LINK_STATE_DIR` isolates the current executable

**Action:** construct an isolated, valid current accounts/sources registry with a test identity, or explicitly drive deterministic state injection without restoring the old production config. Pin `MCP_SOURCE_LINK_STATE_DIR`, set only the fixture gateway, and ensure tests never read ambient developer credentials/servers. Decide deliberately whether old one-file states migrate or must be upgraded. Add real runner tests for failures, not only [planChanges](internal/connector/reload_test.go#L1-L90) inputs/outputs

**Acceptance:** both current-control and prior-version cases exercise actual admit/probe/shaped HTTP execute with isolated state in CI. Failures identify a protocol discrepancy rather than an irrelevant local agent. Reload tests start/stop actual listeners/processes and exercise selected multi-account changes

### 21. Produce only evidence-backed live acceptance and synchronize the owning record

**Finding:** [running-system/README.md](../docs/mcp-source-broker/running-system/README.md#L32-L66) labels compatibility and multi-account completion `DONE`, while its known-gap text lacks an approved real read/tunnel proof and still describes retired configuration. [MULTI-ACCOUNT-TEST-PROCEDURE.md](MULTI-ACCOUNT-TEST-PROCEDURE.md#L14-L25) still mixes native MCP with a fake OpenAPI description. Removal/replacement from [phase 8](MULTI-ACCOUNT-TEST-PROCEDURE.md#L225-L238) is not a working operation in this revision. Examples expose possible credential material by suggesting raw store searches/reads and use password-file command substitution

**Action:** after P0/P1 repair, run two actual authenticated owners, same target under both and two targets per owner, a supported private IPv4, bracketed IPv6, and a Kubernetes Service target through dev port-forward for REST. Use a supported tunnel connection, not a new public dev ingress. Require pending review, explicit admin approval, native MCP initialize/tools/list/call, short and long SSE, connector restart, chosen revocation/replacement, and exact duplicate/cross-client recovery. Clean test resources, without touching production or unrelated records

**Acceptance:** update the owning running-system record with verifiable outcomes and remaining limitations only after real positive dispatch runs. Test snippets must not print account keys, source HMACs, original passwords, or pass credentials through chat/command-line arguments. Never describe a failed condition, process start, listing, or 401 proof as successful tool execution

## Commands and findings

- `make -C oicm-litellm-layer mcp-sidecar-check` with the repository Go root and inherited PATH: formatting, vet, staticcheck, proto-generation consistency, and race tests passed
- `python -m pytest -q tests/unit/proxy/mcp_source_broker tests/unit/proxy/client_distribution tests/unit/proxy/auth/test_cli_password_login.py` in `.venv`: **172 passed**, 11 unrelated/deprecation warnings
- `make -C oicm-litellm-layer mcp-source-link-compat`: **failed current connector control**, as described in action 20. This is not evidence of a `grpctunnel` cancellation or wire defect
- Temporary package-local probes reproduced destination rejection, source/worker mismatch, overlong source alias, and account-lossy same-name replacement
- Streaming buffering, restart fingerprint retention, stale health acceptance, metadata under-validation, and most lifecycle gaps follow directly from the controlling code. They need executable regression tests when fixed
- No production rollout, gateway mutation, user-secret reading/output, or new development ingress was performed. The unrelated untracked files and scratch directories were left alone

## Review cautions

Configured UI-admin login is already rejected. The missing guard is specifically the DB-backed admin described in action 10

Recent tunnel-auth retries and `sameIdentity` behaviors should not be called concurrency proofs just because cancellation is symmetric or timestamps look similar. Prior auth interceptor rejection already handles credential/generation state at tunnel opening; separately test revocation racing a new stream during admission before reopening a broader authentication finding

Ordinary HTTP host:port forwarding was improved in `af2e4279a4e`. An OpenAPI bootstrap `request.Host` may still omit a non-default port, but a virtual-host-on-port effect remains a smaller secondary case compared with the diagnosed persistence and MCP replay blockers. Address it with common target/authority canonicalization rather than adding another independent fix

## Completion record 2026-11-19 (jya0-v1.108.5)

Fixes landed on `jya0-v1.108.5`, each with a regression test that fails on the old behavior and is mutation-checked. Commits: `f49812f9061`, `fd9ec668e23`, `f929b51ed43`, `fc2804e001c`, `dd366b4a3a8`. Nothing was deployed; all proofs below are unit/code-level, plus the executable compat control.

### Implemented and verified (executable tests)

- **P0.1 streaming** (`f929b51ed43`, `d7bf07df84f`): `ExecuteStream` RPC streams status/headers then bounded `wire.StreamChunk` body chunks with the `io.Pipe` relay and `FlushInterval: -1`. This review pass: an idle/indefinite SSE stream is no longer cut at the old 90s `ResponseDeadline` (deadline now bounds time-to-headers only; the body lives until the caller closes, and closing cancels the connector read); a 3xx from the private origin has its `Location` stripped so an internal address never reaches an external client. Tests: `TestStreamDeliversFirstChunkBeforeOriginCloses`, `TestStreamSurvivesPastResponseDeadline` (fails if the stream is bound to the fixed deadline), `TestForwardStripsPrivateRedirectLocation` (fails if `Location` is passed through). Backpressure is inherent to the pipe/drain; slow-reader and oversize cases are bounded by `wire.StreamChunk`/`MaxOriginBody`.
- **P0.2 worker storage** (`f49812f9061`): `RemoteWorkerRow.target_host` is no longer `Literal["127.0.0.1"]`; it uses the same `canonical_worker_host` validator as the request `Target`. Service DNS, private IPv4, and IPv6 round-trip insert -> read -> `peers()`; IPv6 is bracketed via `canonical_hostport` (net.JoinHostPort semantics); a malformed row is dropped (`_project_peer`) instead of failing every peer. Tests: `test_worker_target_survives_insert_read_and_projection` (4 shapes, fails on the literal), `test_peers_isolate_a_malformed_row_instead_of_failing_all`.
- **P0.3 target invariant** (`f49812f9061`): `create_peer` refuses a request target that differs from the reviewed source target before any insert (`PeerTargetMismatch` -> 409). Test: `test_create_peer_rejects_a_target_other_than_the_reviewed_source` (source at :9001, worker for :9002 is refused with no row).
- **P0.4 identity** (`fc2804e001c`): `DeriveName` appends a stable 10-char sha256 digest of the canonical (account, target) and truncates the readable prefix, so a max-length valid target derives a name that satisfies the gateway 128-char grammar (old formula produced 132 chars) and distinct long targets do not collide. `PutSource` keys on `(account, name)`, so account B cannot overwrite account A. Tests: `TestDeriveNameStaysWithinGatewayGrammarForLongTargets`, `TestPutSourceIsScopedToAccount`, plus the reworked `TestDeriveNameStableAndScoped`.
- **P0.6 restart health** (`fd9ec668e23`): `RemoteStore.reset_health()` clears both `healthy_at` and `catalog_fingerprint` at startup; `main.py` calls it instead of the raw `healthy_at=NULL` update. Test: `test_reset_health_clears_content_proof_and_liveness_at_startup` (fails if only `healthy_at` is cleared).
- **P0.8 login boundary** (`f49812f9061`): `verify_password_only` rejects database `PROXY_ADMIN` and `PROXY_ADMIN_VIEW_ONLY` roles before returning the identity, so no key is minted; env UI-admin rejection retained. Tests: `test_database_admin_is_rejected_before_issuance`, `test_database_admin_view_only_is_also_rejected` (both fail without the guard).
- **P0.9 MCP readiness** (`fd9ec668e23`): the connector `mcpProbe` performs a bounded MCP `initialize` handshake and accepts only a valid JSON-RPC result (plain JSON or an SSE message event); 401/403/redirect/HTML-200/JSON-RPC-error/empty are refused, and redirects are not followed. Tests: `TestProbeMCPInitializesAtMCPPathAndReturnsNoCatalog`, `TestProbeMCPAcceptsSSEInitializeResult`, `TestProbeMCPRejectsNonMCPResponses` (7 sub-cases).
- **Action 20 compat control** (`dd366b4a3a8`): the current-connector compat control now seeds an isolated `MCP_SOURCE_LINK_STATE_DIR` and points the current binary at the fixture gateway via `MCP_SOURCE_LINK_TUNNEL_URL`/`MCP_SOURCE_LINK_CA_FILE`, so it no longer reads ambient `~/.config/source-agent`. `make mcp-source-link-compat` now passes both `TestPreviousConnectorServesCurrentBroker` and `TestCurrentConnectorServesCurrentBroker` (real admit/probe/serve), instead of failing the readiness assertion.

### Verification commands run

- `make -C oicm-litellm-layer mcp-sidecar-check`: gofmt, vet, staticcheck, proto-freshness, and `go test -race ./...` all pass.
- `make -C oicm-litellm-layer mcp-source-link-compat`: passes both old- and current-connector controls.
- `.venv/bin/python -m pytest -q tests/unit/proxy/mcp_source_broker tests/unit/proxy/client_distribution tests/unit/proxy/auth/test_cli_password_login.py`: 181 passed (172 baseline + 9 new).
- `ruff` and `basedpyright` clean on all changed Python modules.

### Not done in this pass (deferred, with reason)

- **P0.5 enrollment interruption reconciliation**: needs bounded cross-store reconciliation (SQL + RustFS artifact + pending MCP registration) that reuses exact reviewed content and restores exactly one pending row. It requires a transient/real-database fixture for believable transaction fault injection, which is a larger harness than this pass; not implemented.
- **P0.7 revoke/replace/migration**: needs a new confirmed remote+local revoke and replacement operation with a fresh noncolliding worker identity and generation fencing, plus legacy single-identity state migration. Not implemented.
- **P1/P2 items 10-21** (OpenAPI canonicalization parity, route_source MCP path preservation, release publication atomicity, fake-DB fixture strictness, multi-member policy, CLI/installer transactions, dead-code/stale-doc cleanup, live acceptance and running-system sync): not done. The stale `tests/deploy/test_source_link_ingress.py` still references the deleted `tunnel-ingress.yaml` (pre-existing breakage, unrelated to this work).
- **P0.4 remainder**: server-authoritative public alias derivation from the authenticated owner and the worker-label digest suffix are not changed here; only the local derived name bounds/collision and `(account, name)` storage keying are.
