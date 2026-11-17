# Go-driver tunnel concurrency, 2026-11-18

The Python thread-per-origin and thread-per-request drivers were replaced with Go for this measurement. The resulting valid clean tier is 32768 simultaneous batched dispatches through the existing gRPC reverse tunnel, with no in-flight admission cap in the live data plane.

## Scope

A temporary test source, connector, and Go origin used a distinct proof key and test-only listeners. The origin waits using `time.Timer`, not Python threads. The connector's configured connector-side origin socket allowance was removed in its scratch binary. No production source or network policy was changed. A test-only h2c-to-HTTP/1.1 adapter in the load pod removes frontend TCP-head-of-line and ephemeral-port pressure before forwarding to the real data plane. The actual payload still traverses the worker's `pool.pick`, pushed `DispatchService/Execute`, public grpctunnel ingress, uncapped connector transport, and signed Go origin. Timing includes this adapter hop, so it is end-to-end batched throughput, not a single-server latency profile. No production code was modified for this measurement or this adapter.

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

Resource samples during the h2c run were not an OOM event: no broker or source-link restarts occurred, the sidecar stayed under the supplied 8 CPU and 16 GiB, and eventual recorded sidecar use was about 1.1 CPU cores and 1.1 GiB. The dedicated load pod used about 2.9 CPU cores and 2.4 GiB during the failed 65536 tier. The test-pod Go origin and connector remained alive. There was no recurrence of the already-fixed cumulative nginx request-body reset. A subsequent probe capacity/deadline design, priority reservation, and regression test are needed before further raising the claimed ceiling. Success at 32768 is the verified boundary of this experiment, not the theoretical transport maximum.

Cleanup removed the load pod, private worker, h2c adapters, connector, and Go origins, and all forwarding helpers. The temporary source's transport selection was restored to `reverse-h2`, its worker was revoked, and its short-lived enrollment left to expire. The original connector and signed fixture retained their prior configuration. No existing configuration or source code was changed as part of this load-only operation
