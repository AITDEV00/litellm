# Go-driver tunnel concurrency, 2026-11-18

The Python thread-per-origin and thread-per-request drivers were replaced with Go for this measurement. The highest verified clean result is 65536 simultaneous batched dispatches through the existing gRPC reverse tunnel, with no in-flight admission cap in the live data plane. That result used a temporary internal h2c frontend. Public-ingress 32768 and 65536 runs are incomplete tiers, as recorded below.

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

Neither requested public large tier completed cleanly. The 65536 result has a final summary and outer exit record, unlike the separate inconclusive 131072 internal attempt. No 503 eligibility evictions were counted in these public batches. The worker retained a matching fingerprint and active signed health, with one retained transient probe deadline warning and no broker or sidecar restarts. No nginx errors were found in the queried log window. These are per-RPC upstream-unavailable errors returned through the public tunnel, but the evidence does not isolate the precise component responsible. No protocol-incompatibility claim, sustained-RPS maximum, or clean public maximum is made from these rows.

The public-run connector was built from the then-current branch source with default idle origin pooling of 100 but no active-request cap. It must not be described as binary-identical to the prior high-tier modified fixture after the test source was recovered. That possibility and the small per-RPC 502 counts require a dedicated comparison with controlled ingress-point and transport settings before drawing a root-cause conclusion

Cleanup for this public run revoked worker `b0bd913c-2374-57a1-95ab-c37b86eef12b`, restored the temporary source selection to `reverse-h2`, deleted the load pod, terminated the connector and origin, removed their proof material and helper credentials, and stopped local forwards. No production gateway or ingress resource was modified
