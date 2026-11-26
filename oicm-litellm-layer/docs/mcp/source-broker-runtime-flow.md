# Steps 3 to 5: dial home, admit, probe, dispatch, reconcile

This file covers everything after the binary is installed and a source is registered.

## Step 3: the connector dials home and is admitted

### 3a. One outbound stream per source

- Code: `internal/connector/tunnel.go`, function `runTunnelConnector` (per source), started by `connector.RunAll` from `main.go` `runConnector`.
- One agent process serves **every** registered source across **every** account, each as its own independent tunnel. A revoked or failing source restarts alone.
- Target: `DialTarget(accountURL)`. That helper exists because passing the raw URL with its scheme into `grpc.Dial` left `https://` in the address, gRPC could not parse it, and every attempt died on the 15s dial timeout with no server-side log. It joins host and port and defaults to 443.
- Production path: `https://litellm.ecouncil.ae:443` into nginx, matching the tunnel Ingress, to worker `:8093` over h2c.
- Dev path: `MCP_SOURCE_LINK_TUNNEL_URL=https://localhost:18094` into `grpc_tls_proxy.py`, which needs ALPN h2, then an 18093 port-forward to the worker `:8093`. The plain `tls_proxy.py` on 14443 has no ALPN and cannot carry gRPC.

### 3b. Identity on the stream

Metadata on `TunnelService/OpenReverseTunnel`:

| Key | Value |
|---|---|
| `x-source-member` | worker id |
| `x-source-generation` | generation number |
| `authorization` | the worker credential |

`authStreamInterceptor` validates before the tunnel establishes:

1. the peer exists in cached state,
2. `phase == "active"`,
3. `generation` matches the stored generation,
4. no other live session already holds this worker id,
5. `SameSecret(storedDigest, presented)` in constant time.

Any failure is `Unauthenticated`, and the connector backs off and retries with jitter.

### 3c. Admission handshake

- `onOpen` pushes one `Admit` RPC down the fresh tunnel with the broker's own capability list.
- The connector answers with its capabilities: `execute`, `execute_stream`, `probe`.
- Those are stored on the `Member`. Every later dispatch is gated on the name being present, which is how an old connector degrades instead of breaking.
- On success the log line is `tunnel admitted worker=... source=... generation=... capabilities=[...]`.
- Then `pool.AddSession(member)` makes it visible and `go probeLoop(member)` starts.

### 3d. Probe loop

- Code: `internal/tunnel/tunnel.go`, `probeLoop` and `Member.probe`.
- Every 2s: generate a fresh 32-byte nonce, `Probe` with it.
- OpenAPI source: connector fetches `/openapi.json`; the worker canonicalises the bytes, verifies any `X-Source-Signature` against the pinned key, and derives a fingerprint. A fingerprint equal to `expected_catalog` is the proof.
- MCP source: no OpenAPI document; the probe only confirms reachability and returns a nil fingerprint. `provenCatalog` accepts a nil fingerprint for MCP.
- The result goes to the control plane: `POST /v1/source-link/report` with `{worker_id, generation, catalog_fingerprint?}`.
- Admitted means `lastProbe` is set. Not admitted clears it. A transient probe failure retains the last verified catalog for `HealthGracePeriod` (30s) rather than instantly evicting.

## Step 4: a client calls a tool, the call rides the tunnel

### 4a. Client to gateway

- The client calls the gateway with its own key. Nothing new; this is the normal LiteLLM MCP surface.
- The gateway looks up the source's MCP server row. The row's `url` is the broker data plane at 8888.

### 4b. Gateway to worker

- Arrow A2. The gateway forwards to `http://litellm-mcp-broker...:8888/sources/<source_id>` with `Authorization: Bearer <service-token>`.
- The worker's `pool.Forward` first checks `SameSecret(tokenDigest, bearer)`; failure is 401.

### 4c. Choosing a member

- `eligible` (one shared predicate, `internal/pool/eligibility.go`):
  exists, `active`, not fenced, catalog proven, probe fresh, `execute` capable.
- Stateful rule: if the request has `Mcp-Session-Id`, `Last-Event-Id`, or an SSE `Accept`, and more than one member is eligible, return 409 rather than mis-route. With exactly one member it is unambiguous.
- Multi-member write rule: more than one eligible and the method is not GET/HEAD, return 409, because a write needs an explicit tool policy the tree does not have yet.
- `pick`: fewest in-flight, round-robin on tie, increments in-flight.
- `DispatchInFlightCap = 0` disables the old per-connector ceiling of 64. Everything multiplexes in one HTTP/2 stream, so a held request costs a goroutine and a flow-control window.

### 4d. Forwarding and stripping

- `forwardOnce` builds `httputil.ReverseProxy` with the `Member` as its `RoundTripper`.
- Outbound header allowlist only: `Accept`, `Accept-Encoding`, `Content-Type`, `Mcp-Session-Id`, `Mcp-Protocol-Version`, `Last-Event-Id`, `If-None-Match`, `If-Modified-Since`. Everything else is dropped, which is why the origin reports `credentials_seen=false`, `cookies_seen=false`, and sees no internal nonce.
- Inbound: strip `Set-Cookie`; drop `Location` on any 3xx because a redirect usually points at a private loopback origin, which either leaks internal topology or sends the client somewhere unapproved.
- Path safety: `CleanSourcePath` rejects anything that could escape the source root or smuggle a second origin.
- Unsupported: `Upgrade` and `CONNECT` are refused 400.

### 4e. Down the tunnel

- `Member.RoundTrip` converts the request to a `DispatchRequest` and picks the RPC:
  - `execute_stream` capable: `ExecuteStream`, a server-streaming RPC. First message carries status and headers, later messages carry body chunks, so an SSE or streamable-http response reaches the client as the origin produces it. `ResponseDeadline` bounds only time-to-headers; once headers arrive the stream lives until the body closes, and closing the body cancels it.
  - otherwise: unary `Execute` with `ContentLength`, fully buffered. It exists so an older connector still works.
- Body limits are shared through `internal/wire` so both sides enforce the same numbers; oversize is `ResourceExhausted`.

### 4f. At the origin

- `Server.Execute` / `Server.ExecuteStream` on the connector opens one HTTP round trip to the origin with the target's authority as Host, reads or streams the response, enforces the body limit, and answers back up the same stream.
- `CloseHealthSession` deletes any open MCP health session so a removed source leaves no orphan upstream session.

## Step 5: keeping it honest

### 5a. Reconciliation

- `reconcileLoop` in `main.go`: every 2s, `state.Refresh` reloads peers from the broker, then `sessions.Reconcile` closes any session whose peer vanished, was revoked, or was fenced by a generation change.
- Fencing is also synchronous in `pick` and in `admitted`, so a stale session cannot serve even between reconciles.

### 5b. Health reset on startup

- The Python broker calls `store.remote.reset_health()` at startup, clearing both content proof and timestamps. The intent is that a restart never inherits a stale "healthy" claim. This is why a fresh tunnel must probe before serving.

### 5c. Revocation

- Admin revokes a worker: terminal by design.
- The broker calls the worker's loopback `POST /v1/source-link/revoke`, which closes the member and reconciles.
- A revoked or draining worker refuses to reconnect.
- `adma source remove` only forgets the source locally, and says so. The gateway registration is unchanged.
- Recovery means enrolling a fresh source with a new generation. The design decision is that automated replace is not built, because coupling revoke to credential minting widens the attack surface at the worst moment.

### 5d. The failure contract

| Condition | What the caller sees | Where |
|---|---|---|
| no eligible member | `503 no eligible connector` plus `Retry-After: 1` | `pool.Forward` |
| stateful request with 2+ members | `409 stateful mcp routing requires a single connected member` | `pool.Forward` |
| multi-member non-GET | `409 multi-member write routing requires an explicit tool policy` | `pool.Forward` |
| origin refused | `502 source upstream unavailable` | proxy `ErrorHandler` |
| bad service token | `401 unauthorized` | `pool.Forward` |
| bad connector credential | gRPC `Unauthenticated` | `authStreamInterceptor` |
| bad path, Upgrade, CONNECT | `400 unsupported source request` | `pool.Forward` |
| wrong generation or revoked | session closed on next reconcile or pick | `Fenced` |

## Ports for this whole flow, restated

| Hop | From | To | Port | Protocol |
|---|---|---|---|---|
| agent tunnel | agent | nginx | 443 | gRPC |
| nginx tunnel | nginx | worker | 8093 | h2c gRPC |
| client API | client | nginx | 443 | HTTP |
| nginx API | nginx | gateway | 4000 | HTTP |
| tool dispatch | gateway | worker | Service 8888 to container 9092 | HTTP/h2c |
| worker refresh and report | worker | broker | 127.0.0.1:9091 | HTTP |
| worker revoke receive | broker | worker | 127.0.0.1:9092 | HTTP |
| gateway and broker state | gateway, broker | postgres | 5432 | SQL |
| artifacts and releases | gateway, broker | rustfs | 9000 | S3 |
| agent to origin | agent | origin | user-chosen | HTTP |

## Files referenced in this step

- `oicm-litellm-layer/mcp-source-link/internal/connector/tunnel.go`
- `oicm-litellm-layer/mcp-source-link/internal/connector/connector.go`
- `oicm-litellm-layer/mcp-source-link/internal/connector/mcp_health.go`
- `oicm-litellm-layer/mcp-source-link/internal/tunnel/tunnel.go`
- `oicm-litellm-layer/mcp-source-link/internal/pool/eligibility.go`
- `oicm-litellm-layer/mcp-source-link/internal/pool/forward.go`
- `oicm-litellm-layer/mcp-source-link/internal/pool/registry.go`
- `oicm-litellm-layer/mcp-source-link/internal/control/control.go`
- `oicm-litellm-layer/mcp-source-link/internal/wire/wire.go`
- `oicm-litellm-layer/mcp-source-link/dispatchpb/dispatch.proto`
- `oicm-litellm-layer/mcp-source-link/main.go`
- `litellm/proxy/mcp_source_broker/remote_store.py`
- `litellm/proxy/mcp_source_broker/remote_gateway.py`
- `oicm-litellm-layer/docs/mcp/source-broker/running-system/README.md`
