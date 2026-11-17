# Dev source broker

This dev slice exposes one approved OpenAPI service on a source host through the existing MCP gateway. The source host makes an outbound TLS WebSocket to the broker relay. LiteLLM authenticates and authorizes the normal MCP tool request before the private broker dispatches it to a healthy member

The dev overlay is `deploy/overlays/dev/mcp-source-broker`. It creates one auxiliary Pod with broker and relay containers. The gateway contains no relay and starts no connector. Production is not enabled by this slice

## Reverse-h2 transport (second data plane)

The same Go worker binary now carries a `reverse-h2` transport alongside the WebSocket remotedialer, selected by `MCP_REMOTEDIALER_TRANSPORT=reverse-h2` (default remains `remotedialer`). The connector dials one outbound TLS HTTP/2 session to the public gateway and keeps it alive; every forwarded request is an HTTP/2 stream on that session, so a held request costs stream state on the channel instead of a origin TCP socket per request. The protocol is a dispatch long-poll: the connector registers (`POST /mcp-remotedialer-reverse`), holds (`POST /mcp-remotedialer-hold`, the stream that carries liveness and generation fencing), polls for work (`POST /mcp-remotedialer-dispatch`, the relay answers one pending poll per forwarded request), and posts each origin response back (`POST /mcp-remotedialer-respond`). All four routes ride the reverse listener on port 8093 and are published through the same public host with the nginx `GRPC` backend protocol, which proxies cleartext HTTP/2 to the sidecar so stream multiplexing survives the ingress hop. Per-worker enrollment, catalog-fingerprint gating, generation fencing, revocation, and header stripping are identical across both transports; the dispatch queue is bounded and slow connectors shed to HTTP 503 rather than buffering unboundedly

## Live reverse-h2 verification (2026-11-16)

The reverse data plane was verified end to end on the dev cluster with a scratch origin fixture on the source host (fresh Ed25519 proof key, port 18084) and two enrolled workers (`reverse-h2`, `reverse-h2-b`) for source `f804e8c9-a9c7-5824-a90a-837262788be8`. All of the following were observed live, each through the public ingress `https://litellm.ecouncil.ae/mcp-remotedialer-*` (nginx to h2c `grpc_pass` to sidecar 8093):

- Registration and liveness: the connector registered, held its session, and the probe loop admitted it with `catalog_fingerprint` equal to the enrollment's expected fingerprint; the broker's peers view showed the worker `healthy`
- Auth gating: no-credential and wrong-credential calls to all four reverse routes returned 401, through the ingress and directly on 8093 (`curl --http2-prior-knowledge`, since the listener is h2c-only and correctly gives HTTP/1.1 an empty reply)
- Data plane: `GET /sources/{id}/source.json` and `POST /sources/{id}/echo.json?channel=...` through the private dispatch port returned origin data with `credentials_seen=false` and `cookies_seen=false`
- Concurrency: 8 parallel reads through one reverse session all returned 200 in under 0.62s wall
- Fencing and lifecycle: `revoke` locked the connector out immediately (re-register 401, dispatch 503 with `Retry-After: 1`); `drain` stopped dispatch; `recover` minted a new generation and the connector rejoined with a freshly derived generation-matching credential; duplicate registration for a worker with a live session was rejected with 401 while the original session kept serving
- User path: a virtual key scoped to the source's `mcp_access_groups` called `read_source` and `echo_source` through `POST /mcp-rest/tools/call` on the dev gateway and received origin data (`isError: false`), exercising gateway auth, MCP tool dispatch, the data-plane pool URL, the reverse long-poll, and the origin round trip

Two latent defects surfaced and were fixed during the live run. The worker-enrollment INSERT bound its `expires_at` parameter without a `::timestamptz` cast, so Prisma sent text and PostgreSQL rejected the insert (the enrollment route 500ed); both worker and enrollment inserts now cast explicitly. A pre-existing source row also carried the retired `chisel` value in `remote_transport`, which `selection()` correctly rejected; re-pointing the source with `PUT /mcp-remotedialer-sources/{id}/selection` `{"transport":"remotedialer"}` refreshed both the stored transport and the registered pool URL to the data-plane Service port

A follow-up review pass tightened the reverse-h2 protocol itself. A forwarded request now carries a 90-second response deadline, so a lost respond can no longer leave the caller hanging until the session dies. Oversize dispatch payloads fail fast with 502 instead of being silently truncated, and origin bodies are capped at 760 KiB so the base64-inflated respond envelope always fits the 1 MiB wire limit; before that cap, a just-under-1-MiB echo wedged the connector dispatcher permanently because the relay refused the oversized respond without draining it (the handler now drains rejected uploads). Framing headers (`Content-Length`, `Transfer-Encoding`, hop-by-hop fields) are stripped from relayed responses so truncated bodies can never corrupt response framing, and the connector treats a non-204 respond as a lost session. All of this was re-verified live after redeployment: 8/8 concurrent reads in 0.5s, POST with `credentials_seen=false cookies_seen=false`, 401 auth negatives, near-limit and over-limit echoes failing fast (502 in under 1s) with the connector surviving and the session still serving, revoke lockout, and the gateway MCP user path (`read_source`, `echo_source`) returning origin data through a scoped virtual key

## Enrollment

A proxy administrator posts a `SourceCreate` to `POST /mcp-sources`:


```json
{
  "owner": "source-owner",
  "alias": "private-openapi",
  "key": "admin-generated-at-least-16-chars",
  "expires_at": "2027-01-01T00:00:00Z",
  "target": {"host": "127.0.0.1", "port": 18080},
  "spec": "{\"openapi\":\"3.1.0\",\"paths\":{\"/source.json\":{\"get\":{}}}}"
}
```

The source target allows loopback only and requires an explicit local service. An ordinary source membership stays in the authenticated relay authfile even when unhealthy; quarantine prevents tool dispatch, not subsequent recovery. Administrators can retrieve the existing connector installer whenever the source has been catalog-quarantined

The source target is only a loopback host and port. Source identity is derived from `owner` and `alias`. The gateway saves the catalog object in the gateway-accessible artifact mirror, registers a stable private pool URL with LiteLLM, and grants that source its own MCP access group. Re-enrollment keeps its source, enrollment, connector lease, and relay credentials; it updates the target and expected canonical catalog fingerprint. It does not return another one-time token

The returned `install_path` is relative to the gateway. Fetch it with the one-time enrollment token only once:


```sh
curl -f "http://127.0.0.1:14002/mcp-source-broker/install/$ENROLLMENT_ID?token=$TOKEN" -o install-connector.sh
```

An authenticated owner or proxy administrator can retrieve the same persistent connector configuration without a token. The token is single-use and only its HMAC is saved. Treat the whole rendered installer as sensitive because it provisions relay credentials into a mode-600 environment file

Run the script as the ordinary user who owns the source service. It installs a Chisel binary downloaded through the HTTPS gateway, verifies its pinned SHA-256, writes `~/.config/systemd/user/mcp-source-connector.service`, and enables the user unit. It does not require `sudo`. Prerequisites are `curl`, `gzip`, `sha256sum`, user systemd, and the host's normal CA trust store

The broker control service is on `8089`, dispatch on `9090`, and Chisel relay on `8088`. Only the WebSocket relay and gateway ingress are public. Control and source dispatch require the independent broker service credential

## Calls


Use an ordinary virtual key authorized for the source-specific MCP access group. No Chisel installation is needed on the caller's machine. The gateway creates OpenAPI tools from the saved spec and dispatches them to the pool URL without a trailing slash


A healthy connector has a recent successful catalog probe and actual catalog fingerprint equal to the enrollment's expected fingerprint. Startup clears health timestamps. Different catalogs are quarantined rather than merged. The pool strips gateway authorization, cookies, and transport headers before forwarding. Retries are restricted to errors where the upstream connection was not established


The fixture proof can use this request with `READ_KEY`:


```sh
curl -fsS http://127.0.0.1:14003/mcp-rest/tools/call \
  -H "Authorization: Bearer $READ_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"server_id":"f804e8c9-a9c7-5824-a90a-837262788be8","name":"read_source","arguments":{}}'
```

The returned `CallToolResult.content[0].text` contains JSON from the source's `/source.json` endpoint. For an echo tool, use `{"channel":"dev-tool","body":{"text":"round-trip-through-chisel"}}` as `arguments`. Validate the actual source marker and that its `credentials_seen` and `cookies_seen` booleans are false


## Port map (canonical)

The broker Service `litellm-mcp-broker-dev` is the only gateway-facing address. Its named ports and the container listeners behind them:

| Service port | Service name | Container port | Listener | Who calls it |
|---|---|---|---|---|
| 8888 | `data` | 9092 (`rd-data`) | Go sidecar private dispatch: `/sources/{id}/{path}`, `/v1/remotedialer/revoke` | The gateway's MCP pool URL, `MCP_SOURCE_BROKER_DATA_PLANE_URL` |
| 8093 | `rd-reverse` | 8093 | Go sidecar reverse-h2 ingress `/mcp-remotedialer-{reverse,hold,dispatch,respond}` | Source connector, reverse-h2 transport (the only transport) |
| 9091 | `remote-control` | 9091 | FastAPI broker process (remote control) | Administrator via port-forward |

8888 is the standard gateway-facing data-plane port: the gateway's registered pool URL is always `http://litellm-mcp-broker-dev.adeo-litellm.svc.cluster.local:8888/sources/{id}`, from the `MCP_SOURCE_BROKER_DATA_PLANE_URL` env var. The connector routes ride the shared 443 ingress, unchanged. The NetworkPolicy matches pod ports after Service DNAT, so it keeps listing the container ports. The 8092 WSS listener and its `rd-worker` Service port and Ingress were removed on 2026-11-17; reverse-h2 is the only transport

## Verification


```sh
.venv/bin/python -m pytest -q tests/unit/proxy/mcp_source_broker
.venv/bin/python -m pytest -q oicm-litellm-layer/tests/deploy/test_dev_prod_parity.py
.venv/bin/python -m basedpyright litellm/proxy/mcp_source_broker
make -C oicm-litellm-layer litellm-src-release-dev
```

Build the auxiliary image from the exact running gateway digest after the gateway rollout completes. `LITELLM_SRC_BASE` explicitly selects that digest, so the same old base is not accidentally reused when the dev tag moves. The artifact sync checks the pinned checksum and gzip integrity. Its ignored `chisel.gz` belongs in the overlay directory, which is the complete Buildah context below. Keep `KUBECONFIG` pointing at the Al Ain kubeconfig for these commands


```sh
sh oicm-litellm-layer/scripts/sync-mcp-source-artifacts.sh
BASE=$(kubectl -n adeo-litellm get pods -l app=litellm-proxy-dev \
  -o jsonpath='{.items[?(@.status.phase=="Running")].status.containerStatuses[0].imageID}')
IMAGE=registry.adeoaiengine.ecouncil.ae/openinnovationai/platform/mlops/mlops-serving/litellm-mcp-broker:jya0-v1.108.5
podman build --pull=always --build-arg "LITELLM_SRC_BASE=$BASE" --tag "$IMAGE" \
  -f oicm-litellm-layer/deploy/overlays/dev/mcp-source-broker/Dockerfile \
  oicm-litellm-layer/deploy/overlays/dev/mcp-source-broker
podman push --tls-verify=false "$IMAGE"
kubectl -n adeo-litellm rollout restart deployment/litellm-mcp-broker-dev
```

Do not commit the downloaded `chisel.gz`. Its build input is checked by SHA-256 and gzip integrity. The existing gateway release workflow publishes only the dev overlay, never production

On 2026-11-13, the installed dev gateway and auxiliary image carried `read_source` and `echo_source` for source `f804e8c9-a9c7-5824-a90a-837262788be8`. The one-time enrollment claim minted connector `7f3a49c4-59a7-5611-8c4e-fff9b8a0e670` at `127.0.0.20:18081`. An ordinary same-identity re-enrollment refreshed the saved canonical fingerprint after catalog drift. It returned no new token or connector

An administrator retrieved the authenticated recovery script through both the public dev ingress and a local gateway forward. The identical script installed the connector as user `jyao` on `aitdev00`, verified Chisel `1.12.0`, wrote a mode-600 environment file, and generated a matching user systemd unit. No `sudo`, downloaded shell execution, disabled TLS verification, or temporary service command override was needed

A virtual key restricted to this source's MCP access group listed exactly `read_source` and `echo_source`. `read_source` returned hostname `aitdev00` and the persisted source marker. `echo_source` returned `round-trip-through-chisel` on channel `dev-tool`, with `credentials_seen=false` and `cookies_seen=false`. The same key received HTTP 403 for the other source and a non-admin key could not retrieve an installer. Direct dispatch without the service credential returned HTTP 401. An already-redeemed token was rejected, including the complete claim-rejection test in the unit suite

Local connector, dev auxiliary, and dev gateway restarts each recovered with the same source ID, connector ID, listener, and correct MCP responses. Control port 8089 and dispatch port 9090 were unreachable on the public host. Production workloads were not modified

The real-PostgreSQL store suite passed nine tests, including catalog-mismatch quarantine, owner recovery, and valid-fingerprint reactivation. Its temporary `postgres-regression` tenant rows were removed. For that opt-in developer test, forward only `svc/adeo-litellm-postgres-dev-rw` to local port 15432, build its `LITELLM_TEST_POSTGRES` URL from the existing dev credential in a protected temporary file, and unset the variable afterward. Run the store tests with `--confcutdir=tests/unit/proxy/mcp_source_broker` so ambient environment stripping does not select unrelated host fixtures. The normal test command keeps this database test skipped

Focused broker tests, deploy parity tests, strict Ruff, and basedpyright passed. The complete repository gates were attempted but did not pass: dependency resolution was blocked by the GitHub codeload DNS sinkhole, format checks reported inherited files, and default-branch discipline comparisons reported inherited findings. The strict Ruff worktree gate also lacked its expected configuration. These are not claimed as successful repository-wide checks

Multi-source host failover, native MCP catalog membership, authorization across source replicas, root/system connector installation, and multi-replica session ownership are not implemented or claimed by this dev slice. The README next to this document remains the earlier design proposal, not a release claim

## Next steps

The reverse-h2 transport is verified on dev as a single-replica data plane. Work is ordered by risk, and each step should carry its own live verification the same way this slice did. Revised 2026-11-17 after the service-topology review:

1. Service and port standardization. Both broker Services keep named ports, but the public data-plane entry is renumbered to the standard 8888 on the Service (gateway-facing dispatch), so no code or registration carries a magic number anymore: the gateway's pool URL moves into an overlay env var, the owning doc gets one canonical port map, and the parallel source-agent research relay is decommissioned once the broker path is proven (it has zero gateway references and doubles the review surface). The connector routes stay on the shared 443 ingress, unchanged. Status: DONE. The 8888 Service port, the `MCP_SOURCE_BROKER_DATA_PLANE_URL` env var, and the canonical port map above landed and were re-verified live; the research relay's Deployment, Service, Ingress, NetworkPolicy, secret, and overlay directory were all deleted on 2026-11-17 after the 8888 round trip passed (the install mirror still serves the signed source-agent release script, which is client-distribution surface, not relay surface)
2. Multi-host failover. Enroll a second source host with its own origin and proof key, run both connectors concurrently, and verify request distribution and failover when one host stops. The pool already prefers least-in-flight members, but no test has exercised two live reverse sessions from different hosts. Status: DONE (2026-11-17). Two workers (`failover-host-a` on origin port 18084, `failover-host-b` on 18085, each with its own Ed25519 proof key and dataset) held concurrent reverse sessions; 12 dispatch calls split exactly 6/6 across markers; killing host A's origin moved all 12 subsequent calls to host B with zero failures while A's connector kept its session (the origin refused, dispatch shed, no poisoning); after one probe cycle the broker marked A `healthy_at=NULL` and excluded it from dispatch (8/8 to B); restarting A's origin re-admitted it within one probe cycle and distribution rebalanced 5/5 over 10 calls
3. Load profile. The Pass 3 feasibility control held 100k SSE streams on one h2c connection at 610 MiB with a raw prototype; the shipped long-poll design has not been loaded past tens of concurrent requests. Run the Pass 3 harness against the dev deployment and record streams, memory per connector, and p95 dispatch latency, so the architecture claim is measured on the real protocol. Status: DONE (2026-11-17). Measured on the dev deployment through Service port 8888 with one reverse-h2 connector (`load-host`, uvicorn origin fixture on the source host) and a driver inside the broker pod, 10s runs, GET `/source.json`: 50 RPS 500/500 p50 77 ms p95 269 ms; 100 RPS 1000/1000 p50 553 p95 849; 250 RPS 2500/2500 p50 563 p95 855; 500 RPS 5000/5000 p50 538 p95 798; 1000 RPS 9998/10000 (two 502 origin timeouts) p50 546 p95 802 p99 951. The Python-thread driver saturated at 2000 RPS offered, so the true per-connector ceiling above 1000/s is not established (same driver caveat as Pass 3). Held streams: 32 and 64 concurrent 3s-held calls all 200; 128 concurrency sheds exactly at the designed 64 in-flight cap (64 x 200 + 64 x 503 with `Retry-After`), and memory was flat across the sweep within sampling noise: connector RSS 19.0-19.2 MiB, sidecar RSS 8-13 MiB (metrics-server), sidecar CPU 70-73 milli-cores at 500 RPS. Sustained overload wedges the single-process origin fixture, not the data plane: dispatch sheds 503 with zero memory growth and no session loss, drain is bounded by the 8 concurrent origin round trips per connector, and full recovery was verified after rest (200 RPS 1600/1600). The architecture claim is measured on the shipped protocol: a held request costs pending-queue entry plus h2 stream state, not a socket or buffer growth. In passing, two connector processes sharing one config re-confirmed duplicate-session rejection: the winner serves, the loser gets 401 with capped backoff
4. Session and connector ownership. The dispatch long-poll ties a session to one pod; a multi-replica broker needs pinned placement plus fan-out or a shared queue. The debate, positions, and resume conditions live in `MULTI-REPLICA-DESIGN-DEBATE.md`; do not redesign here, and decide only when a replica increase is actually demanded
5. Recovery tooling. After `revoke`, a worker is terminal and a new label must be enrolled by hand. Decide whether an admin-facing replace flow should mint the next label and a fresh connector config in one step, or whether terminal revoke stays manual by design. Status: DECIDED (2026-11-17), terminal revoke stays manual by design. Rationale: `revoke` is an emergency action and coupling it to automated credential minting widens the security surface at exactly the wrong moment; enrolling a replacement is one existing admin call plus the documented credential derivation, so the manual path costs seconds; no incident or demand has shown it too slow; and a replace flow would prejudge the parked multi-replica ownership design. Revisit only when a real recovery exercise shows the manual path exceeding an operator's incident budget
6. WSS transport deprecation. Flip the sidecar to reverse-h2 only, soak, then delete the remotedialer code path, the 8092 WSS listener, its ingress route, and the rancher dependency tree. The comparison between the transports is complete; keeping both is a live security surface and a confusing dual path. Status: DONE (2026-11-17). The Go binary now rejects any transport env except empty or `reverse-h2` ("reverse-h2 is the only transport"); the worker mux and 8092 listener, the gorilla/websocket and rancher/remotedialer dependency tree, the worker session/relay/probe-loop machinery, and the `MCP_REMOTEDIALER_WORKER_LISTEN` env contract are all deleted (go.mod dropped gorilla + rancher; module tidy clean). The full regression suite was rebuilt for the reverse path (`transport_test.go`: synthetic credential digests, three-peer read sharing with revoke, drain-excludes-dispatch, revoked/stale-generation reconnect rejection) and passes under `go test -race` (1.3s), gofmt/vet/tidy clean. Dev overlay: `rd-worker` Service port, container port 8092, and the `litellm-mcp-remotedialer-worker-dev` Ingress deleted; network policy dropped 8092 (8093 stays); parity suite updated to no longer declare the WSS ingress. Live soak on the redeployed sidecar (image config digest `sha256:9b6ceef...`, registry manifest `sha256:1c64e047...`, pod re-pulled): port-forward to 8092 inside the pod gets `connection refused` (listener gone), Service ports are `data rd-reverse remote-control`, readyz 204 on rd-data; a fresh worker `wss-removal-soak` (fresh Ed25519 proof key, origin 18085) enrolled, connected via `https://litellm.ecouncil.ae/mcp-remotedialer-*` with `MCP_REMOTEDIALER_TRANSPORT=reverse-h2`, admitted healthy within one probe cycle, GET and POST round-tripped 200 through the data plane with credential stripping intact, 20-concurrency burst 20/20 and a 60s 5-RPS run 300/300 with zero failures; revoke flipped it to generation 2, the connector's re-registration 401s with capped backoff, and post-revoke reads shed 503 as designed. Python side deliberately unchanged: `TransportName` ("remotedialer"/"reverse-h2") names the stored DB route value in `OicmMcpSource.remote_transport`, which is the data-plane route name, not the Go connector transport; both literals remain valid stored values
7. Production overlay. Only when 2-3 land: mirror the 8888 data-plane Service port, the reverse ingress, and the network-policy port into the prod overlay, and re-run the parity suite, which will require declaring the new objects in both. Status: PARKED BY DESIGN (2026-11-17). The parity suite deliberately asserts that prod does not declare or enable the broker, and that contract stays: a prod broker needs prod secrets (the dev pepper and service token were created out of band on 2026-11-12 and no prod counterparts exist), prod database tables and gateway config wiring that have never been provisioned, and a demand the burn-in has not produced. Mirroring only the network shape while the dev broker stays the sole instance would make the parity suite lie about which environment owns the object. Trigger to reopen: an actual production consumer of an MCP source. In passing, the 2026-11-17 WSS removal surfaced three orphaned Ingresses applied out of band on 2026-11-12 that no manifest declared: `litellm-mcp-worker-dev` (WSS-era `/mcp-worker` route whose Service port name no longer existed, returning 503), `litellm-mcp-enrollment-dev` (`POST /mcp-sources`), and `litellm-mcp-public-mirror-dev` (the public install mirror). All three were deleted after deciding step 7 is parked: the worker route was dead surface, and the enrollment and mirror routes pointed at `litellm-proxy-dev`, meaning the public host was exposing dev-burn-in enrollment and install surfaces that prod itself does not serve (prod returns 404 in-pod for `/install/source-agent`; it has no `MCP_SOURCE_BROKER_ENABLED` and no source-agent env). Enrollment and install retrieval remain reachable through the dev gateway port-forward, which is the documented operator path

The Pass 2 delta and Pass 3 handover archives next to this document hold the prototype source, benchmark evidence, and profile data that steps 3-4 build on. `LiteLLM-1.108.5-Source-Agent-Reference.zip` remains the received research reference; the staging relay built from it was decommissioned on 2026-11-17
