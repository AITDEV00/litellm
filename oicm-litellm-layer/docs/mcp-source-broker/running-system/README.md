# Dev source broker

> **The dev LiteLLM gateway has no ingress. `kubectl port-forward` is how you test it.**
>
> Nothing outside the cluster can open a connection to `litellm-proxy-dev`. The public host
> `litellm.ecouncil.ae` fronts **prod** (`litellm-proxy`) and, separately, the dev broker's gRPC
> tunnel path. It does not front the dev gateway, so a request to a dev gateway REST route over
> the public host lands on prod instead. That is exactly why every dev proof runs through a
> forward: `kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000`, re-run after
> any `litellm-src-release-dev` because a rollout replaces the pod. Test dev against that forward.
> When a proof below says it ran through "the public ingress", the public host reached prod, not
> the dev gateway.

**This is the owning implementation record for the MCP source broker.** It describes what is built and running, and it is the file to update when behavior changes. `../history/superseded-design-proposal.md` is a superseded design proposal kept for decision history; `../decisions/approaches/lifecycle-and-pooling-questions.md` holds undecided questions; `../measurements/` holds measurements. Code wins over every document here, including this one

Build, test, lint, and proto-regeneration instructions for the Go sidecar live in `mcp-source-link/README.md` at the repository root of this layer, not here

The dev overlay is `deploy/overlays/dev/mcp-source-broker`. It creates one auxiliary Pod with broker and sidecar containers. LiteLLM authenticates and authorizes the normal MCP tool request before the private worker dispatches it to a healthy member

## Status register

Read this before reading any step below. A step that says DONE describes shipped behavior; anything else is not built

| Item | Status | Where the evidence lives |
|---|---|---|
| 1. Reproducibility from Makefile + manifests | DONE | step 1, this doc |
| 2. Service/port standardization on 8888 | DONE | step 2, this doc |
| 3. Multi-host failover | DONE | step 3, this doc |
| 4. Load profile measurement | DONE | step 4, this doc |
| 5. Multi-replica broker ownership | PARKED BY DESIGN | `../decisions/approaches/multi-replica-scaling.md` |
| 6. Post-revoke recovery tooling | DECIDED (manual revoke) | step 6, this doc |
| 7. WSS/remotedialer removal | DONE | step 7, this doc |
| 8. Production overlay | PARKED BY DESIGN | step 8, this doc |
| 9. Transport standardization on `jhump/grpctunnel` | DONE | step 9, this doc; reverse-h2 deleted |
| 10. Per-connector in-flight cap removed | DONE | step 10, this doc |
| Connector-version compatibility gate | DONE | `make -C oicm-litellm-layer mcp-source-link-compat` |
| Bootstrap credential exchange | IMPLEMENTED, last step runs over a port-forward | `../decisions/approaches/connector-owned-enrollment.md` |
| First-bootstrap resume after a lost worker response | IMPLEMENTED, last step runs over a port-forward | `../../../mcp-source-link/BOOTSTRAP-GAPS.md` B1 |
| Source-agent release publisher | BUILT, live RustFS upload unproven | `scripts/publish-source-agent-release.sh` |
| Admitted-capability logging | DONE | `internal/tunnel` `Runtime.onOpen` |

## Known gaps

These are not steps; they are things the design called for that the tree does not yet contain. Each is a real, verifiable absence, not a suspicion

**Connector-version compatibility gate.** Built. `make -C oicm-litellm-layer mcp-source-link-compat` builds the connector at the pre-transition revision and the current connector, and runs each against a current broker over a TLS gateway: admit, probe, execute, generation fencing, revoke, and clean degradation when the broker lacks a capability the connector wants. It is a local capability regression check, not a GitHub Actions release gate

**Bootstrap credential exchange.** Implemented 2026-11-18 on
[`../decisions/approaches/connector-owned-enrollment.md`](../decisions/approaches/connector-owned-enrollment.md).
An ordinary gateway user account key with no team is now enough: `POST /mcp-sources` accepts it
and returns the gateway-derived `owner` plus the deterministic `source_id`. Worker bootstrap on
`POST /mcp-source-link-sources/{id}/workers` accepts Proxy Admin or the real owner, and issues the
complete connector identity (`worker_id`, `source_id`, `target`, `generation`, `credential`). The
connector in `internal/bootstrap` performs both calls itself with the user's key, persists the
identity atomically at mode 0600, and reuses it on restart. The pepper and the service token never
reach a source host

Proven live on dev: a no-team account enrolled its own source, the connector registered itself
with no human touching a route, restart reused the identity with zero re-registration, and the
pending source stayed out of the MCP registry

Not yet proven: the approved real read, and reaching admitted/healthy. That is not
blocked by the missing ingress, which is what the forward is for. REST goes through
`kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000`, and the
tunnel path is already served for dev by the public host's
`/grpctunnel.v1.TunnelService/` rule, because `/mcp-sources` over the public host lands
on prod. The remaining step is restarting that forward and repeating the approved read
against it. Nothing here is marked SHIPPED until that runs

Neither requirement changes the approval gate. A newly registered entry still lands in
`pending_review` and serves nothing until an administrator approves it, whatever process made the
registration call and whoever it authenticated as. Approval, guardrail edits and rejection stay
administrator actions

**First-bootstrap resume.** Added 2026-11-19. The first bootstrap is not atomic across the gateway
writes, so a failure after the worker row existed but before the identity was persisted used to
strand the install: the retry collided on the source identifier and again on the worker label and
never saw the lost credential. A repeat enroll now resumes when the collided row is the same
owner's and still `pending_review`, and the worker route falls back to `resume_peer`, which
re-derives the credential for the worker already stored under that label at its stored generation,
without a new row and without advancing the generation. An approved, rejected or foreign row is
still a 409, and a revoked or draining worker is refused, so the duplicate-label fencing and the
terminal-revoke decision are both intact. Connector side, `setup.Initialize` resumes from
`user.key` and `bootstrap.json` alone, with no second prompt and no second key. Those are facts
about the claim. In current code 401/429 produce `ErrNotYetAvailable` but `Initialize` does not
wait/retry: it returns directly out of `runInit` before the installer enables the service. The
actual worker response remains a one-attempt code path even though documentation states retries.
See [the independent review](../../mcp-source-link/REVIEW-2026-11-19.md) for this and approval/
credential drift traps rather than just checking pending-review ownership

**Admitted-capability visibility.** Done. `internal/tunnel` `Runtime.onOpen` logs worker id, generation, and the sorted admitted capability set on every tunnel open, e.g. `tunnel admitted worker=... source=... generation=1 capabilities=[execute probe]`

## What this system is today

One Go binary, two modes. As `worker` it runs as a sidecar beside the Python broker and owns two listeners. As `connector` it runs on a source host and dials out. All three files below are the same binary at `mcp-source-link/`

One transport: `tunnel`, one long-lived `TunnelService/OpenReverseTunnel` gRPC stream over the 8093 listener, with typed `DispatchService/{Admit,Probe,Execute}` RPCs multiplexed inside it by `github.com/jhump/grpctunnel`. The reverse-h2 data plane was deleted along with `internal/reverseh2/`, its long-poll routes, and the wire limits only it used. `MCP_SOURCE_LINK_TRANSPORT` is connector-only and vestigial: the worker has one plane and ignores it, and an empty value means tunnel

Port map, from `deploy/overlays/dev/mcp-source-broker/source-link-patch.yaml`:

| Port | Name | Protocol | Owner | Carries |
|---|---|---|---|---|
| 9091 | `remote-control` | HTTP | Python broker | `/v1/source-link/{peers,report,revoke}` and admin routes |
| 9092 | `link-data` | HTTP/1.1 **and** h2c | Go sidecar | `/readyz`, `/sources/{id}/{path}`, `/v1/source-link/revoke` |
| 8093 | `link-tunnel` | h2c only | Go sidecar | `/grpctunnel.v1.TunnelService/*` only |
| 8888 | Service `data` | — | Service → 9092 | the URL the gateway is registered with |

The two containers share a pod network namespace deliberately: the broker reaches the sidecar's revoke endpoint over `127.0.0.1:9092`, and that endpoint accepts loopback callers only

The gateway contains no relay and starts no connector. Production is not enabled by this slice

## Where to look

The Go sidecar is split into one package per behavior under `mcp-source-link/internal/`, so the owning file for a question is the package named after it.

| Question | Package |
|---|---|
| Caller request routing, admission cap, header stripping | `internal/pool` |
| Tunnel plane, admission, probe loop, fencing | `internal/tunnel` |
| Listener construction (h2c and data mux) | `internal/listen` |
| Catalog canonicalization and proof verification | `internal/catalog` |
| Connector side: origin round trip, reconnect loop | `internal/connector` |
| Broker peer list, health report, service auth | `internal/control` |
| Environment, identity file, and peer record validation | `internal/config` |
| Envelope types and shared size limits | `internal/wire` |
| Process wiring for both modes | `mcp-source-link/main.go` |
| Gateway registration and data-plane URL | `litellm/proxy/mcp_source_broker/gateway.py` |
| Connector installer and release publishing | `litellm/proxy/client_distribution/source_agent_distribution.py` |

## Tunnel transport (the only data plane)

The connector dials one long-lived `TunnelService/OpenReverseTunnel` gRPC stream to the
public gateway over the 8093 listener and keeps it alive. Typed
`DispatchService/{Admit,Probe,Execute}` RPCs are multiplexed inside that single stream by
`github.com/jhump/grpctunnel`, so a forwarded request is a tunnel stream rather than an
origin TCP socket per request. nginx publishes the route with
`nginx.ingress.kubernetes.io/backend-protocol: GRPC`, which proxies cleartext HTTP/2 to
the sidecar so stream multiplexing survives the ingress hop. Per-worker enrollment,
catalog-fingerprint gating, generation fencing, revocation, header stripping, and the
bounded dispatch queue (slow connectors shed to HTTP 503 rather than buffering) all live
in `internal/tunnel` and `internal/pool`

The reverse-h2 long-poll plane that used to sit beside it (`POST
/mcp-source-link-{reverse,hold,dispatch,respond}`) is deleted, along with
`internal/reverseh2/` and the wire limits only it used. There is no fallback and no
dual-plane soak: the worker serves only the tunnel on 8093, and
`MCP_SOURCE_LINK_TRANSPORT` is connector-only and vestigial, since the worker has one
plane and an empty value means tunnel

## Live tunnel verification (2026-11-18)

Verified end to end on the dev cluster with a connector built from this tree, a scratch
origin fixture on loopback (`127.0.0.1:18099`, fresh Ed25519 proof key), and one enrolled
worker for a self-service source. The connector dialed `https://litellm.ecouncil.ae`, so
the whole path ran through the public ingress:

- Admission and capability log: the worker logged `tunnel admitted worker=... source=...
  generation=1 capabilities=[execute probe]`
- Probe and fingerprint: the peer reached `phase=active`, `healthy=True`, with
  `catalog_fingerprint` equal to the enrollment's `expected`
- Data plane and header stripping: `GET /sources/{id}/read` on the broker data port
  returned the origin payload, and an origin that echoed its received headers reported
  `authorization_seen=false`, `cookie_seen=false`, and no internal nonce, so caller
  credentials and cookies never reach the source
- Enrollment and approval, recorded separately: a non-admin user key self-enrolled and got
  `201 {"status": "pending_review"}`, read its own row back, was absent from
  `GET /v1/mcp/server` while pending, and appeared only after an admin approved it

Three defects surfaced under this real traffic and each has a regression test. The
enrollment anchor row had been skipped once the claim token was gone, which broke every
later worker registration through
`OicmMcpRemoteWorker.enrollment_id_fkey`. The enroll alias pattern allowed `-`, which
`MCP_TOOL_PREFIX_SEPARATOR` forbids downstream, so no alias could satisfy both. A
submitter got a 403 reading the row enrollment had just handed them


## Enrollment

There is one administrator account, the `admin` account the gateway is deployed with, and it
creates every other gateway user account. It creates one ordinary user account per person who
will expose a source. Those accounts are not admins, are not team-scoped, and need no team
membership. Each is used by that person's client binary: the connector carries that user's own
virtual key, so the gateway sees its calls arriving as that user

The connector posts a `SourceCreate` to `POST /mcp-sources` with the user's own key, so the
gateway sees the call arriving as that user. The owner is derived from the key's user id rather
than from the body:

The identity requirement is recorded in
[`../decisions/approaches/connector-owned-enrollment.md`](../decisions/approaches/connector-owned-enrollment.md),
which is open: today the handler also demands `team_id`, and the connector does not make these
calls at all, so a human still does

The request body:

```json
{
  "alias": "private_openapi",
  "target": {"host": "127.0.0.1", "port": 18080},
  "spec": "{\"openapi\":\"3.1.0\",\"paths\":{\"/source.json\":{\"get\":{}}}}"
}
```

`alias` excludes `-`, which is `MCP_TOOL_PREFIX_SEPARATOR`: the alias becomes the MCP
server name and that name prefixes every tool name. The response is
`201 {"server_id": ..., "status": "pending_review"}`. The submitter can read that row
back, and nobody else sees it until an administrator approves it

The source target allows loopback only and requires an explicit local service. Quarantine prevents tool dispatch, not subsequent recovery. Administrators can retrieve the existing connector installer whenever the source has been catalog-quarantined

The source target is only a loopback host and port. Source identity is derived from the authenticated user id and `alias`. The gateway saves the catalog object in the gateway-accessible artifact mirror, registers a stable private pool URL with LiteLLM, and grants that source its own MCP access group. Re-enrollment keeps its source, enrollment, connector lease, and relay credentials; it updates the target and expected canonical catalog fingerprint. It does not return another one-time token

The connector transport is the grpctunnel stream in `mcp-source-link/`: the connector on the source host dials in (the gateway cannot make outbound connections, so an agent-initiated session is the only way back), the sidecar accepts it, and dispatch rides back over that session. There is no in-flight admission cap (`DispatchInFlightCap = 0` as of step 10) and each connector runs 8 concurrent origin round trips. The 64 in-flight figure quoted here and in step 4 describes behavior before step 10 and no longer holds. This path is fully verified live (steps 2, 3, 4, 7 below) but its wire contract is implicit Go structs on both ends, which is why step 9 spikes `jhump/grpctunnel`: any version of either side must keep connecting, degrading to less functionality rather than breaking, and that contract must be enforced by a versioned proto, not implied by struct layout. That compatibility requirement is verified across versions by `make -C oicm-litellm-layer mcp-source-link-compat`

Connector installation no longer has a per-source token installer; the Chisel relay text below
is retained only as the 2026-11-13 historical record. Also **not current**: the following prose
describes an obsolete generated installer/env-continuation workflow; new `init` work exists but
[is independently refused](../../mcp-source-link/REVIEW-2026-11-19.md). What partly exists
today is `PUT /install/admin/v1/source-agent/release` receiving base64 binary + JSON + optional
installer. It **does** gateway-publish to RustFS without a gateway image deploy, but its actual
current publisher never replaces `__BASE__`, `__VERSION__`, `__SHA__`, `__STATE__`, hence cannot
safely drive a working normal installer. It also left runtime env connection missing

The already-supported source-of-truth design for the general cc-switch path is raw admin artifact
PUT via the gateway, RustFS-resident templates and active release metadata, validated gateway-
rendered install script and authenticated-only writes. That pattern is what source-agent must
use, with its devoted version/hand-off validation and unattended service/lifecycle fixes;
[source-agent-releases](../client-distribution/source-agent-releases.md) defines the concrete
contract. There is no reason for a separate direct-S3 route for admin machines, gateway builds
for each new binary/installer, or any unauthenticated RustFS write endpoint. This design is
not yet accepted merely because the storage objects can move. There is no release signature
and no signing key: releases briefly had them but those were removed as over-engineering

Release `0.2.0` (2026-11-17) is the first reverse-h2 release and stays in the bucket as immutable history (SHA-256 `03c607b6...702d00c`). Its assets include a `release.sig.b64` from the signed era that nothing reads any more. The pre-rename `0.1.0` release (retired WSS-era source-agent) also stays in the bucket as harmless legacy

A release version is immutable once published: re-publishing a version that already has a `source-link` object returns 409, so a version number always names the same bytes

Release `0.3.0` was the historical last-verified release on 2026-11-17: the reverse-h2 `source-link` built by `make mcp-sidecar-build` (SHA-256 `1242a6e1...b5269`), with `release.json` pinning sidecar image manifest `sha256:3ad55297...a3b7a4`. It carries the round-trip caller-cancellation and shutdown-deadlock fixes, so connector hosts get a behaviour change beyond the installer script, not just a new checksum. The published 0.3.0 binary and manifest are unchanged by the installer-contract revert; only the served script changed. A republish of `0.3.0` returns 409 `release version is already published and immutable` with the stored bytes unchanged, and both `0.1.0` and `0.2.0` still serve their original bytes

## Calls


Use an ordinary virtual key authorized for the source-specific MCP access group, which is named `mcp-source-{source_id}`. Nothing is installed on the caller's machine. The gateway creates OpenAPI tools from the saved spec and dispatches them to the pool URL without a trailing slash


A healthy connector has a recent successful catalog probe and actual catalog fingerprint equal to the enrollment's expected fingerprint. Startup clears health timestamps. Different catalogs are quarantined rather than merged. The pool strips gateway authorization, cookies, and transport headers before forwarding. Retries are restricted to errors where the upstream connection was not established


The fixture proof can use this request with `READ_KEY` (substitute the `server_id` a source reported):


```sh
curl -fsS http://127.0.0.1:14003/mcp-rest/tools/call \
  -H "Authorization: Bearer $READ_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"server_id":"<server_id>","name":"read_source","arguments":{}}'
```

The returned `CallToolResult.content[0].text` contains JSON from the source's catalog endpoint. For an echo tool, use `{"channel":"dev-tool","body":{"text":"round-trip-through-tunnel"}}` as `arguments`. Validate the actual source marker and that its `credentials_seen` and `cookies_seen` booleans are false


## Port map (canonical)

The broker Service `litellm-mcp-broker-dev` is the only gateway-facing address. Its named ports and the container listeners behind them:

| Service port | Service name | Container port | Listener | Who calls it |
|---|---|---|---|---|
| 8888 | `data` | 9092 (`link-data`) | Go sidecar private dispatch: `/sources/{id}/{path}`, `/v1/source-link/revoke` | The gateway's MCP pool URL, `MCP_SOURCE_BROKER_DATA_PLANE_URL` |
| 8093 | `link-tunnel` | 8093 | Go sidecar `/grpctunnel.v1.TunnelService/*` only | Source connector, over the `tunnel` gRPC plane |
| 9091 | `remote-control` | 9091 | FastAPI broker process (remote control) | Administrator via port-forward |

8888 is the standard gateway-facing data-plane port: the gateway's registered pool URL is always `http://litellm-mcp-broker-dev.adeo-litellm.svc.cluster.local:8888/sources/{id}`, from the `MCP_SOURCE_BROKER_DATA_PLANE_URL` env var. The connector routes ride the shared 443 ingress, unchanged. The NetworkPolicy matches pod ports after Service DNAT, so it keeps listing the container ports. The 8092 WSS listener and its `rd-worker` Service port and Ingress were removed on 2026-11-17. The worker serves only `tunnel` on 8093; `reverse-h2` and its four long-poll routes were deleted on 2026-11-18

## Verification


```sh
.venv/bin/python -m pytest -q tests/unit/proxy/mcp_source_broker
.venv/bin/python -m pytest -q tests/unit/proxy/client_distribution
.venv/bin/python -m pytest -q oicm-litellm-layer/tests/deploy
.venv/bin/python -m basedpyright litellm/proxy/mcp_source_broker
make -C oicm-litellm-layer mcp-sidecar-check
make -C oicm-litellm-layer mcp-source-link-compat
```

Publishing to a cluster that cannot reach `codeload.github.com` needs the documented host override, or `litellm-src-build` dies fetching the admin-agent tarball: `make litellm-src-release-dev LITELLM_SRC_BUILD_ARGS=--add-host=codeload.github.com:20.233.83.150`. The Go race suite needs `CGO_ENABLED=1` despite the sidecar build exporting `CGO_ENABLED=0`: `CGO_ENABLED=1 go test -race ./...` in `mcp-source-link`. The dev gateway's install surface is the `litellm-proxy-dev` Service on port 4000, so the documented 14001 port-forward is `kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000`; the broker Service has no 14001 port. A gateway roll replaces the pod and kills that forward, so re-run it after every `litellm-src-release-dev`.

Both broker images are reproducible from the Makefile and nothing else. `make -C oicm-litellm-layer mcp-broker-release-dev` resolves the running dev gateway's image digest itself, builds the auxiliary image from `deploy/overlays/dev/mcp-source-broker/Dockerfile` against it with `--pull=always`, pushes the tag, and rolls the dev broker Deployment. `make -C oicm-litellm-layer mcp-sidecar-release` builds the Go connector binary in `mcp-source-link` with the host toolchain (`CGO_ENABLED=0`, statically linked, guarded by a `file` check), layers it into the `FROM scratch` image in `mcp-source-link/Dockerfile`, pushes, pins the tag into `source-link-patch.yaml` so the committed manifest names the bytes that run, and finally deletes the sidecar pod so the same tag re-pulls. The Go build runs through `GO_ENV` (`/tmp/sa-build/bin/go`, `GOPATH=/tmp/rd-gopath`); keep `KUBECONFIG` pointing at the Al Ain kubeconfig. Do not hand-roll `podman build` for either image: a same-tag push is invisible to a running pod without the pod delete / rollout the targets perform

The 2026-11-17 reproducibility audit closed the last manual paths: the auxiliary recipe above used to live only in this document and its `Dockerfile` had been deleted at commit `db12cdb7946`; both are now committed and the Makefile is the canonical build entry. The released binary provenance is the same chain: `make mcp-sidecar-build` produces the exact `source-link` that release `0.3.0` publishes, and its `release.json` pins the sidecar image's registry manifest digest so a release always names the image its connector matches

The three paragraphs that follow describe the 2026-11-13 Chisel delivery and are kept as
the record of the first verified end-to-end path. Chisel, the one-time claim, and the
contexts port 8089/9090 they mention no longer exist; the current equivalent proof is the
2026-11-18 tunnel verification above and the step-9/step-10 entries below

On 2026-11-13, the installed dev gateway and auxiliary image carried `read_source` and `echo_source` for source `f804e8c9-a9c7-5824-a90a-837262788be8`. The one-time enrollment claim minted connector `7f3a49c4-59a7-5611-8c4e-fff9b8a0e670` at `127.0.0.20:18081`. An ordinary same-identity re-enrollment refreshed the saved canonical fingerprint after catalog drift. It returned no new token or connector

An administrator retrieved the authenticated recovery script through a local gateway forward and, in the historical Chisel flow, a route that no longer exists. The identical script installed the connector as user `jyao` on `aitdev00`, verified Chisel `1.12.0`, wrote a mode-600 environment file, and generated a matching user systemd unit. No `sudo`, downloaded shell execution, disabled TLS verification, or temporary service command override was needed

A virtual key restricted to this source's MCP access group listed exactly `read_source` and `echo_source`. `read_source` returned hostname `aitdev00` and the persisted source marker. `echo_source` returned `round-trip-through-chisel` on channel `dev-tool`, with `credentials_seen=false` and `cookies_seen=false`. The same key received HTTP 403 for the other source and a non-admin key could not retrieve an installer. Direct dispatch without the service credential returned HTTP 401. An already-redeemed token was rejected, including the complete claim-rejection test in the unit suite

Local connector, dev auxiliary, and dev gateway restarts each recovered with the same source ID, connector ID, listener, and correct MCP responses. Control port 8089 and dispatch port 9090 were unreachable on the public host. Production workloads were not modified

The real-PostgreSQL store suite passed nine tests, including catalog-mismatch quarantine, owner recovery, and valid-fingerprint reactivation. Its temporary `postgres-regression` tenant rows were removed. For that opt-in developer test, forward only `svc/adeo-litellm-postgres-dev-rw` to local port 15432, build its `LITELLM_TEST_POSTGRES` URL from the existing dev credential in a protected temporary file, and unset the variable afterward. Run the store tests with `--confcutdir=tests/unit/proxy/mcp_source_broker` so ambient environment stripping does not select unrelated host fixtures. The normal test command keeps this database test skipped

Focused broker tests, deploy parity tests, strict Ruff, and basedpyright passed. The complete repository gates were attempted but did not pass: dependency resolution was blocked by the GitHub codeload DNS sinkhole, format checks reported inherited files, and default-branch discipline comparisons reported inherited findings. The strict Ruff worktree gate also lacked its expected configuration. These are not claimed as successful repository-wide checks. Re-attempted 2026-11-17 after the WSS removal and smell pass: the sinkhole blocker is gone (`uv sync --inexact --frozen` resolves fully from the wheel cache in 65ms), and every ratchet gate passes against this slice's pre-work base `d042e605866` (ruff strict, type discipline, test quality, basedpyright ceilings with 111,189 errors all within budget, and the OpenAPI docs gate). The only genuine slice finding was ruff-format drift in `gateway.py` and `store.py` introduced by the 8888 standardization, now formatted. Against `origin/main` the gates still report overage (the branch is 1,100 commits past the merge base and carries findings inherited from the whole internal line, e.g. openrouter_compat and voice_routes), which is a branch-versus-main reconciliation for whoever next rebases or merges, not a property of this slice

Multi-source host failover is implemented and verified live (see step 2 below): two hosts held concurrent reverse sessions, reads distributed 6/6, origin-loss failover moved all traffic to the survivor with zero failures, and the probe cycle excluded and re-admitted the failed host. Native MCP catalog membership, authorization across source replicas, root/system connector installation, and multi-replica session ownership remain unimplemented and unclaimed by this dev slice. The README next to this document remains the earlier design proposal, not a release claim

## Step evidence trail

Historical record of each step's acceptance evidence, kept in original form so the benchmarks and live results stay citable. Status lives in the Status register at the top of this file; this section is the proof, not the plan. Numbers quoted inside these entries reflect the code at the time of the run and may describe superseded behavior, notably the 64 in-flight cap removed in step 10:

0. Release signing (REMOVED, 2026-11-17, not parked). Releases were briefly Ed25519-signed at publish and the installer briefly verified the signature; both were removed the same day as over-engineering. The publish route is admin-only, the installer checksum is derived from what the gateway published, and a substituted binary fails at the broker without valid enrollment credentials, so signing protected nothing the existing gates do not. The signing key existed only in `/tmp` and was destroyed after the removal. Reopen only if a real distribution channel appears that the gateway does not control, for example the binary being mirrored outside RustFS or an air-gapped host receiving the release over untrusted media; the enforcement point belongs at that channel's boundary. TODO: https://github.com/AITDEV00/litellm/issues/new (file under this doc's ownership when reopened)

1. Reproducibility. Everything deployed must be spawnable again from the Makefile and the committed manifests, with no shadow patches. Status: DONE (2026-11-17). The audit compared every broker object (Deployment, Service, NetworkPolicy, Ingress) field-by-field against the live cluster: MATCH, no drift, no out-of-band patches; the broker Secret is deliberately untracked in git and the CNPG secret is operator-owned. Both image builds were previously Makefile-unrecorded (the sidecar had no Dockerfile at all, the auxiliary recipe lived only in this doc with its `Dockerfile` deleted at `db12cdb7946`), and the RustFS-stored install binary was the retired WSS-era source-agent. All three gaps are closed: `mcp-sidecar-release` and `mcp-broker-release-dev` build, push, and roll from the Makefile (proven live: the pods' imageIDs equal the pushed registry manifests `sha256:47f3b330...` and `sha256:641e085a...`), both Dockerfiles are committed, and release `0.2.0` publishes the current reverse-h2 `source-link` through the admin release route. The vestigial `fingerprint` and `relay-host-key` keys on the broker Secret remain noted but unread by anything

2. Service and port standardization. Both broker Services keep named ports, but the public data-plane entry is renumbered to the standard 8888 on the Service (gateway-facing dispatch), so no code or registration carries a magic number anymore: the gateway's pool URL moves into an overlay env var, the owning doc gets one canonical port map, and the parallel source-agent research relay is decommissioned once the broker path is proven (it has zero gateway references and doubles the review surface). The connector routes stay on the shared 443 ingress, unchanged. Status: DONE. The 8888 Service port, the `MCP_SOURCE_BROKER_DATA_PLANE_URL` env var, and the canonical port map above landed and were re-verified live; the research relay's Deployment, Service, Ingress, NetworkPolicy, secret, and overlay directory were all deleted on 2026-11-17 after the 8888 round trip passed (the install mirror still serves the source-agent release script, which is client-distribution surface, not relay surface)
3. Multi-host failover. Enroll a second source host with its own origin and proof key, run both connectors concurrently, and verify request distribution and failover when one host stops. The pool already prefers least-in-flight members, but no test has exercised two live reverse sessions from different hosts. Status: DONE (2026-11-17). Two workers (`failover-host-a` on origin port 18084, `failover-host-b` on 18085, each with its own Ed25519 proof key and dataset) held concurrent reverse sessions; 12 dispatch calls split exactly 6/6 across markers; killing host A's origin moved all 12 subsequent calls to host B with zero failures while A's connector kept its session (the origin refused, dispatch shed, no poisoning); after one probe cycle the broker marked A `healthy_at=NULL` and excluded it from dispatch (8/8 to B); restarting A's origin re-admitted it within one probe cycle and distribution rebalanced 5/5 over 10 calls
4. Load profile. The Pass 3 feasibility control held 100k SSE streams on one h2c connection at 610 MiB with a raw prototype; the shipped long-poll design has not been loaded past tens of concurrent requests. Run the Pass 3 harness against the dev deployment and record streams, memory per connector, and p95 dispatch latency, so the architecture claim is measured on the real protocol. Status: DONE (2026-11-17). Measured on the dev deployment through Service port 8888 with one reverse-h2 connector (`load-host`, uvicorn origin fixture on the source host) and a driver inside the broker pod, 10s runs, GET `/source.json`: 50 RPS 500/500 p50 77 ms p95 269 ms; 100 RPS 1000/1000 p50 553 p95 849; 250 RPS 2500/2500 p50 563 p95 855; 500 RPS 5000/5000 p50 538 p95 798; 1000 RPS 9998/10000 (two 502 origin timeouts) p50 546 p95 802 p99 951. The Python-thread driver saturated at 2000 RPS offered, so the true per-connector ceiling above 1000/s is not established (same driver caveat as Pass 3). Held streams: 32 and 64 concurrent 3s-held calls all 200; 128 concurrency sheds exactly at the designed 64 in-flight cap (64 x 200 + 64 x 503 with `Retry-After`), and memory was flat across the sweep within sampling noise: connector RSS 19.0-19.2 MiB, sidecar RSS 8-13 MiB (metrics-server), sidecar CPU 70-73 milli-cores at 500 RPS. Sustained overload wedges the single-process origin fixture, not the data plane: dispatch sheds 503 with zero memory growth and no session loss, drain is bounded by the 8 concurrent origin round trips per connector, and full recovery was verified after rest (200 RPS 1600/1600). The architecture claim is measured on the shipped protocol: a held request costs pending-queue entry plus h2 stream state, not a socket or buffer growth. In passing, two connector processes sharing one config re-confirmed duplicate-session rejection: the winner serves, the loser gets 401 with capped backoff
5. Session and connector ownership. The dispatch long-poll ties a session to one pod; a multi-replica broker needs pinned placement plus fan-out or a shared queue. The debate, positions, and resume conditions live in `../decisions/approaches/multi-replica-scaling.md`; do not redesign here, and decide only when a replica increase is actually demanded
6. Recovery tooling. After `revoke`, a worker is terminal and a new label must be enrolled by hand. Decide whether an admin-facing replace flow should mint the next label and a fresh connector config in one step, or whether terminal revoke stays manual by design. Status: DECIDED (2026-11-17), terminal revoke stays manual by design. Rationale: `revoke` is an emergency action and coupling it to automated credential minting widens the security surface at exactly the wrong moment; enrolling a replacement is one existing admin call plus the documented credential derivation, so the manual path costs seconds; no incident or demand has shown it too slow; and a replace flow would prejudge the parked multi-replica ownership design. Revisit only when a real recovery exercise shows the manual path exceeding an operator's incident budget
7. WSS transport deprecation. Flip the sidecar to reverse-h2 only, soak, then delete the remotedialer code path, the 8092 WSS listener, its ingress route, and the rancher dependency tree. The comparison between the transports is complete; keeping both is a live security surface and a confusing dual path. Status: DONE (2026-11-17). The `loadConfig` worker path rejects any transport env except empty or `reverse-h2`; note that `run` separately accepts `reverse-h2` and `tunnel`, because the tunnel plane was added later in step 9 and every worker build serves both. Deleted in this step: the worker mux and 8092 listener, the gorilla/websocket and rancher/remotedialer dependency tree, the worker session/relay/probe-loop machinery, and the `MCP_REMOTEDIALER_WORKER_LISTEN` env contract are all deleted (go.mod dropped gorilla + rancher; module tidy clean). The full regression suite was rebuilt for the reverse path (`transport_test.go`: synthetic credential digests, three-peer read sharing with revoke, drain-excludes-dispatch, revoked/stale-generation reconnect rejection) and passes under `go test -race` (1.3s), gofmt/vet/tidy clean. Dev overlay: `rd-worker` Service port, container port 8092, and the `litellm-mcp-remotedialer-worker-dev` Ingress deleted; network policy dropped 8092 (8093 stays); parity suite updated to no longer declare the WSS ingress. Live soak on the redeployed sidecar (image config digest `sha256:9b6ceef...`, registry manifest `sha256:1c64e047...`, pod re-pulled): port-forward to 8092 inside the pod gets `connection refused` (listener gone), Service ports are `data link-reverse remote-control`, readyz 204 on link-data; a fresh worker `wss-removal-soak` (fresh Ed25519 proof key, origin 18085) enrolled, connected via `https://litellm.ecouncil.ae/mcp-remotedialer-*` with `MCP_SOURCE_LINK_TRANSPORT=reverse-h2`, admitted healthy within one probe cycle, GET and POST round-tripped 200 through the data plane with credential stripping intact, 20-concurrency burst 20/20 and a 60s 5-RPS run 300/300 with zero failures; revoke flipped it to generation 2, the connector's re-registration 401s with capped backoff, and post-revoke reads shed 503 as designed. Python side deliberately unchanged: `TransportName` ("remotedialer"/"reverse-h2") names the stored DB route value in `OicmMcpSource.remote_transport`, which is the data-plane route name, not the Go connector transport; both literals remain valid stored values. Code smell pass (the 4-layer technique) after the removal found six leftovers of the surgery, all fixed: `address()` and the generic `control.request()` helper orphaned by the listener/mux deletion, `dispatchMember.ready`/`.close` wrapper methods with no callers, `reverseSession.noteProbe` (the probe loop writes `lastProbe` under `session.mu` directly), the write-only `runtime.drained` map (drain eligibility is re-derived from `worker.Phase` per request, so the cache was dead state), and an unreachable second body-cap check in `executeDispatch` (`len(body) > reverseMaxBody` can never fire behind the tighter `reverseMaxOriginBody` check ahead of it); staticcheck (U1000) confirmed all symbol removals and runs clean, the removal cascaded one dead `fmt` import caught by the recheck loop. Sidecar rebuilt and redeployed on the smell-fixed binary (image config digest `sha256:5fd7fd4e...`, registry manifest `sha256:4f1ceb07...`, pod re-pulled) and every step-6 claim re-verified live on it: `MCP_SOURCE_LINK_TRANSPORT=remotedialer` rejected at startup with the exact message, 8092 `connection refused` inside the pod, readyz 204, Service ports `data link-reverse remote-control`, WSS ingress absent; a fresh worker (`smell-review-soak`) enrolled, connected, and admitted healthy; GET and POST round-tripped with `credentials_seen=false cookies_seen=false`; stateful read 400, unauthorized read 401; 20-concurrency burst 20/20 and 60s 5-RPS 300/300 zero failures; revoke to generation 2, connector re-registration 401s, post-revoke reads 503. The full user path was also re-run end to end through a second worker: a virtual key minted against the prefixed access group `mcp-source-{id}` dispatched `read_source` and `echo_source` through the gateway and the 8888 data plane to the origin and back. The test keys were deleted and both workers revoked after the run
8. Production overlay. Only when 3-4 land: mirror the 8888 data-plane Service port, the reverse ingress, and the network-policy port into the prod overlay, and re-run the parity suite, which will require declaring the new objects in both. Status: PARKED BY DESIGN (2026-11-17). The parity suite deliberately asserts that prod does not declare or enable the broker, and that contract stays: a prod broker needs prod secrets (the dev pepper and service token were created out of band on 2026-11-12 and no prod counterparts exist), prod database tables and gateway config wiring that have never been provisioned, and a demand the burn-in has not produced. Mirroring only the network shape while the dev broker stays the sole instance would make the parity suite lie about which environment owns the object. Trigger to reopen: an actual production consumer of an MCP source. In passing, the 2026-11-17 WSS removal surfaced three orphaned Ingresses applied out of band on 2026-11-12 that no manifest declared: `litellm-mcp-worker-dev` (WSS-era `/mcp-worker` route whose Service port name no longer existed, returning 503), `litellm-mcp-enrollment-dev` (`POST /mcp-sources`), and `litellm-mcp-public-mirror-dev` (the public install mirror). All three were deleted after deciding step 8 is parked: the worker route was dead surface, and the enrollment and mirror routes pointed at `litellm-proxy-dev`, meaning the public host was exposing dev-burn-in enrollment and install surfaces that prod itself does not serve (prod returns 404 in-pod for `/install/source-agent`; it has no `MCP_SOURCE_BROKER_ENABLED` and no source-agent env). Enrollment and install retrieval remain reachable through the dev gateway port-forward, which is the documented operator path

## Rename to source-link (2026-11-17)

The Go module directory is `mcp-source-link`, matching the `source-link` binary it builds and the release name the distribution slice publishes. It was named `mcp-remotedialer` from the `rancher/remotedialer` WSS transport that step 7 deleted, and the stale name misdescribed a module that now implements two data planes, `reverse-h2` and `tunnel`

The rename covers every published surface, not just the directory: the connector routes `/mcp-source-link-{reverse,hold,dispatch,respond}`, the gateway's `/mcp-source-link-sources` routes, the sidecar's control routes `/v1/source-link/{peers,report,revoke}`, the `MCP_SOURCE_LINK_*` environment variables an installed connector reads from `connector.env`, the `MCP_SOURCE_LINK_ENABLED` broker flag, the `link-data` and `link-reverse` Service port names, the sidecar container name, and the image repository `mcp-source-link-worker`. The reverse ingress also gains the `/grpctunnel.v1.TunnelService/` prefix for the tunnel plane

Three strings deliberately keep the old name because each is stored state, so renaming it would silently invalidate live records: the `remotedialer-credential` HMAC domain in `keys.py` (it derives every enrolled worker's stored credential digest), and the `remote_transport` column's legacy `'remotedialer'` value, which `selection()` still accepts at read time rather than rewriting rows at boot. New rows default to `'tunnel'`, and `selection()` returns `tunnel` for every accepted literal (`'tunnel'`, `'remotedialer'`, `'reverse-h2'`) since the tunnel is now the only data plane; rejecting anything else is what keeps a corrupted value loud instead of silently rerouted. Because the installed connector's env vars moved, a connector upgraded to a build before this rename keeps working against the old route names only if its broker also predates the rename; the two must be rolled together, which is why release 0.4.0 ships both

9. Transport standardization on `jhump/grpctunnel` (DONE 2026-11-18, started 2026-11-17; reverse-h2 deleted). The broker-to-connector pair must keep working across independent upgrades of either side: any client version connects to any server version, degrades to less functionality rather than breaking. The current hand-rolled reverse-h2 framing makes that contract unenforceable, and the bugs fixed in the 2026-11-17 smell pass (round-trip ignoring caller cancellation, shutdown deadlock while holding the runtime mutex) are exactly the classes of defect the library handles natively. Evaluation of six candidates (scratch clones in `evaluate-delete-later/`, 2026-11-17): `rancher/remotedialer` is the closest API match but is WebSocket-based, offers the server a general `net.Dial` into the agent's namespace, has an open concurrency-issue PR, and this repo already ran it in the WSS era and paid to remove it (commit `34aee32e104` built on it; step 7 records the removal); `apiserver-network-proxy` says in its README it should not be imported as a library; `frp` is standalone binaries; `openconfig/grpctunnel` is TCP-proxy-focused and explicitly not an official Google product; `yamux` is only a multiplexer and we already multiplex via HTTP/2. `jhump/grpctunnel` is the fit: reverse tunnel is precisely agent-dials-home, the server pushes typed RPCs down the agent-initiated stream, windowed flow control, per-RPC cancellation native, tunnel revision negotiation built in; single-maintainer risk noted. Spike scope: a `dispatch.proto` defining the RPCs the broker pushes (admission/probe/dispatch with a capability set exchanged at registration so old binaries degrade to less functionality instead of breaking), generated stubs pinned in the repo, sidecar hosting the tunnel handler and the Python broker dialing into it, and a compatibility soak gate: every broker release runs against the previously published connector binary. Two binding rules for the dispatch proto, decided before the first `.proto` exists: additive-only evolution (never renumber, never repurpose, never remove), and breaking changes require a deliberate new connector release, never a silent one. Reconnection does not resurrect in-flight requests; retry safety remains application logic because only the broker knows which origin operations are idempotent. Ingress carries gRPC over the shared 443 route with the `backend-protocol: GRPC` annotation on the backend leg. Acceptance for the spike: tunnel regression suite passes under `go test -race`, head-to-head benchmark against reverse-h2 at the step-4 load profile (50-1000 RPS, held streams, failover), and both-direction compatibility demonstrated (old stubs against new broker and new stubs against old broker) before any decision to replace the reverse-h2 path

Step 9 first live result (2026-11-17). The tunnel plane now connects and serves end to end. The connector's first bug was silent: `runTunnelConnector` built the dial address with string surgery, so the scheme survived into the target (`https://litellm.ecouncil.ae:443`) and gRPC could not parse it, which killed every attempt on the dial timeout without a single server-side log line. `grpcDialTarget` (now `DialTarget` in `internal/connector/tunnel.go`) parses the URL, joins host and port, and defaults to 443, with a regression test that fails on the old scheme-keeping form. Diagnosis took three separate traps worth recording: the sidecar is `FROM scratch` so `kubectl exec` has no shell, the ingress access log never shows `/grpctunnel.v1.TunnelService/` traffic because gRPC streams log at completion, and `grpctunnel`'s `Serve` returns `started=true` once the reverse stream opens, so a healthy tunnel is indistinguishable from a stuck one unless the broker-side `healthy_at` moves. Live proofs with transport `tunnel`: worker reaches `phase active` with `healthy_at` set and `catalog_fingerprint` equal to `expected_catalog`; data-plane `GET /sources/{id}/source.json` returns 200; the origin marker reports `credentials_seen=false` and `cookies_seen=false`; 8 parallel reads all 200; no token and bad token both 401; unknown source 503. The probe also needs the origin to sign `nonce + "\n" + source_id + "\n" + dataset_id + "\n" + canonical_catalog` with the Ed25519 key whose public half is the enrolled `proof_key`, so a fixture that serves the right bytes but no `X-Source-Signature` leaves `healthy_at` null forever; that is production behaviour, not a bug

Load testing the same day exposed the plane's second defect, and the 2026-11-18 root cause is worth the archaeology. Under sustained dispatch churn the tunnel died with `RST_STREAM INTERNAL_ERROR` and in-flight requests failed as 502s; the failure never reproduced in-process because it was not ours or grpc-go's. nginx error log at the exact reset instants: `client intended to send too large chunked body: 1048608 bytes while sending request to upstream`. The reverse tunnel is ONE client-streaming POST whose body accumulates every pushed RPC request for the tunnel's whole lifetime, and nginx's default `client_max_body_size 1m` (rendered inside the grpc location) kills that stream the moment the cumulative bytes pass 1MB, which under load is 15 to 50 seconds. Fix: `nginx.ingress.kubernetes.io/proxy-body-size: "0"` on the reverse ingress (commit pinned by `tests/deploy/test_source_link_ingress.py`), then a second full reload's worth of load verified clean: 17,624 dispatches in 60s at 64 in-flight with zero failures and zero resets, where the pre-fix tunnel died roughly every megabyte. Max concurrency measured the same day: the 64 in-flight cap is per connector and fail-fast (immediate `503 no eligible connector`), and adding a second connector doubles it to 128, so capacity scales with connector count rather than degrading

10. Per-connector in-flight cap removed (2026-11-18, commit `0b1a7001a47`). The literal `64` in `pool.go` is now `DispatchInFlightCap = 0`, and `pick` only checks `inFlight` when the cap is positive. The fix in step 9 removed the proxy-body-size cliff that used to masquerade as a per-connector ceiling; nothing structurally requires 64 anymore, since every dispatch multiplexes inside one HTTP/2 stream and a held request costs only a goroutine plus a 64KiB grpctunnel flow-control window per message. To measure the real ceiling the dev sidecar was raised from 1/256Mi to 1 CPU/512Mi requests + 8 CPU/16Gi limits, an uncapped `mcp-source-link-worker:jya0-v1.108.5` image was built and pushed (config digest `4fe6d79b9fc7cd...`), and a held-request ramp was run against the live ingress with ONE clean connector and a 2s-slow origin. Clean ladder against a freshly deduped connector: 512 in 5.60s (p50 3.30, p95 3.79, all 200), 1024 in 10.59s (p50 2.08, p95 2.20, all 200), 2048 in 5.55s (p50 2.68, p95 2.81, all 200), 4096 in 73.06s (p50 36.65, p95 69.62, all 200). Sidecar metrics at 4096: 8m CPU, 192Mi RAM. The 73s wall is NOT the transport: the Python thread-per-request loadgen pegged its own 8 CPU cores and grew its RAM from 146Mi to 326Mi doing TLS handshakes and GIL serialization, and the extra ~70s were client-side. To find a real transport ceiling the loadgen must move off Python (wrk, vegeta, or Go). At 4096 the sidecar itself uses well under one core and ~200 MiB, so neither in-tunnel streams nor the new resources are the wall. The 8192 row was cancelled deliberately: further rows would measure only more client CPU starvation. Three measurement hygiene rules were learned the hard way: (a) one worker identity must have exactly one live connector process, any duplicates masquerade as transport flakiness via 401 session jockeying; (b) the narrowest ramp rows must complete in seconds or the test setup itself is broken, so always launch with `mode=async`, per-row `timeout`, and an append-only log with `START`/`END` markers; (c) broker pod IP changes on every rollout, so the direct to-pod load path must re-read `status.podIP` rather than reusing the cached `10.42.6.6`

The Go-driver follow-up measured 65536 fully successful dispatches through the real tunnel in 15.151 seconds using a temporary internal h2c frontend and a modified connector origin connection pool after fixing transient probe-health eviction. Before the fix one two-second probe timeout cleared the connector's catalog fingerprint and readiness, producing large immediate 503 and 502 bursts despite the connector remaining alive. Transient probe or health-report transport errors now retain the last admitted catalog for 30 seconds. A fingerprint mismatch, revocation, generation fence, or closed session still makes a member ineligible. Generation fencing also applies synchronously in worker selection before asynchronous reconciliation.

The exact public HTTPS reverse-tunnel path was exercised with authenticated fresh-worker health through shared nginx, including follow-up completions of 16384, 32768, and 65536 requests. Subsequent per-leg TCP measurements and balanced idle-pool tests established connector-origin connection lifecycle pressure for the reproduced local failure. Public failures were not all strictly controlled and cannot retrospectively receive one guaranteed cancellation initiator. The earlier "pool tuning cannot cure" conclusion was invalid: the compared fixtures were not controlled. See the corrected final sections of the concurrency report rather than the intermediate cancellation diagnosis.

The full race suite covers transient probe timeout retention, expiry after the grace period, mismatch quarantine, and immediate generation fencing. The successful 65536 run, its resource use, the uncapped active connector, and the test-only h2c frontend are recorded in [the concurrency record](../measurements/go-driver-concurrency-2026-11-18.md). That report also records the 131072 offer as inconclusive because it lacked a final result. The sidecar image validated during that run was `h2c-data-20261118`; the current dev tag is pinned by `source-link-patch.yaml` and advanced by `mcp-sidecar-pin`. Live verification on 2026-11-18 shows `HTTP/1.1 204` and `HTTP/2 204` from `/readyz`, and h2c 401 on `/sources/probe-id/read`, proving h2c reaches the authenticated dispatch handler. The gateway-facing private data listener intentionally supports both h2c and the existing HTTP/1.1 dispatch, loopback-revoke, and kubelet-readiness contracts. The 8093 listener (`link-tunnel`, named `link-reverse` until 2026-11-18) stays h2c-only. In-process protocol and race suites pass. Production unchanged: `litellm-proxy` stays on its existing image with one `litellm` container and no source-link sidecar. Historical `probe-health-20261118` references above describe the previous rollout. `main.go` also sets the tunnel connector's `MaxIdleConns` and `MaxIdleConnsPerHost` to 65536 for established-loopback reuse under a measured burst. This is not an admission cap and not an independent connector deployment: it becomes active when the supplied static connector artifact is installed on the source host. The pod's Go image alone carries server-side support, not an assumption that remote hosts upgraded

Post-roll protocol checks covered Service readiness and the h2c authentication path, not a guaranteed live data read at scale. Two control rows with active phase and matching catalog fingerprints still returned 503 `no eligible connector` for an actual source read, and no `healthy_at` was published. Persisted peer/fingerprint retention alone is not proof of a present tunnel or successful end-to-end dispatch; a fresh actual source read is still required after connectors reconnect. Existing source connectors were not restarted and no new test enrollment was added in the final reconciliation. This final verification boundary does not retrospectively invalidate earlier successful real-source tests under older images, but it distinguishes them from final live dispatch health

A custom-tagged sidecar release requires a dev-overlay apply after `make mcp-sidecar-release`; deleting the pod alone does not select the new tag

11. BYOM self-service enrollment, and the deletion of the reverse-h2 data plane (DONE
2026-11-18). `POST /mcp-sources` no longer requires `PROXY_ADMIN`: the owner is the calling
key's user id, the source is registered through the BYOM `register_mcp_server` path rather
than the `default_registration` upsert, and the response is
`201 {"server_id": ..., "status": "pending_review"}`. `owner_may_mutate` gives an owner
edit/delete of their own row while it is pending or rejected, and `authorize_mcp_server`
lets a submitter read back what they submitted. The reverse-h2 long-poll plane and
`internal/reverseh2/` are gone; the worker serves only the tunnel on 8093. Proven live
through the public ingress with a connector built from this tree: `tunnel admitted
worker=... generation=1 capabilities=[execute probe]`, `phase=active healthy=True` with the
catalog fingerprint equal to the enrollment's expected value, and `GET /sources/{id}/read`
returning origin data with `authorization_seen=false` and `cookie_seen=false`. The approval
gate was proven separately: a pending row is absent from `GET /v1/mcp/server` and appears
only after `PUT /v1/mcp/server/{id}/approve`. Three defects surfaced only under real
traffic and each has a regression test: the enrollment anchor row is a foreign-key
dependency of worker registration and cannot be skipped, the enroll alias pattern allowed
the `-` that `MCP_TOOL_PREFIX_SEPARATOR` forbids, and a submitter could not read their own
new row. Reasoning and the departures from it are in
[`../decisions/approaches/byom-self-service.md`](../decisions/approaches/byom-self-service.md)

The Pass 2 delta, Pass 3 handover, MCP ingress pilot, and source-agent reference archives have been deleted from this directory. They held the research prototypes and evidence logs that steps 3-4 were built on; none of that content is recoverable from this repository now. The shipped implementation is `mcp-source-link/`, and the surviving measurements are in [the concurrency record](../measurements/go-driver-concurrency-2026-11-18.md)
