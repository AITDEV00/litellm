# Go-driver tunnel concurrency, 2026-11-18

The Python thread-per-origin and thread-per-request drivers were replaced with Go for this measurement. The highest verified clean result is 65536 simultaneous batched dispatches through the existing gRPC reverse tunnel, with no in-flight admission cap in the live data plane. That result used a temporary internal h2c frontend. Initial public-ingress 32768 and 65536 runs failed intermittently; a later instrumented public-ingress 65536 tier passed completely, as recorded below.

## Scope

A temporary test source, connector, and Go origin used a distinct proof key and test-only listeners. The origin waits using `time.Timer`, not Python threads. The connector fixture allowed unlimited active connections to the origin; the fixed-tier fixture additionally increased the idle-connection pool. The test-only h2c-to-HTTP/1.1 adapter in the load pod removed frontend ephemeral-port pressure before forwarding internally to the actual dev data plane. The actual payload still traversed the worker's `pool.pick`, pushed `DispatchService/Execute`, the gRPC reverse tunnel, connector transport, and signed Go origin. These rows do not cover public nginx unless that path is stated explicitly. Timing includes the adapter hop, so it is end-to-end batched throughput, not a single-server latency profile. No production source or network policy was changed, and no production code was changed for this adapter.

Each row used a goroutine per request and a collective start barrier. The batch was counted as successful only when every status was 200 and every response body was `slow-ok`. A per-row hard command timeout, request deadline, and five-second progress output bounded every experiment. Failures and cancelled rows are not ceiling results.

| Offered dispatches | Frontend experiment | Successful 200 `slow-ok` | Wall | p50 | p95 | p99 | Interpretation |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 4096 | Go HTTP/1.1 | 4096 | 2.669 s | 2.294 s | 2.451 s | 2.460 s | Complete |
| 16384 | Go HTTP/1.1 | 16384 | 5.937 s | 3.241 s | 4.292 s | 4.831 s | Complete |
| 32768 | Go HTTP/1.1, immediate dials | 19457 | 19.699 s | 10.477 s | 11.370 s | 13.536 s | 13063 EOF, 221 idle connection closes, 27 local `EADDRNOTAVAIL` |
| 32768 | HTTP/1.1, max 4000 new dials/sec | 26010 | 15.800 s | 11.929 s | 14.769 s | 14.881 s | Incomplete: 6680 EOF, 75 idle closes, 3 dial timeouts |
| 32768 | HTTP/1.1 keep-alive, paced | 26915 | 26.852 s | 23.628 s | 25.834 s | 25.876 s | Incomplete: 5261 dial timeouts, 298 local `EADDRNOTAVAIL`, 273 EOF |
| 32768 | h2c front-end adapter, actual tunnel data plane | 32768 | 8.944 s | 6.749 s | 8.474 s | 8.558 s | Complete |
| 65536 | Same validated h2c front end | 795 | 52.329 s | 2.971 s | 5.151 s | 5.168 s | 40929 HTTP 503 plus 23812 HTTP 502 after eligibility loss; failed row |

## What the experiments established

The Python origin originally failed at roughly 4096 requests with `RuntimeError: can't start new thread`, and the Python request driver pegged its own CPU allocation. The Go origin/driver removed both artifacts. Even the stock Go connector origin transport defaults to 100 pooled sockets per host; a genuinely simultaneous per-origin TCP experiment requires no such fixture socket cap. A slim, glibc-less load container also requires `CGO_ENABLED=0`: a dynamically linked client can return `no such file or directory` because its ELF interpreter is absent. This failure has no relationship to the tunnel.

A one-shot HTTP/1.1 client over one broker IP begins conflicting with frontend socket setup and TIME_WAIT output around 20,000 calls, regardless of the tunneled RPC stream budget. Pacing only improved successful responses to 26010/32768. Switching to multiplexed h2c input gave a clean 32768/32768 tier in 8.944 seconds. This proves both the Go driver and HTTP/2 frontend adaptation work at that batch size. It does not prove unlimited concurrent execution, nor that 65,536 entirely simultaneous requests are supported.

## Failure boundary to fix separately

During the 65536-offered batch, `progress completed` stopped at 41724 dispatches. The connector process and the Go origin stayed alive and there was no connector disconnect log, but the source's `catalog_fingerprint` later cleared and `healthy_at` became null. Subsequent results were 40929 `503 no eligible connector` and 23812 `502 source upstream unavailable`. This was not `dispatchInFlightCap`, which was configured to zero in the live worker. In `tunnel.go`, `probeLoop` uses `tunnelProbeTimeout = 2*time.Second` and clears the reported catalog fingerprint whenever one pushed `Probe` fails. `ready` also requires `lastProbe` to be younger than `2*reverseProbeInterval`. Under heavy request pressure, a probe that cannot acquire an origin socket and complete its cryptographic verification within two seconds makes a still-connected connector temporarily ineligible. At that point one health-probe failure creates many more failing requests, and no maximum tunnel RPC concurrency can be inferred from the resulting 503s.

Resource samples during the h2c run were not an OOM event: no broker or source-link restarts occurred, the sidecar stayed under the supplied 8 CPU and 16 GiB, and eventual recorded sidecar use was about 1.1 CPU cores and 1.1 GiB. The dedicated load pod used about 2.9 CPU cores and 2.4 GiB during the failed 65536 tier. The test-pod Go origin and connector remained alive. There was no recurrence of the already-fixed cumulative nginx request-body reset.

## Probe-health fix and 65536 pass

A single transient probe RPC error or health-report transport error no longer removes the last successfully verified catalog. The member retains that verification for 30 seconds from its last admission, and a probe mismatch still clears the fingerprint immediately. Revocation, generation fencing, session closure, and rejected health reports remain immediate routing blocks. After a control-plane generation change, `pick`, `hasActive`, and `eligibleMembers` check the session fence synchronously instead of waiting for background reconciliation.

The same real data plane and test h2c adapter passed 65536 offered dispatches after the fix: `n=65536 wall=15.151s ok=65536 p50=10.315s p95=13.723s p99=14.463s codes=map[200:65536] failures=0`. The origin still delayed each request for two seconds and every response body was exactly `slow-ok`. Two `DeadlineExceeded` probe warnings were logged 15 seconds apart. The worker retained its verified catalog throughout and successfully refreshed health afterward. Sidecar use was about 1.5 GiB near completion, below its 16 GiB limit, with no restarts.

The `probe-health-20261118` sidecar image used in this run has manifest digest `sha256:76377d9f057b08867422eaa800801da6390410c48eac215ac65b9e87dce1b44e`. Its connector was also rebuilt with an uncapped active-connection allowance and a 65536-entry idle pool per host. The default 100-entry idle pool is a connection-reuse limit, not an execution cap. Connector transport differences between the initial failure and fixed high-tier run are not controlled here, so this report does not attribute the improvement solely to probe-health retention from the batched measurements alone.

The first fixed-client attempt at 65536 exited with 137 when its disposable load pod had a 4 GiB limit. That is a load-driver memory failure, not a tunnel or worker ceiling. Recreating only that pod with 16 CPU and 32 GiB allowed the successful run above. It does not change the sidecar's 8 CPU and 16 GiB allocation.

## Inconclusive 131072 batch

The next 131072-offered batch did not produce a final result row and is not a successful tier or a transport maximum. Its log contains the 32-request smoke round and `progress completed=8547/131072 elapsed=5s`, then no completion summary. Mid-run resource samples showed sidecar CPU 2538m and memory 1495 MiB, loadgen CPU 8440m and memory 5403 MiB, and fixture CPU 2664m and memory 2735 MiB, with no observed restarts. These numbers cannot establish the batch's result. A future attempt needs a bounded outer run that always records its final exit status and client completion state.

The verified internal batched-dispatch boundary remains 65536 simultaneous dispatches with the stated test adapter and fixture. It is not an unlimited-stream claim, an origin saturation claim, a public-ingress result, or a sustained-RPS measurement. Full dispatch source, pushed RPC, tunnel, connector, and origin paths were used in every successful row.

## Explicit public-ingress batches

A fresh dedicated worker enrolled and reached `healthy_at` with its signed catalog matching the expected fingerprint. Its connector used `MCP_SOURCE_LINK_PUBLIC_URL=https://litellm.ecouncil.ae`, producing exactly `https://litellm.ecouncil.ae/grpctunnel.v1.TunnelService/OpenReverseTunnel` for the gRPC service stream (gRPC supplies that service path; clients provide the root HTTPS gateway URL). The hostname resolves to the shared nginx LoadBalancer at `10.34.104.100`, and the ingress selects only `litellm-mcp-broker-dev` on the `link-reverse` port. The production gateway does not contain the test sidecar.

The request driver retained the internal h2c frontend to the real data plane as above. Therefore these batches explicitly test the connector-to-public-nginx tunnel, not a second public frontend hop. All results verified HTTP 200 plus exact `slow-ok` bodies. A 32-request smoke passed in 2.020 seconds with no failures.

| Public-tunnel offered dispatches | Exact successful responses | 502 responses | Wall | p50 | p95 | Result |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 32768 | 29376 | 3392 | 12.467 s | 10.612 s | 11.540 s | Failed batch |
| 32768 repeat | 28294 | 4474 | 12.034 s | 10.209 s | 10.756 s | Failed batch |
| 65536 | 62503 | 3033 | 38.066 s | 27.107 s | 37.181 s | Failed batch |

Neither requested public large tier completed cleanly in this initial sequence. The 65536 result has a final summary and outer exit record, unlike the separate inconclusive 131072 internal attempt. No 503 eligibility evictions were counted in these public batches. The worker retained a matching fingerprint and active signed health, with one retained transient probe deadline warning and no broker or sidecar restarts. No nginx errors were found in the queried log window. These are per-RPC upstream-unavailable errors returned through the public tunnel, but the evidence does not isolate the precise component responsible. No protocol-incompatibility claim, sustained-RPS maximum, or clean public maximum is made from these rows.

The public-run connector was built from the then-current branch source with default idle origin pooling of 100 but no active-request cap. It must not be described as binary-identical to the prior high-tier modified fixture after the test source was recovered. That possibility and the small per-RPC 502 counts require a dedicated comparison with controlled ingress-point and transport settings before drawing a root-cause conclusion

Cleanup for this public run revoked worker `b0bd913c-2374-57a1-95ab-c37b86eef12b`, restored the temporary source selection to `reverse-h2`, deleted the load pod, terminated the connector and origin, removed their proof material and helper credentials, and stopped local forwards. No production gateway or ingress resource was modified

## What the failed public rows exposed

Additional instrumentation recorded the underlying transport errors instead of only HTTP 502. The failed runs returned `rpc error: code = Canceled desc = context canceled` for pushed `DispatchService/Execute` calls. The connector subsequently logged `connector origin execute failure: context canceled`, which means the pushed request's context was canceled before its local origin operation completed. It does not identify an origin connection failure. One diagnostic 65536 burst completed with 1442 successful responses, 53093 HTTP 502, and 11001 HTTP 503. Its worker first logged cancellations, then lost the eligible member as the cancelled session drained. The 503s were subsequent eligibility shed, not the initial error.

The public path is nevertheless capable of the same scale as the internal path. A later static diagnostic sidecar and the preserved 65536-idle-pool connector passed all 65536 exact `slow-ok` responses through the public HTTPS tunnel in 13.142 seconds, with p50 7.051 s, p95 11.282 s, and p99 11.596 s. Public 16384 passed in 4.922 seconds and public 32768 in 7.304 seconds on the same session. Subsequent 256 and 8-request smoke requests also passed. No connector restart, broker restart, body-cap reset, or HTTP/2 ALPN failure was found. Signed health remained matching during that successful batch. This is a new complete public result, not an all-runs reliability guarantee.

Two early public 65536-pooled-fixture runs returned 735 and 645 failures respectively. They should not be cited as proof that origin-pool capacity cannot cure the high-concurrency failures: those fixtures had other behavioral confounders and lacked per-leg TCP connection accounting. The later controlled experiment below specifically corrects this interpretation

The confirmed failure mechanism is intermittent tunnel context cancellation with an eligibility cascade in some rows. The originating stream-cancellation condition remains unconfirmed. Because the later public 65536 row was completely clean and identical deployments were not used throughout the diagnostic sequence, it is not possible yet to attribute initial cancellations conclusively to nginx, client CPU, or a global grpctunnel stream issue. Nginx access records showed completed HTTP/2 request streams with status 200, so an outer accept status cannot say whether the nested calls all succeeded. 65536 nested dispatches were multiplexed by grpctunnel over one public gRPC stream, not 65536 direct public client HTTP/2 streams. The clean row proves multiplexed public tunnel capacity at that fixture size. Intermittent reliability defects remain active and the precise first-cancellation source is not determined. This does not prove there is no raw public HTTP/2 stream quota: the public tunnel itself is one gRPC stream carrying many nested RPCs, not 65536 direct public client streams. There is no hard cap on nested dispatches in the implemented data plane, but intermittent reliability defects are not resolved by the successful run

The diagnostics retain transport error context before converting the failure to HTTP 502. Log volume is bounded per worker to three immediate entries plus one per 1000 additional failures. Track future cancellations by first affected request and outer tunnel stream teardown if they recur, rather than adjusting connection pools or grace periods based only on the failed HTTP summary

## Historical port-pressure diagnosis and its later correction

A later timeline merge across the saved driver, worker, connector, and nginx logs, plus a gated in-process soak (`tunnel_soak_test.go`, `MCP_TUNNEL_SOAK=1`), established that connection lifecycle pressure can produce the nested canceled calls and eligibility cascade. It did **not** prove that every canceled call was merely unimportant load-client noise, and the later direct-h2c measurements below revive a decisive connection-reuse finding omitted from this initial diagnosis. Preserve this section as the contemporaneous investigation, not as the final exclusive root cause

First, the two catastrophic 65536 runs each lost the outer tunnel stream mid-run to `RST_STREAM with error code: PROTOCOL_ERROR` at 05:31:47 and 05:36:55 (connector logs: `tunnel session finished ... error=rpc error: code = Internal desc = stream terminated by RST_STREAM`). The nested-RPC cancel storms at those exact seconds were the consequence of that teardown. nginx access logs show the same streams served 200 with multi-minute durations (178.733s, 79.615s), consistent with the outer gRPC stream being killed while its child RPCs drained. In the 05:36 run the driver had already exited at 05:36:40; the connector cancel storm at 05:36:55 was post-exit drain, not the initiator.

Second, the local soak then reported TCP port pressure at 32768 concurrency: `dial tcp 127.0.0.1:PORT: connect: cannot assign requested address`, success counts near 28222/28237/28223, and the host's `net.ipv4.ip_local_port_range` confirmed a 32768-60999 range. This proved pressure in the test topology but, without counting individual TCP legs, it did not isolate the initiator. Public 32768 runs measured 28294/29376 successes. The stated comparison cohorts were not identical, even if the numbers resemble the loopback range

Contemporaneous interpretation: the h2c-to-HTTP/1.1 frontend adaptation made the data plane its internal HTTP/1.1 upstream hop rather than the gRPC stream count bottleneck. Its pool sizing and reuse therefore matter for each upstream connection. The later pinned sidecar also supports native h2c directly on private dispatch 9092, eliminating that extra frontend HTTP/1.1 hop from test calls intentionally using HTTP/2. The exact cancellation initiator for public stream resets seen in these rows was not isolated by these observations

## Genuine HTTP/2-multiplexed load: 65536 simultaneous, repeatedly clean

The initial h2c-only load-leg design removed per-request agent-side TCP dials but was incompatible with the HTTP/1.1 gateway, loopback revocation, and kubelet readiness clients. The deployed additive design is described in the corrected section below: clients that want multiplexing request h2c explicitly, while legacy clients retain HTTP/1.1 on the same 9092 listener. Connector-reuse behavior is measured separately

With that leg, the gated soak (`MCP_TUNNEL_SOAK=1`, simultaneous-release barrier, 2s origin delay per request, exact-body verification) ran the previously-impossible tier fully in-process:

| Simultaneous dispatches | Rounds | Result | Wall per round | Failures |
| ---: | ---: | --- | ---: | ---: |
| 65536 | 3 separate invocations, 5 rounds total | 65536/65536 every round | 13.81-15.64 s | 0 |
| 65536 under `-race` | 1 | 56795 ok, 8741 x 503 `no eligible connector` | 44.078 s | race-instrumentation slowdown, see below |

The 65k rows are genuinely simultaneous: one release barrier frees all 65536 goroutines at once, every request carries the 2s origin delay, and every response body is verified. Zero 502, zero cancellations, zero tunnel teardowns across all five rounds. The measured in-process fixture capacity is at least 65536. These rows span multiple h2c TCP legs where needed plus one outer gRPC reverse stream to the connector; they are not claims of a single end-to-end physical TCP connection

The `-race` row is not a defect: the detector stretches the run to 44s, the 2s probe deadline lapses under instrumentation, and the health gate correctly sheds to 503 until probes recover. No `DATA RACE` report was produced; the full race suite passes clean. It is recorded here because an operator repeating the soak under `-race` will see it.

Historical deploy note: the previous `probe-health-20261118` image predates the
h2c-capable data listener. The live additive-protocol deployment and verified digests are recorded below

> **Deployment facts below are current as of the run, not as of today.** The sidecar
> has since been rebuilt twice: the reverse-h2 plane was deleted on 2026-11-18, so the
> 8093 listener carries only `/grpctunnel.v1.TunnelService/`, and the Service port that
> was `link-reverse` is now `link-tunnel`. The concurrency numbers are unaffected, since
> they measure the tunnel and pool path, which did not change. For what runs today read
> [the owning implementation record](../running-system/README.md)

## Live deployment and the correction to the h2c and pooling record

The live dev sidecar now runs `mcp-source-link-worker:h2c-data-20261118` at registry digest `sha256:2465ab2ba89f1b9e4df5a561471c2d766484409c19a94b9c55064dc9e5bc1111`, with two healthy containers and zero restarts. The 9092 `link-data` listener accepts both h2c and HTTP/1.1 (`dataProtocols`); the Python control client's loopback revocation, gateway dispatch, and kubelet HTTP readiness remain supported. The 8093 `link-reverse` connector listener remains h2c-only (`h2cProtocols`). Direct live requests to `/readyz` returned HTTP/1.1 204 and h2c 204. An h2c request to `/sources/probe-id/read` returned 401, proving requests reach the authenticated forward handler and not some fallback listener

The earlier h2c description overstated its test boundary. The harness's `httptest.NewUnstartedServer(...).Start()` reset a configured server before serving, silently dropping its HTTP/2 settings. The fixture now serves the actual `http.Server` directly and checks `resp.ProtoMajor == 2` on every returned response. A separate `TestDataProtocolsServeLegacyAndMultiplexedRequests` asserts that HTTP/1.1 and h2c both work against the real fixture-mux configuration

Per-leg dial counters then identified the previously-unattributed resource pressure. With a 4096-request burst, the agent's h2c frontend required 62 TCP dials while connector-to-origin traffic required 4097. At 32768 simultaneous dispatches with the old 100-idle transport retained only as a test control, the round produced 28472 successful responses and 21616 connector-origin dials; with a deliberately retained 65536-idle connector transport, all 32768 succeeded with a much shorter wall time. A separate sustained 65536-idle two-round run at 65536 dispatches passed both. High idle capacity is about retaining reusable loopback TCP sockets, not an admission cap, and `dispatchInFlightCap` remains zero. This is the resource difference that made earlier same-shape-but-not-identical pool measurements non-comparable

The connector implementation now sets `MaxIdleConns` and `MaxIdleConnsPerHost` to 65536 when `MCP_SOURCE_LINK_TRANSPORT=tunnel`, matching the test's corrected behavior. This source-host binary setting is independent of the sidecar image: it becomes active when the newly supplied connector artifact is installed on the source host. No test-source connector was restarted during final verification, so the current parent connector's transport revision is an unverified deployment fact

Live HTTP/1.1 and HTTP/2 protocol handling and authenticated dispatch routing entry were verified after the final rebuild. The per-dial figures here belong to the in-process test fixture, not a new public-ingress load measurement. After the final sidecar rollout, two persisted control rows with matching catalog fingerprints returned 503 `no eligible connector` for an actual source read and exposed no `healthy_at`; a current successful source-host dispatch was therefore not proven. Fingerprints in the control plane are an eligibility input, not proof of an existing connector session

The historical rows above still document public failures rather than claiming they all necessarily pass again under the current deployment. Connection-lifecycle pressure in the load and connector-origin legs is the measured resource failure. Nested Execute cancellation and subsequent 503 eligibility loss were propagated effects in the reproduced case, not a fixed stream-count ceiling. The public canceled-stream initiator remains unproven for past uncontrolled runs

Production was not modified; the deployed `litellm-proxy` remains a single LiteLLM container and no production broker sidecar was created. `make mcp-sidecar-release MCP_SIDECAR_TAG=...` now builds, pushes, pins the tag into the overlay, and rolls the pod, so the committed manifest and the cluster no longer drift apart. Applying the overlay is still how a hand-edited tag reaches the cluster

Final current-fixture checks before release: `TestTunnelSoakReproducesPublicFailure` with 4096 simultaneous requests passed 4096/4096 exact responses; its configured 32768-request round passed 32768/32768 exact responses in 6.915 seconds with 5439 agent dials and 17049 connector-origin dials. `TestTunnelMultiChannelStress` with 4 x 1000 passed 4000/4000 exact responses in 2.336 seconds. Full `go test -race ./...` and `go vet ./...` passed. Each channel has a distinct transport with its own loopback dial override because Go's request transport cloning does not retain a custom `DialContext`; a shared supplied test transport makes fixture probes resolve the placeholder origin host. Product transport instances are constructed per source-host peer

## Multi-channel stress: 16 mock MCP servers x 32768 simultaneous requests

The multi-channel harness (`tunnel_multi_test.go`, `MCP_TUNNEL_MULTI=1`) models many independent MCP sources behind one broker and one agent client: each channel is a separate fixture stack with its own source ID, dataset, Ed25519 proof key, mock MCP origin (fixed 2s per-request delay), and admitted reverse tunnel. The agent client multiplexes every channel over h2c, and one global release barrier frees all channels' requests at the same instant, so the offered load is channels x per-channel genuinely simultaneous dispatches. Every response body is verified against its own channel's marker, proving per-source routing integrity under stress, not just status codes.

| Matrix | Total simultaneous | Result | Wall | Failures |
| --- | ---: | --- | ---: | ---: |
| 4 channels x 1000 | 4000 | 4000/4000 | 2.362 s | 0 |
| 16 channels x 4096 | 65536 | 65536/65536 | 6.678 s | 0 |
| 16 channels x 32768 | 524288 | 524288/524288 | 52.634 s | 0 |

The 16 x 32768 row is the headline: half a million dispatches in flight at one instant across 16 concurrently admitted tunnels, all routed to the correct per-channel origins, zero 502s, zero 503s, zero cancellations, zero tunnel teardowns, every body matching its channel marker. Host memory peaked near 29 GB of 30 GB (driver goroutines dominate; this is a single-process test harness bound, not a broker bound), and the run completed without swapping itself to death. The full race suite passes with the harness committed.

These historical rows cover multi-source fan-out in the in-process fixture, with independent runtime sessions and exact response markers. There is no limit-versus-admission test proving the host could not do less, and no live high-load result to extrapolate from them. Those assertions of an unbounded data plane or of only a per-source one-worker routing-pool pattern were too strong. Each fixture has one session and one origin; the measured capacity is jointly bounded by driver memory and the host TCP configuration. As reproduced below in `tunnel_soak_test.go`, stream multiplexing reduces load-side dials, while connector socket retention materially changes burst outcomes. Re-run with the current fixture before treating these rows as current production capacity evidence
