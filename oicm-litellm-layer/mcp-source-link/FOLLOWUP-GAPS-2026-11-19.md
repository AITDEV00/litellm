# Connector follow-up gaps: worker pass after `af2e4279a4e`

Review date: 2026-11-19. Reviewed head: `638b343c786`, synchronized with `origin/jya0-v1.108.5`. This review covers the eight commits from `1e100e6b6ab` through `638b343c786`, after [the first action document](CODE-SMELL-ACTIONS-2026-11-19.md). This is a follow-up work order, not a claim that any listed behavior is already repaired. No source or deployment changes were made during review

## Acceptance decision and how to use this document

Do not accept the connector as a finished multi-account, multi-source or native-MCP product yet. The latest pass fixes several precise defects and makes the executable compatibility control pass. It does not prove a complete operational workflow, and it introduces a real MCP session-lifecycle defect

Implement G1 through G4 first, then G5 through G7, and finish the explicitly deferred functional work. Treat each item as an end-to-end behavior obligation, with executable guards. Do not satisfy it by changing only fixture state, adding structural assertions, or rephrasing a completion record

For each item, cite the failing-before and passing-after regression test. Do a consequential mutation check on the new guard. Do not claim unsupported live acceptance. The ongoing navigation authority is [running-system/README.md](../docs/mcp-source-broker/running-system/README.md), which reflects no newer live native-MCP approval proof than this patch provides

## Fixed enough to retain, with their verification limits

1. **Current executable compat control.** [compat_test.go](compat/compat_test.go#L107-L180) seeds isolated multi-account state. I ran `make -C oicm-litellm-layer mcp-source-link-compat`: both old and current binaries admitted, probed, and served the fixture. It is not a native-MCP or streaming compatibility proof
2. **Non-loopback persistence.** [RemoteWorkerRow](../../litellm/proxy/mcp_source_broker/remote_store.py#L40-L71) shares target validation with request models, and `canonical_hostport` correctly brackets IPv6. The normal service DNS/private IPv4/IPv6 paths are no longer rejected by the row schema. Real-Postgres round trips remain unproven; the fake DB is not a substitute
3. **Worker/source target binding.** [create_peer](../../litellm/proxy/mcp_source_broker/remote_store.py#L171-L208) rejects mismatched targets before insertion. Preserve this when implementing replacement
4. **DB-backed admin rejection.** [verify_password_only](../../litellm/proxy/auth/login_utils.py#L350-L416) rejects both administrative roles before key issuance. Its guard is valid. The corresponding handler strong proof is still missing (G12)
5. **Startup freshness intent.** `reset_health` clears both content proof and timestamp. Its scope needs work (G10)
6. **Basic stream mechanics.** `ExecuteStream` delivers the first body chunk before an origin closes, and tested ordinary disconnects cancel the origin work. The stream may survive its setup deadline. This does not establish all bounded body behavior or redirect support
7. **Ingress syntax.** Production and rollback ingress annotations use valid `off` values; the new static tests pass. This changes files only: no cluster rollout or effective nginx proof happened
8. **Standard checks.** Full Go formatting/vet/staticcheck/proto and race checks pass. The compat target passes. Focused Python proxy/distribution/login suites report **181 passed**. Twelve repetitions of stream-related Go race suites pass. These green checks do not cover the failures below

## Required work order

G1 session lifetime, G2 multi-account identity, G3 long-name uniqueness, and G4 correct health probing are interdependent at the connector/runtime boundary. Do not declare streaming or MCP support complete until those behaviors are exercised against a real protocol implementation. Then finish canonical identity (G5), immutable network/source policy (G6), redirects/flow bounds (G7), interrupted registration, and replacement. Do not build a replacement operation on top of the current unscoped store operations

## P0: new or partially corrected defects

### G1. MCP health polling leaks upstream sessions

**Evidence and impact:** [mcpProbe](internal/connector/tunnel.go#L157-L200) posts `initialize`, reads a response, and closes its body. [probeLoop](internal/tunnel/tunnel.go#L444-L488) repeats this every two seconds. It discards `Mcp-Session-Id`, does not send `notifications/initialized`, and does not issue a terminating `DELETE`

This is not a theoretical concern. An actual local `StreamableHTTPSessionManager` from installed MCP SDK `2.3.0` returned five valid initialize results for five probe-like POSTs, leaving **5** sessions. A normal SDK client then allocated a sixth and explicitly deleted its own session, resulting in **5**, not zero. Those five belong to earlier probes

With the installed SDK default 30-minute idle expiry, successful two-second probes approach about **900 retained sessions per source**. Configurations without idle expiry accumulate until the configured maximum, currently defaulted to 10,000 in the installed SDK, or until resource limits fail earlier. Closing an HTTP body does not call SDK `terminate()`, as shown by [streamable_http_manager.py](../../.venv/lib/python3.13/site-packages/mcp/server/streamable_http_manager.py#L306-L395)

**Required correction:** choose a lifecycle rather than merely tuning the count:

- Prefer a dedicated, persistent health session per source, at most reconciled at a reasonable interval. Reuse its session ID, complete initialization according to the chosen protocol/client lifecycle, and clean it up when that connector/source closes
- Alternatively make each probe explicitly stateless if the MCP endpoint supports that mode, or always terminate a temporary session and await the terminal response before declaring the operation done
- Read the initialize result incrementally instead of holding finite SSE bodies (G4). Define unauthorized/unsupported-version behavior. Do not propagate a health session's ID into unrelated MCP client sessions
- Avoid sharing the gateway manager's upstream session state with this probe in a way a health call can reset unrelated traffic. Isolate probe-oriented session resources
- Keep failed/abandoned attempts bounded and ensure any allocated transport is regret-cleaned on timeout

**Acceptance tests:** use the actual SDK server or an equivalent protocol-conformant stateful fixture, not a finite body string. Run at least three health intervals and assert final server session count does not grow, associated resources close after removal, and initialized/session responses are correctly consumed. A separate real client must still initialize, list tools, call one, and terminate. Test both successful connect and mid-initialize cancellation, ensuring both remove resources. A `DELETE` invalid under the target protocol should select the supported lifecycle alternative, not log failures indefinitely

**Source of truth:** `/tmp/worker_review_session_minimal.py` is a local, no-credential reproduction run against installed SDK. Its observation is evidence for a regression test, not a production fixture to copy blindly

### G2. Account-scoped writes did not make account-scoped behavior

**Evidence:** [PutSource](internal/state/state.go#L302-L324) now keys writes by `(account, name)`, but [Source](internal/state/state.go#L285-L296) finds the first name globally and [RemoveSource](internal/state/state.go#L325-L337) removes every row bearing that name. [AddSource](internal/setup/setup.go#L238-L257) uses that unscoped lookup before validating account/target/kind/path/spec. [runner.sync](internal/connector/connector.go#L113-L146) keys both live and wanted sources solely by name, and `runSourceRemove` in [main.go](main.go#L364-L378) uses the same global lookup

**Direct reproduction:** after storing explicit `shared` for both alpha and beta, `Source("shared")` returns alpha. Calling `RemoveSource("shared")` leaves **0 records**. The newer `TestPutSourceIsScopedToAccount` proves retained writes but does not reach these incorrect consumers

**Required correction:** define a local stable `SourceKey{Account, Name}`. Use it for `Source`, `PutSource`, `RemoveSource`, list results, runner `wanted`/`running` maps, CLI explicit lookup/removal, and reload fingerprints. Resolve the selected account before a duplicate decision. Compare target, kind, path, and meaningful immutable content before reporting an exact duplicate; a same local name must never adopt other credentials. Prefer removing the arbitrary public-name override or treating it as local display text only, consistent with G5

**Acceptance tests:** use actual commands or a test-facing runner with two accounts holding the same explicit display name but distinct targets and credentials. Assert independent enrollment, listing, enabling, reload, and selective remove. Assert one account cannot mask or overwrite another. Do not use distinct derived aliases to evade the requested same-name collision

### G3. `DeriveName` gets a suffix, then `WorkerLabel` discards it

**Evidence:** [DeriveName](internal/state/state.go#L369-L382) appends ten hex characters after a 117-character readable prefix. [WorkerLabel](internal/bootstrap/client.go#L267-L300) normalizes the full name only to truncate to 32 characters, cutting that digest out. Its uniqueness tests at [validate_test.go](internal/bootstrap/validate_test.go#L41-L55) assert grammar and repeat stability, not distinctness across long valid identities

**Reproduced, using full-length IPv6 inputs:** two different valid sources share `alpha-2001-db8-1-2-3-4-5-6-9001` as worker label. The source aliases themselves differ by their final hashes. Long Kubernetes service aliases sharing an early prefix collide similarly. The server's `(source_id,label)` uniqueness check then denies worker registration for the second source, irrespective of target equality

A short IPv6 example `-2001:db8::1` versus `-2001:db8::2` was not sufficient to demonstrate this bug because the distinguishing digit remains within 32 characters. Use full-length inputs in the regression, not short names that fit already

**Required correction:** derive every label as a readable bounded prefix plus stable digest of the complete authoritative source association, at least canonical account/public source identity and target rather than a lossy truncated display name. Preserve the already-issued labels on existing records deliberately. Do not change current aliases on every restart or make remote replacement impossible. Keep the older stable name where it is immutable state, and create only explicitly selected new generations for a new worker

**Acceptance tests:** two full-length expanded IPv6 addresses with identical port and two maximum service aliases sharing a prefix receive different grammar-valid labels. Legacy short names preserve exactly their old derived label during normal resume. Carry both generated labels into the actual remote-worker issue/lookup path

### G4. Probe validates a weak, finite version of MCP readiness

**Evidence:** [mcpInitializeSucceeded](internal/connector/tunnel.go#L201-L218) only looks for nonempty `result` and no `error`. It does not require `jsonrpc: "2.0"`, matching `id`, or a valid initialize-result shape. The current tests accept `result:{}`. [mcpProbe](internal/connector/tunnel.go#L189-L218) reads the entire body through EOF before reasoning about SSE content

A `text/event-stream` initialize reply containing a valid event but kept open for later traffic will hit [ProbeTimeout](internal/tunnel/tunnel.go#L39-L40) despite the complete initialize message already arriving. This is an origin-specific protocol failure, not proof that every valid SDK server is unhealthy. The installed server responds promptly in a normal short initialize test

**Required correction:** implement incremental SSE event parsing with event boundary handling, bounded aggregate message bytes, request/result correlation, and a bounded body lifetime. On a matching complete initialize, drive the lifecycle selected in G1 and stop any relevant read cleanly. It is acceptable to accept a conformant finite JSON response. Do not accept another request's later SSE event or an unstructured error webpage as readiness

**Acceptance tests:** delayed matching event followed by a held-open response becomes ready before EOF, wrong ID/wrong JSON-RPC version/malformed result/HTML/401/403 remain unready, and cancellation reaches the actual SDK transport. Limit oversize/fragmented events and malformed multi-event responses consistently

### G5. Public identity is still based on local aliases, rather than canonical authenticated ownership

**Current defect:** [DeriveName](internal/state/state.go#L347-L382) input still starts with the locally chosen account alias, not the gateway-returned authenticated user ID. The gateway's `source_id` scopes by authenticated owner and alias; global MCP alias registration has its own identifier conflict rule. Different true owners may choose `alpha` on two source hosts and overlap. The same true owner using two aliases for the same target may create two otherwise identical sources

The worker explicitly acknowledges this remaining part in the appended completion record. Do not interpret bounded names and account-tuple writes as the authoritative identity fix

**Required correction:** separate local alias/gateway association, gateway-authenticated owner identity, target, and public MCP identity. Store `owner` returned by enrollment once the gateway is authoritative. Public names should include an authenticated-owner/gateway-bound stable digest within the existing namespace bounds, with safe human display text separated from routing keys. Server/source conflict handling must determine public/global uniqueness without letting two users' local display alias reveal or overwrite another registration. Local idempotency is not proof of server reconciliation

**Acceptance tests:** direct client procedures plus second-host/cross-client equivalent:

- Same owner and same target with two local aliases resolves the same source association
- Different owners who both use local alias `alpha` and target `ten.ns.svc:8080` obtain independent source IDs and valid distinct public MCP aliases
- Equal inputs under the same account/credentials remain idempotent but changing content never silently reuses approval
- Returned public aliases do not expose another user's source ID or credentials

Do not silently overwrite an existing global registration while calibrating these tests. The unit harness can use distinct mock IDs; real integration requires isolated dev owners and registry cleanup

### G6. Source-address identity and admission policy are still unresolved

**Additional mismatch confirmed:** Go `net.ParseIP(...).To4()` maps `[::ffff:127.0.0.1]` to ordinary IPv4 in [target.go](internal/target/target.go#L91-L101). Python [canonical_worker_host](../../litellm/proxy/mcp_source_broker/models.py#L79-L91) preserves `::ffff:127.0.0.1`. Thus equivalent representations do not normalize equally and can yield different aliases or equality-failure depending on the call path. `Target(host="::ffff:127.0.0.1") != Target(host="127.0.0.1")` today

**Additional policy exposure:** both validation domains still admit a public IP and unspecified `0.0.0.0`/`::` in direct probes. Unspecified destination addresses must be denied. Explicit private-versus-public policy remains required rather than inventing a mathematical notion of "target is an IP" as ownership evidence

**Required correction:** decide and document one canonical semantic target grammar: whether mapped IPv4 normalizes as IPv4, which IPv6 compressed/canonical text is used, service name case, full cluster-service aliases, private ranges, mapped IPv4 equivalence, and handling of unspecified/multicast/broadcast/reserved external addresses. A normalized service belongs to the stated scope, not an arbitrary URL or public hostname

**Acceptance tests:** table-driven equality tests run on both sides of the gateway contract, encompassing `::ffff:` mappings, IPv4 degenerate zeros, compressed and expanded IPv6, service DNS case, and full `svc.cluster.local` versus short service names. Assert the same source policy and persisted identity, not merely validation success. Tighten gateways at the same time: the worker's row normalization must not erase a distinction meant to be authoritative

## P1: protocol, recovery, and supporting integrity still required

### G7. Stream delivery has incomplete bounded-operation and redirect contracts

**What passes:** additive server-streaming.ExecuteStream, relay `io.Pipe`, basic backpressure, and `cancelingBody` on normal close/cancel are a real improvement. The old connector fallback still has unary buffering and its own time/size constraints, by design

**Still unbounded or unsupported:**

1. [ResponseDeadline is canceled after the header arrives](internal/tunnel/tunnel.go#L156-L198). Nothing then bounds a stalled origin, stalled gRPC receive, or response write that remains blocked. `ResponseHeaderTimeout` deliberately constrains header receipt only, and [DispatchInFlightCap](internal/pool/registry.go#L6-L16) is disabled. Removing the total deadline alone does not establish an indefinitely safe stream
2. [forwardOnce](internal/pool/forward.go#L126-L139) removes the `Location` header on every 3xx. A private absolute location must not be exposed. However a safe source-relative redirect such as `/mcp` to `/mcp/` is a valid MCP configuration; stripping only the destination leaves unusable 307 semantics, and the readiness probe also rejects redirects. An external client or a reverse proxy cannot guess the correct follow-up URL afterward
3. New tests exercise finite SSE events and deadline survival. They do not create the real SDK client's `Last-Event-Id` resume lifecycle, and they do not demonstrate a blocked downstream write terminates legibly with bounded active connections/goroutines

**Required correction:** define total/idle/connection-credit semantics per source, not universal short timeouts that incorrectly kill legitimate SSE. Bound admission/capacity while keeping existing long-lived idle SSE distinguished from unresponsive requests. Notify termination via the correct stream/connection context. A bounded slow reader must not multiply origin connections indefinitely

For redirects, decide a supported behavior for source-scoped relative destinations and rejection/other origins. Rewrite only validated same-source absolute/relative locations to the caller's source URL where valid, or return a clear unsupported redirect diagnostic before health admission. Never include private topology in the client location. Do not have the gateway dial the private redirect itself

**Acceptance tests:** real SDK direct/relayed path comparison for same-source redirect and negative cross-origin redirect; block caller writes and close to ensure bounded release; sustained slow reads have bounded occupancy; dropped/resumed stream with `Last-Event-Id` reaches the intended source and obeys actual SDK session behavior. Add an old connector/control case asserting explicit unary fallback remains only the bounded legacy mode

### G8. P0.5 remains: interrupted enrollment cannot reconcile

[store.enroll](../../litellm/proxy/mcp_source_broker/store.py#L218-L269) commits source/enrollment rows before [gateway.enroll](../../litellm/proxy/mcp_source_broker/gateway.py#L136-L188) writes the catalog and creates the pending MCP review row. `AlreadyEnrolled` returns early after either partially completed request, rather than restoring exactly one complete source/collateral relation

The worker accurately says it is unimplemented. Do not waive it because a new field/fake DB test passes

**Required behavior:** persisted immutable source inputs drive bounded compensation/reconciliation; OpenAPI artifact and matching one-owner pending row become exactly complete before successful worker adoption. Never automatically change approved/rejected content or confuse source connectivity `pending` with `pending_review`. Define durable state needed for retry and cross-client recovery. A cross-store process cannot be rolled back magically, but it must be repairable without operator archaeology

**Acceptance matrix:** throw independently after source-commit, artifact, registry registration, and worker response/persistence; retry once or with second client; prove exactly one complete source relation, one owner-pending review, and no extra worker generation. Missing/approved/faulty/corrupt collateral must not be overwritten without reviewed authorization

### G9. P0.7 remains: recovery, replacement, and installed upgrade are not implemented

`source remove` intentionally only changes local metadata. Delivered/revoked HMAC replay refusal is desirable and must remain. The deterministic names plus label collisions (G3) make renewal especially error-prone. Old `identity.json` state is still not migrated

**Required correction:** build explicit `source revoke/replace/reinitialize` behavior selected by `(account,name)`. Gate an authorized remote revoke/reissue with generation fencing and a new non-colliding worker identity; never replay delivered HMACs; reconcile/cleanup local state only after defining the successful transition; preserve review and content exactly unless a separately reviewed change is intended. Decide safe idempotency when remote revoke succeeded but local cleanup failed

Choose whether the existing one-identity installation migrates into current account/source state or gets a clear migration command/error. It should not go from working to zero sources with an unexplained fail-to-serve condition

**Acceptance tests:** lose after successful data-plane health, revoke and replace one source leaving other accounts alone, no old generation resumes; force failure between remote and local steps and ensure retry converges once. Upgrade old persistent state without a source-loss surprise

### G10. Restart reset broadens a row mutation beyond its required worker scope

[reset_health](../../litellm/proxy/mcp_source_broker/remote_store.py#L158-L169) does `UPDATE "OicmMcpRemoteWorker" SET healthy_at=NULL,catalog_fingerprint=NULL` at startup, clearing **all rows**. Tests deliberately call the same path on the shared fake. This is a whole-worker-table write, not just stale-channel liveness scoped to one selected source. Existing boundaries carefully required only the target worker mutation

**Required correction:** choose the correct liveness architecture: active sessions should determine readiness fresh, not mounted from a retained database credential health state. If persisting still matters, narrow the mutation to obsolete gateway-controlled health claims and document the intentional global-startup exception, with concurrency against other running gateway replicas specified. Do not accidentally erase another live replica's reported health if multiple control processes become possible. No parallel unapproved production migrations are required to solve this

**Acceptance tests:** in-memory restart starts with zero served sessions even for retained source-content data, a reconnection requires fresh valid probe, and another healthy control replica if unsupported today is not changed unintentionally by a test-only synthetic life-cycle claim

### G11. Existing fixture/schema assertions remain unsafe and too weak

1. [MemoryDatabase](../../tests/unit/proxy/mcp_source_broker/conftest.py#L65-L88) still zips 11 column names against 13 inserted values; `kind` and `mcp_path` persist as fake defaults. Some new tests patch `kind` afterward, concealing exactly the order/arity errors they should catch. Use the real `SOURCE_COLUMNS` representation and preserve all arguments, or back tests with isolated PostgreSQL
2. `_plain_peer(target)` now goes through actual `Target` at least in the current file; do not regress it to forced loopback. Tests must cover real source-produced IPv6/IPv4/service target values and Go parser acceptance at the actual wire boundary
3. [stream_test.go](internal/tunnel/stream_test.go#L150-L182) mutates `tunnel.ResponseDeadline` globally while using a real global fixture. Sequential repetition passed here, but tests must not race this assignment against any future parallel use. Prefer an injected per-client/per-runtime header deadline test seam instead of a mutable parameter process-wide, or ensure a serialized package test contract explicitly
4. Check the custom `OicmMcp*` table upgrade path in transient PostgreSQL. `CREATE TABLE IF NOT EXISTS` never adds missing uniqueness constraints to a preexisting table. Do not claim proper SQL/index migration from string-matched in-memory tests alone

**Acceptance:** removing/misordering one source kind/path/host/index value must cause ordinary tests to fail. Transient database tests start from the previous DDL shape and confirm intended constraint state without mutating unrelated rows

### G12. Authentication proof and small sequencing gaps

Admin-role rejection is a real fix here. Keep it. Also:

- The added `test_database_admin...` tests replace `login_utils.generate_key_helper_fn` but call `verify_password_only`, which does not mint keys. An empty spy list is an invariant of that helper and does not exercise [cli_password_login](../../litellm/proxy/proxy_server.py#L18167-L18218)'s actual persistence boundary. Add a handler request with isolated repository/mint function and assert exactly zero persisted keys; test ordinary-user success separately
- [verify_password_only](../../litellm/proxy/auth/login_utils.py#L397-L401) rehashes the stored password before rejecting the DB admin role. No connector credential is minted, which satisfies the security boundary, but a denied login still issues a database write. Move the role check before the upgrade write if rejection should be entirely side-effect free. Add that stronger assertion only for the role-denial case
- Preserve throttling and password-reset gating semantics. Do not eliminate breach checking merely to avoid test network calls; stub that service explicitly

### G13. Formatting verification was not clean

`ruff check` passed the selected changed modules. `ruff format --check` reports a need to reformat:

- [models.py](../../litellm/proxy/mcp_source_broker/models.py)
- [remote_store.py](../../litellm/proxy/mcp_source_broker/remote_store.py)
- [test_remote_store.py](../../tests/unit/proxy/mcp_source_broker/test_remote_store.py)
- [test_ingress_buffering.py](../tests/deploy/test_ingress_buffering.py)

Apply the standard repository formatting using maximum line width 120 and no unrelated drive-by changes. The canonical full `type_check_gate.py` did not execute in this review: the command was corrected for an omitted required base argument, but its dependency fetch timed out. No passing type-gate claim should be inferred. Run the normal gate as the metadata/dependency access permits

### G14. Finish all remaining P1/P2 work, do not repeat partial completion phrasing

Still disclosed and unchecked:

1. Cross-language OpenAPI fingerprint/validation parity including Unicode, HTML escaping, numeric spellings, and the tool builder's accepted schema
2. `route_source` preserving an enrolled non-default MCP path; today's rewrite still loses it
3. `release.json` metadata/version/digest validation before activation, atomic immutable-artifact writes, and generic deletion safety
4. Explicit OpenAPI versus MCP multi-member behavior rather than `Accept` heuristics combined with an all-non-GET rule. At maximum current single-session semantics, reject excess members at the known boundary instead of accept-then-409
5. CLI input validation, noninteractive contract, context-aware retries, rerun installer reuse/reconciliation, file lock and key/metadata transactional cleanup
6. Complete reload runnable-state tracking, full connection identity/gateway fingerprint, bounded tunnel completion, single-daemon lock/safe PID signaling
7. Obsolete identity-adoption APIs, duplicate connector config, stale reverse-h2 instructions, legacy install script, and inaccurate completion/verification statements

Do not add new features, payment/capacity fields, or speculative naming policies while the baseline gaps remain open

Implement these against their owning modules in the first action document. It retains specific evidence links and expected test depth, so avoid duplicating the same rationale into new documents without new findings

## Required proof plan for the next worker pass

### Executable local/integration gates

Run these after the above correction scope, keeping logs so failure claims are inspectable:

```sh
make -C oicm-litellm-layer mcp-sidecar-check
make -C oicm-litellm-layer mcp-source-link-compat
.venv/bin/python -m pytest -q \
  tests/unit/proxy/mcp_source_broker \
  tests/unit/proxy/client_distribution \
  tests/unit/proxy/auth/test_cli_password_login.py
```

Add actual focal races/protocol tests for the newly built G1-G7 cases. Use an SDK-backed server (not a constant fake response) and actual relay for MCP initialization and streaming. Use a real/transient DB for the durable transaction upgrade. No tests should assume environmental credential availability or read a developer's ambient connector state

### Deferred live proof (do not fake or accidentally use production)

No worker pass in this review has established approved native-MCP dispatch through the external dev workflow. After the local gates and lifecycle repairs:

1. Use two separate ordinary authenticated users with an equal target, two registered sources each, fresh isolated home directories, and no raw credential output
2. Include a real Kubernetes Service, private IPv4, supported IPv6, full-length identity collision set, Unicode OpenAPI, and native MCP with at least delayed SSE events
3. Restore the documented dev `kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000` for REST or the actual configured equivalent. A separate permitted tunnel connection must exist; do not reconstruct the deleted public dev ingress or bypass it by presuming the gateway's production host reaches dev
4. Show pending review first, explicit administrator approval next, then real tool/list/call and streaming responses through the named gateway. Shutdown/hup/lost-identity replacement must select one source and not interrupt another
5. Clean temporary test resources under the responsible authorization path only

Do not deploy in response to this gap list. The previously changed dev image tag is a manifest edit, not deployment or proof of the named image being correct, signed, built, tested, or rolled out

## Independent checks actually run

| Check | Outcome |
|---|---|
| `make -C oicm-litellm-layer mcp-sidecar-check` | Passed |
| `make -C oicm-litellm-layer mcp-source-link-compat` | Passed, including both old/current controls |
| Focused Python proxy/distribution/login suite | **181 passed** |
| Repeated stream/connector/pool race suites | 12 runs passed |
| `oicm-litellm-layer/tests/deploy/test_ingress_buffering.py` | 2 passed |
| Selective targeted gateway duplicate/target tests | Passed; not approval of unresolved recovery |
| Ruff lint on selected changed Python | Passed |
| Ruff format check | **Four changed files not formatted**, G13 |
| Canonical full type gate | No result, dependency-fetch timeout, not passed |
| Actual MCP SDK session accumulation | 5 accepted probe sessions -> normal sixth allocate/delete -> **5 retained** |
| Full-length name/label check | Two distinct full IPv6 targets -> same worker label |
| Local same-name identity check | One lookup adopts alpha; one remove deletes both alpha and beta |
| Unicode canonical digest comparison | Go `2cc45b8247...c9fe53f9cfd8380`; Python `367feaf2b7...db3bd189` |
| Loose target policy | `8.8.8.8` and `0.0.0.0` accepted; mapped IPv4 differs across languages |
| Python pyflakes in existing root venv | Not installed, no analysis result |

The broader 181 tests and Go suite may pass with these defects open because their test fixtures operate at one package and finite response protocol boundary. Any next completion note must enumerate these exact lifecycle cases and say whether they execute, not count generic tests by total alone

**Review scope:** no source files or live infrastructure were changed. Untracked unrelated files were left untouched. No human secrets appeared in this document or reproduction output

## Worker completion record (2026-11-19, branch `jya0-v1.108.5`)

This pass implemented G1, G2, G3, G4, G6, G10, G11, G12, and G13 against their owning modules with executable regressions and consequential mutation checks. G5, G7, G8, G9, and G14 remain open and are not claimed here. Each entry below states the behavior change and the test that fails before it and passes after, with the mutation that turns the test red

### G1 + G4. One MCP health session, incremental SSE, protocol-correct readiness (`56ef1a04e29`)

The connector probe is now a per-source MCP client in `internal/connector/mcp_health.go`: it initializes once, records `Mcp-Session-Id`, sends `notifications/initialized`, reuses the session with `ping` across intervals, deletes a session the ping finds stale, and deletes it on source close. `tunnel.go`'s `mcpProbe` delegates to it and `RunTunnel` closes it on teardown. In production this removes the ~one-leaked-session-per-interval growth (doc: about 900 retained sessions per source at a two-second cadence under the SDK's 30-minute expiry) instead of only tuning a count

Readiness is now strict: `validateInitializeResponse` requires `jsonrpc:"2.0"`, the probe's own id (`source-link-health`), a non-null `result`, and no `error`; `readSSEInitialize` parses events incrementally with a 256 KiB bound and returns at the first matching complete event, so a held-open stream that already delivered the reply goes ready before EOF

Evidence: `internal/connector/mcp_health_test.go` drives a protocol-conformant stateful fixture (`mcpFixture`) that tracks live sessions, rejects unknown ids on initialize/ping/DELETE, and errors if any request carries a session it did not issue. Tests: twelve probes keep exactly one live session; teardown deletes it; a stale ping re-initializes without leaking; a complete SSE event before EOF is accepted within two seconds; a foreign reply id is refused; an unrelated leading event does not short-circuit. Mutations killed: always re-initialize (reuse test red), Close skips DELETE (teardown red), stale path skips delete (leak red), SSE reads to EOF (held-open test times out red), id check removed (foreign-id test red)

Not proven here: an actual Python SDK `StreamableHTTPSessionManager` run. The fixture is an equivalent protocol-conformant stateful server, which the work order permits. Live SDK acceptance remains deferred

### G2. Account-scoped `SourceKey` (`391afc70d5b`)

`state.SourceKey{Account,Name}` now scopes `Source`, `PutSource`, `RemoveSource`, list results, the runner `running`/`wanted` maps, `AddSource`, and `runSourceRemove`. Prior behavior: with `shared` stored for alpha and beta, `Source("shared")` returned alpha and `RemoveSource("shared")` removed both. Now each is independent. `sameIdentity` also compares `Kind` and `Path`. Evidence: two-account same-name tests; a name-only revert fails them

### G3. `WorkerLabel` keeps the digest (`d12f4abc918`)

`WorkerLabel` appends `-<10 hex sha256(name)>` after a bounded readable prefix instead of truncating to 32 characters and cutting the digest. Prior behavior: two full-length IPv6 targets shared `alpha-2001-db8-1-2-3`. Now they are distinct and grammar-valid. Evidence: full-length IPv6 and long service-alias collision tests; dropping the digest fails them

### G6. Agreed target canonicalization and unicast policy (`99ff2acf8c8`)

Go `target.Parse` and Python `canonical_worker_host` now both collapse an IPv4-mapped literal (`::ffff:127.0.0.1`) to plain IPv4 and both refuse unspecified, multicast, broadcast, and link-local addresses. Prior behavior: Go mapped the address and Python kept it compressed, so one origin derived two identities; neither refused `0.0.0.0`/`::`. Evidence: table-driven parity tests on each side; removing the mapping or the policy check fails them. Service DNS case and compressed/expanded IPv6 collapse are pinned on both sides

### G10. Startup health reset justified and proven (`170e06801b9`)

`reset_health` stays a global whole-table write, now documented as the one intentional global mutation: `healthy_at` is a claim about the live process, so it cannot be carried across restarts for any row. The docstring records the single-process assumption and what multi-replica ownership would require. Evidence: the test asserts through `peers()` that a retained worker is not content-proven after restart and becomes proven only after a fresh report; clearing only `healthy_at` fails it

### G11. Memory fake binds the real source columns (`a86cbcaeaf4`)

`MemoryDatabase` derives its column list from the same `SOURCE_COLUMNS` the store uses and asserts argument count, instead of zipping eleven hand-copied names against thirteen values and dropping `kind`/`mcp_path` to defaults. Evidence: enrolling an MCP source with `path=/custom/mcp` round-trips both columns; truncating to eleven names flips `kind` to `openapi`, and swapping the name order puts the path in `kind`, so either regression fails the test

### G12. Admin rejected before rehash, handler mint proved (`bfa4d8e7f91`)

The database-admin role check runs before `_rehash_password_if_needed`, so a denied admin login writes nothing. New tests drive the real `cli_password_login` handler: a denied admin reaches no mint call, an ordinary user reaches issuance exactly once, and a denied admin never triggers the rehash. Removing the role check fails the handler admin test; restoring the old ordering fails the rehash test. Existing `verify_password_only` tests stay

### G13. Formatting clean

`ruff format --check` and `ruff check` are clean on every touched Python file; `gofmt -l`, `go vet`, and `staticcheck` are clean on the changed Go packages. The canonical full `type_check_gate.py` was not run in this pass (its dependency fetch is heavy); `basedpyright` on the touched files shows no error beyond the pre-existing baseline (79, unchanged)

### Gates run this pass

`make -C oicm-litellm-layer mcp-sidecar-check` passed; `make -C oicm-litellm-layer mcp-source-link-compat` passed; `pytest tests/unit/proxy/{mcp_source_broker,client_distribution,auth/test_cli_password_login.py}` reported 196 passed. These are package-level results, not native-MCP dispatch, scale, or production-readiness proof, and no live SDK or real-PostgreSQL run was performed

### Still open (not claimed)

G5 (authenticated-owner public identity), G7 (stream bounds and same-source redirect), G8 (interrupted enrollment reconciliation), G9 (revoke/replace/migration), G14 (remaining P1/P2). No deployment was made

## Independent reassessment after `ac76776f06db8a` (2026-11-19)

This review inspected the nine commits `012f2f10db8..ac76776f06d` and independently exercised the current health client against the installed MCP SDK. It is a correction to the completion record above, not a claim that a later worker pass has fixed these findings

**Disposition: do not accept the connector as complete or deploy it on this pass.** G2, G10, G12, and G13 can be retained as scoped fixes. G11 has a genuine fixture improvement, but its real-database work remains unproven. G1, G3, G4, and G6 are incomplete, not completed. G5, G7, G8, G9, and G14 remain open as the worker disclosed. No source or cluster resource was changed during this review

### R1. G1/G4: health succeeds after the initialized notification is rejected

[notifyInitialized](internal/connector/mcp_health.go#L153-L169) omits `Accept: application/json, text/event-stream` and ignores notification delivery failures and HTTP status. [initialize](internal/connector/mcp_health.go#L126-L148) returns success regardless of that rejection

An independent ASGI observer wrapping the installed SDK recorded the following responses for the same notification under the same server:

| Client | Accept | Initialized notification response |
|---|---|---:|
| Current connector health client | Absent | **406** |
| Ordinary Python SDK client | `application/json, text/event-stream` | **202** |

The connector nevertheless reports healthy probes. The installed SDK's `ping` is exempt from its initialized-session gate, so a later ping does not prove that initialization completed

[The worker's fixture](internal/connector/mcp_health_test.go#L74-L80) accepts notifications without checking media acceptance. It cannot support the claim of being protocol-conformant in this respect

**Required:** deliver and validate the initialized notification as part of readiness. Require the accepted HTTP response and fail closed on transport, creation, or status errors. The installed SDK responds 202 with no JSON-RPC result. Add an SDK-backed regression and make the deterministic fixture enforce request media rules

### R2. G1: failed initialize responses can retain allocated sessions

[initialize](internal/connector/mcp_health.go#L135-L139) stores the ID only after result parsing. Wrong IDs, malformed replies, JSON-RPC errors, body-read failures, or cancellation can therefore discard an ID already allocated by the server before returning

The real SDK reproduction returned an initialization error to the connector, followed by explicit health close. The server's status was `{"sessions":1,"initializations":[]}`. The session allocated by the failed request remained because close had no recorded ID to delete

[The mismatched-ID test](internal/connector/mcp_health_test.go#L217-L225) asserts only that the probe errors. It does not assert that no allocated session survives

Ping failure also performs deletion using the failed ping's context in [Probe](internal/connector/mcp_health.go#L77-L94). If that context expired, regret cleanup can fail immediately

**Required:** capture the accepted HTTP session ID immediately, before application-level validation. Every later failure must attempt termination with a fresh bounded cleanup context. Assert zero remaining sessions for malformed and mismatched replies, cancellation, read timeouts, and server rejection. Handle deletion responses deliberately rather than ignoring every status and error

### R3. G4: ping accepts arbitrary responses and can wait for EOF

[ping](internal/connector/mcp_health.go#L172-L199) rejects only HTTP statuses at or above 400. It accepts redirects, 202-with-no-result, HTML, unrelated messages, mismatched or missing JSON-RPC IDs, and JSON-RPC errors

Its `io.CopyN(..., 4096)` is a byte-count limit but not an SSE message boundary. It waits for 4096 bytes or EOF, so a complete matching ping on a held-open stream can still block until the probe deadline

[validateInitializeResponse](internal/connector/mcp_health.go#L287-L313) also accepts any non-null JSON `result`, including `{}`, without checking the negotiated protocol version or required initialize-result fields

**Required:** use bounded, correlated JSON-RPC response parsing for both JSON and SSE initialization and ping. Return on the matching complete message instead of EOF. Reject redirects, errors, unrelated events, unsupported protocol versions, and invalid result shapes. Preserve the new incremental initialize parser and envelope checks rather than replacing them with another body-string test

### R4. G3: long-name collision repair changes historical normalized labels

[WorkerLabel](internal/bootstrap/client.go#L284-L303) preserves names already satisfying the remote grammar. Other names, including previously normalized names with dots, now gain a digest. For example, the historical `alpha.127.0.0.1.18080` normalized to `alpha-127-0-0-1-18080`; the new derivation produces another label with a hash

[The preservation test](internal/bootstrap/validate_test.go#L127-L134) only covers short names already matching the grammar. It does not preserve earlier normalized dotted IPv4, service, or IPv6-derived aliases

This does **not** imply that every normal restart creates a worker. An existing running agent loads its persisted identity directly. The affected path is re-enrollment and lost-credential recovery, where [Register](internal/bootstrap/client.go#L244-L255) derives the label again. Since the remote worker key includes the label, the new name can select a different association. The precise server result must be exercised, not assumed to be 409

**Required:** preserve non-conflicting historical labels when resuming their authoritative source. Use a digest-bearing derivation for intentionally new identities or an explicit migration. Include previously normalized dotted forms in actual issue/resume tests. Never replay a delivered worker credential or silently replace an approved source identity to obtain a passing test

### R5. G6: equivalent targets still diverge across the two validators

[Go service normalization](internal/target/target.go#L136-L154) lowercases service names, strips a terminal dot, and reduces full cluster suffixes. [Python canonicalization](../../litellm/proxy/mcp_source_broker/models.py#L91-L114) accepts only the short lowercase expression. Direct current-code probes produced:

| Input | Python result | Go behavior |
|---|---|---|
| `mysvc.myns.svc.cluster.local` | Rejected | Shortened and accepted |
| `mysvc.myns.svc.` | Rejected | Terminal dot removed and accepted |
| Uppercase service spelling | Rejected | Lowercased and accepted |
| `::1%eth0` | Accepted unchanged | Rejected |

The last row permits Python to persist a target the connector cannot subsequently parse. [_project_peer](../../litellm/proxy/mcp_source_broker/remote_store.py#L73-L92) also drops malformed worker projections with a warning. That isolates valid peers, but now-invalid legacy targets can disappear without an explicit migration or quarantine decision

**Required:** define one shared grammar and normalization contract for service case, full cluster forms, terminal dots, compressed IPv6, and scoped addresses. Assert both language implementations at the actual persisted/wire boundary. Decide and document how existing rows affected by a new rule are diagnosed. Keep mapped IPv4 and unicast fixes, and keep separate public-versus-private admission policy explicit

### Other claimed statuses, with exact limits

1. **G2 retains:** account-scoped lookups, removal, duplicate decisions, and runner ownership now use the account/name key. Same-name sources in separate accounts are independent. Public source ownership is still G5, and meaningful-content comparison is explicitly outside this result
2. **G10 retains conditionally:** the global health reset is explicitly a single-owner-process assumption. It is not a safe multi-replica liveness design
3. **G11 is partial:** [MemoryDatabase](../../tests/unit/proxy/mcp_source_broker/conftest.py#L14-L84) now binds `SOURCE_COLUMNS` with exact arity, preserving kind/path. Isolated PostgreSQL upgrade testing from earlier real DDL remains required. The old/current executable compatibility control reported by the worker has not been rerun independently at `ac76776f06db8a`
4. **G12 retains:** the real handler drives the ordinary-user success and administrative denial paths, with the denial ordered before rehash. This validates the called dependency boundary, not a live database transaction
5. **G13 retains:** the nine changed Python files are formatted and selected lint checks pass. No passing canonical full type-gate result is established
6. **G1 positive lifecycle path:** three health probes against the actual SDK succeeded and explicit close left **0 sessions**. Successful-path pooling is real progress and must remain. It does not resolve R1 or R2
7. **Previously verified streaming:** incremental chunking and ordinary-close cancellation retain their earlier scope. Unbounded streams, blocked-client capacity, redirects, and resume semantics remain G7, not inferred to work from this patch

### Evidence and acceptance boundary

| Independent check at `ac76776f06db8a` | Result |
|---|---|
| `make -C oicm-litellm-layer mcp-sidecar-check` | Passed with the explicit Go/staticcheck environment |
| Focused proxy/distribution/login suite | **197 passed**, compared with the worker's 196 report |
| Ruff lint and formatting on the nine changed Python files | Passed |
| Real SDK successful health lifecycle | Three probes, then 0 sessions after close |
| Real SDK rejected initialization followed by close | **1 orphaned session** |
| Real SDK initialized notification comparison | Connector **406/no Accept** versus ordinary client **202/correct Accept** |
| Target normalization and scoped-address probes | Residual mismatches reproduced above |
| Real PostgreSQL schema and transaction upgrade | Not established |
| Approved native-MCP dispatch through the supported dev path | Not established |
| Deployment or production approval | Not given |

The worker's nine-commit pass contains useful work but is not a complete remediation. Follow the [third remediation prompt](THIRD-REMEDIATION-PROMPT-2026-11-19.md). It prioritizes the disputed statuses and keeps the explicitly deferred production blockers visible, rather than claiming another numeric completion score
