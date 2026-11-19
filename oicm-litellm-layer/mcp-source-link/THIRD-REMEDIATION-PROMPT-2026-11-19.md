# Third connector remediation prompt

## Copy this task as the worker instruction

You are working in `/home/jyao/ADEO/litellm`, branch `jya0-v1.108.5`. The review head is `ac76776f06db8a`. Your earlier completion record overstates G1, G3, G4, G6, and G11. The required work is not another test-count exercise. Correct concrete defects at their owning application boundaries and show failures become safe outcomes under mutation and protocol testing

Read the full current behavior before editing, including the independent reassessment appended to [FOLLOWUP-GAPS-2026-11-19.md](FOLLOWUP-GAPS-2026-11-19.md). Follow the surrounding engineering conventions, but present your plan before changing code. State the assumptions about target admission and legacy-data compatibility rather than silently choosing new policies

Read the OICM navigation in `oicm-litellm-layer/mkdocs.yml`, then `oicm-litellm-layer/docs/docs-map.md`, then the owning connector/broker docs before public-contract or deployment decisions. Do not alter the running gateway, deploy an image, create a public ingress, or resurrect `reverse-h2`. Keep `grpctunnel` and generation-scoped HMAC worker credentials. Do not deploy for this work

Protect all unrelated local changes, including the currently modified dev overlay and untracked paths. Pull before starting. Keep source commits separated from documentation, never include temporary probes or credentials, and commit and push completed work as expected by these instructions

## Required delivery order

Finish Task 1 completely before declaring native-MCP health implemented. Then complete Tasks 2 and 3. Only afterward address remaining Tasks 4 through 7, or state precisely why they remain outside this pass. Do not spend this pass on unrelated gateway styling or tooling

The retained account-scoping, admin-before-rehash, source-column, formatting, and basic streaming improvements must not be rolled back. Preserve successful session pooling, incremental initialize parsing, mapped IPv4 canonicalization, and worker/source target equality while fixing the incomplete behavior

## Task 1, P0: make health readiness and regret cleanup an MCP lifecycle, not an HTTP status check

### Reproduction that currently fails against the installed SDK

1. `notifyInitialized` sends no `Accept`. The actual SDK responds **406** to the connector, versus **202** for an ordinary SDK client with `application/json, text/event-stream`. The connector ignores this failure and returns healthy

2. An initialize response with `{"jsonrpc":"2.0","id":"other","error":{"code":-32601,"message":"wrong id"}}` produces a connector error after the SDK allocated a session. Current `initialize` has not stored the returned `Mcp-Session-Id`. `CloseHealthSession` cannot delete it. The independent result is one retained session after failure and close

3. `ping` accepts response codes below 400 and discards the body. A redirect, 202 with no result, HTML body, mismatched ID, or JSON-RPC error can therefore be reported healthy. A ping response delivered in a complete SSE event but held open can stall on `io.CopyN(..., 4096)` waiting for the size limit or EOF

4. Any non-null JSON `result` passes initialize shape validation. It need not contain an agreed protocol version or required result shape

### Behavior that must exist

- Keep one private health-client state machine per source. Never share it with unrelated gateway MCP client sessions
- When initialization is admitted by HTTP, capture a supplied `Mcp-Session-Id` immediately. Validate and cache it in a form all failure branches can reach
- On a JSON-RPC, schema, transport, cancellation, or version failure after allocation, attempt `DELETE` with a **new bounded cleanup context independent of the failed request context**
- `notifications/initialized` carries `Content-Type: application/json` and `Accept: application/json, text/event-stream`, with the allocated session ID. Request creation, transport, or response-status failure makes the probe unhealthy. Read the notification response according to the installed contract: the SDK returns 202 Accepted and no JSON-RPC result. Do not infer success from sending bytes or from a later ping
- A session cleanup does not silently redefine a failed initialization as healthy. Distinguish unavoidable remote cleanup failure, expose it at the health/logging boundary, and bound it
- Read initialize and ping replies incrementally for `text/event-stream`. Accept JSON finite bodies where valid. Correlate JSON-RPC 2.0, request ID, absent error, and result. Return at the matching completed message without waiting for EOF. Keep a real aggregate byte limit, event-size limit, cancellation behavior, and response closure
- Reject redirects rather than following or interpreting them as MCP health. Do not accept HTML, arbitrary 2xx bodies, empty required results, mismatched IDs, or another request's events
- Validate the initialize-result shape and negotiated MCP version using a protocol authority. Decide and document unsupported-version handling. Do not copy arbitrary fields into health state without purpose
- Session ID absence, no-session server support, and expiry must follow an explicitly supported mode, not an accidental perpetual `initialize` every probe with no cleanup
- Normal connector shutdown keeps its independent bounded deletion behavior. Repeated stale-session recovery and reconnects must retain at most the one intended health session per source
- Remove or use `initializ`. Readiness must reflect a completed handshake, not a boolean written but never checked

### Tests that must fail before and pass after

Use the installed Python MCP SDK in a temporary local fixture for the live lifecycle, plus deterministic connector tests. Do not check in a fixture that claims protocol conformance while ignoring request acceptance rules

The SDK-backed checks must cover:

1. Three health cycles, successful initialization, bounded session count, correct initialized-state acceptance, normal client list-tools and call-tool on the same server, and final 0 health sessions after close
2. Wrong JSON-RPC ID, application JSON-RPC error, malformed result, body-read failure, and canceled initialize. Each asserts no allocated server session survives cleanup, not only that `Probe` returned an error
3. The notification's actual `Accept` header and accepted HTTP response are exercised. Rejection produces unhealthy state and cleanup, never a cached healthy state
4. `ping` rejects HTTP 3xx, 202-without-result, HTML, wrong ID, absent ID, and JSON-RPC error. Matching JSON and held-open SSE replies succeed before EOF
5. Unsupported negotiated version and malformed initialize result are refused without leaked sessions
6. Failed ping cleanup does not use the already expired context. Injected transport failure is observed, bounded, and cannot keep a dead session pinned forever

For each mutation that removes header validation, delays ID storage, skips cleanup, ignores result correlation, or reads a held-open ping to EOF, demonstrate the intended test failure. A test that merely asks the worker's fake to return the same finite body is insufficient evidence

## Task 2, P0: preserve historical worker identity during reregistration

Add the new digest-bearing labels for genuinely new nonconforming long names, but preserve the deterministic historical label during recovery or idempotent re-enrollment of its original authoritative association. At minimum test the old `alpha.127.0.0.1.18080` to `alpha-127-0-0-1-18080` case plus historical dotted service and IPv6-derived aliases

Do not claim an ordinary restart changes a worker when the runtime loads persisted identity directly. Prove the reregistration/lost-credential path: `Register` recomputes a changed label, and remote workers are keyed by `(source_id,label)`. Exercise actual issuance and lost-credential resume with `delivered_at` unset, plus the separate delivered-worker rejection path. Assert which identity the server selects. Do not assert 409 unless observed; an observed new association is also a defect

A compatibility implementation must not return the same historical label to two formerly colliding long identities. If distinguishing old/new derivation requires migration state, add a deliberate compatibility decision, not a heuristic that creates duplicate identities on every call

Required regressions:

- All previously working nonconflicting normalized aliases continue their own resume association
- Full-length IPv6 and long service aliases with a common truncation prefix still get distinct grammar-valid new labels
- Remote issue then resume preserves source ID, worker ID, generation, policy target, and delivery restrictions
- A delivered credential cannot be replayed. An explicitly initiated replacement follows the generation-fenced lifecycle, not a disguise around existing credential replay
- Carry label behavior through the remote store path rather than testing only strings

## Task 3, P0: make Go and Python accept and persist identical target semantics

Choose and document one policy across these exact inputs:

- `svc.ns.svc`
- `svc.ns.svc.cluster.local`, `svc.ns.svc.local`, and any suffixes already supported
- Uppercase and lowercase spellings
- Terminal-dot spellings
- Expanded and compressed IPv6, bracketed host:port handling, and mapped IPv4
- Scoped IPv6 such as `::1%eth0`
- Unspecified, multicast, broadcast, link-local, and any deliberately admitted public or private address class

Do not imply public addresses are denied in both directions today. Present the explicit policy decision and preserve it consistently if admitted

Either implement a single code-generated/spec-pinned shared grammar or assert direct behavioral parity at the actual Python insert and Go parse/wire boundary. Every value accepted by Python must have the exact canonical host/port accepted by Go. Every Go spelling accepted as equivalent must persist one source identity in Python rather than triggering a rejected read or different association

Define legacy-row behavior when an earlier stored value violates new normalization. `_project_peer` dropping invalid rows prevents cross-peer denial of service, but silently disappearing a migratable worker is not an acceptable default without an explicit quarantine/migration contract. Prove a valid peer remains available while the affected row reports its documented state

Regression examples must include the four direct current mismatches: full cluster service, terminal dot, uppercase service, and scoped loopback IPv6. Mutations changing case folding, suffix handling, zone acceptance, or canonical equality must fail those tests

## Task 4, P0/P1: use authenticated-owner source identity, not local alias authority

This is original G5 and remains open. Separate local display/account alias from gateway-authenticated owner, public alias, target association, and issued worker identity. Persist authoritative enrollment results only after appropriate validation. Never expose another owner's source or credentials through duplicate/conflict handling

Required behavior at direct client plus second-client harness:

- Same actual owner and target with different local aliases resolves one reviewed association, or produces a clearly intentional alternative rather than accidental duplicate approval
- Different real owners who each call themselves `alpha` and use the same target get independent source associations and public aliases
- Equal new content is idempotent, but changed kind/path/target/spec does not silently adopt an earlier approval
- Show returned public identity and record it alongside the original authoritative owner in state. Do not overwrite an existing global test registration for a passing result

Keep this task independent from reverting the local `SourceKey{Account,Name}` correction. Local isolation remains necessary even after remote identity changes

## Task 5, P0: make interrupted enrollment and replacement recoverable

Deliver original G8 and G9, not database-free mock claims

- Add durable recovery state and bounded compensation for source persistence, OpenAPI artifact publication, one-owner review registration, worker response, and local metadata persistence
- Retry after each injected failure collapses to one complete reviewed source association and one intended worker generation. Define safe behavior if content was already approved or rejected; do not overwrite it silently
- Add selected-account/source revoke, replace, and reinitialize semantics. Gate remote operations using current generation fencing and preserve worker credentials' one-use delivery restrictions
- Retry independent failures between remote revoke and local removal/replacement until convergence. No old generation can resume after replacement
- Define migration for the legacy single `identity.json` state, even if that choice is an explicit actionable command/error. Existing sources cannot silently become unusable
- Remove selected local metadata only through the defined successful transition; account removal cannot affect another account's source

Run isolated real PostgreSQL upgrade testing from the previous `OicmMcp*` DDL. `CREATE TABLE IF NOT EXISTS` and fake dictionaries do not establish constraints on a preexisting table. Run concurrent duplicate enrollment/recovery and assert constraints and transaction results, without modifying unrelated rows or requiring production access

The global health reset may remain only while deployment is explicitly one owning control process. If multi-replica support is adopted, persist process/boot ownership rather than one replica erasing another's health

## Task 6, P1: finish the MCP path, streaming, and multi-member contract

Complete original G7 and relevant original G14 behavior at the direct and relayed surfaces

1. Preserve enrolled non-default MCP paths through `route_source`; test `/custom/mcp` for initialize, tools/list, tools/call, ping, notification, and DELETE. Do not make a custom path behave exactly as `/mcp` only because the fixture ignores its route
2. Define total connection/header/idle/capacity/termination semantics. Preserve valid long-lived idle SSE but bound stuck origins/streams and connection/goroutine occupancy. A blocked/slow caller closing must release actual source resources. Test concurrent blocked clients and bounded occupancy
3. Support validated same-origin relative redirects such as `/mcp` to `/mcp/` without exposing private topology. Reject or clearly diagnose cross-origin redirects as the selected policy dictates. Never expose a private `Location` or let the gateway dial the private redirect itself
4. Exercise Python SDK stream reconnect using `Last-Event-Id` and session termination. Do not classify a body being flushed once as protocol correctness
5. Replace implicit `Accept` and all-non-GET multi-member heuristics with explicit source-kind/session/member policy. At supported single-member MCP semantics, reject an actual excess ambivalent member request at the boundary, never accept-then-409 after allocation where avoidable
6. Retain old-connector unary fallback as explicitly bounded legacy behavior. Verify current connectors do not rebuffer the entire streamed body. Keep the additive protocol compatible; do not rename fields or remove the unary RPC
7. Canonicalize OpenAPI across Go/Python including Unicode, escapes, and numeric equivalents. Enforce the actual tool builder's supported schema and document any unsupported constructs instead of allowing gateway registration to fail later

## Task 7, P1/P2: finish installation, transaction, and runtime integrity

Complete the meaningful remainder of original action items/deviation, not superficial cleanup

- Validate `release.json` version/metadata/digest before activation. Publish immutable artifacts atomically, including constrained deletion, package staging, and idempotent rerun recovery
- Drive actual CLI noninteractive flows. Reject incomplete/conflicting input before writes, propagate context into retry loops, stop retrying terminal validation errors, and reconcile pre-existing partial credentials/state. Scope locks by the correct user/state directory, clean temporary secret files on all exits, and avoid printing key material
- Make reload runnable-state/restart decisions depend on actual started `Run` success, not log intent alone. Include source ID and resolved gateway in the connection fingerprint. Preserve same-name multi-account independence while stopping/restarting one tunnel, and define failed/gone tunnel completion behavior. Bound the existing `RunTunnel` shutdown path as well: connection close and idled origin/client transports must not prevent a canceled source from reaching transport cleanup and health-session deletion indefinitely
- Prevent duplicate daemon processes or stale PID signaling with a correct locked identity, not a predictable file existence check
- Replace duplicate configuration and stale legacy instructions/APIs. Retain authoritative `grpctunnel` dev operations. Do not reintroduce `reverse-h2`
- Do not alter public ingress assumptions. Dev REST runs through an explicitly authorized port-forward, not the production host presumed to reach the dev gateway

## Required proof, evidence discipline, and delivery report

Run and preserve the actual output of:

```sh
make -C oicm-litellm-layer mcp-sidecar-check
make -C oicm-litellm-layer mcp-source-link-compat
.venv/bin/python -m pytest -q \
  tests/unit/proxy/mcp_source_broker \
  tests/unit/proxy/client_distribution \
  tests/unit/proxy/auth/test_cli_password_login.py
```

Run the repository's normal focused lint/format/type gates with existing dependencies where possible. If you change an explicit gate command, show the invoked form. Report dependency/toolchain or environment failure as no result, not as alignment with a baseline

Provide an evidence table per task with:

- Defect before your patch, including a reproducible response/state observation
- Owning production file(s) and precise behavior correction
- Exact focused test command and output
- Consequential mutation tested and why the test failed with it
- Real SDK or real PostgreSQL result where required
- Any remaining deferred behavior, without transferring it into the fixed column

Update the completion statements in the existing follow-up document to match actual validated behavior. Do not count G1/G4/G3/G6/G11 as complete when only their subset passes. Do not paste large generated fixture code into the report, and never include credential values or real customer/company identities

Do not claim production readiness or native-MCP approval until the authorized proof below is complete. If this pass covers only local/real-SDK recovery work, say so clearly and stop after committing that validated work. No numeric summary such as nine-of-fourteen complete is useful unless every claimed item's boundary is genuinely tested

### Separate authorized dev proof, not permission to deploy automatically

Once local and durable backend gates pass, ask before rollout. Use the documented dev path (`kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000` or current owning-document equivalent) rather than public ingress. Existing local deployment tests do not establish that a named image was built, signed, deployed, or admitted

The subsequent proof uses two ordinary authenticated users, equal targets, isolated home directories, real private/service addresses, both non-default MCP paths and delayed SSE, then actual tool listing/call/streaming after the required review transition. Test recovery of one source while the other remains available. Record externally visible client commands, before/after session/resource outcomes, and authorized cleanup. No public dev tunnel endpoint and no `reverse-h2`

### Current review evidence to preserve

The independent checks at `ac76776f06db8a` passed full sidecar checks, 197 focused Python tests, and selected lint/format. Real SDK success-path health reaches 0 sessions after close. A rejected initialize leaves 1 session, and connector initialization notification gets **406** because it sends no Accept, while the ordinary SDK client gets **202** with `application/json, text/event-stream`. Target probes also confirm full-service/trailing-dot/uppercase rejection in Python versus accepted normalization in Go, plus Python acceptance of `::1%eth0` that Go rejects

These are the immediate bugs to reproduce before changing behavior. They are not hypothetical documentation improvements
