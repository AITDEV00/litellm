# File map: every file named in the answers, and what it does

Use this as the index when a later question starts from "where does X live". Grouped by layer.

## Deploy manifests

| Path | Purpose |
|---|---|
| `oicm-litellm-layer/deploy/overlays/prod/kustomization.yaml` | prod assembly: shared + gateway + rustfs + mcp-source-broker, one gateway patch |
| `oicm-litellm-layer/deploy/overlays/dev/kustomization.yaml` | dev assembly: shared unsuffixed, gateway suffixed `-dev`, own rustfs, own broker |
| `oicm-litellm-layer/deploy/base/shared/kustomization.yaml` | hooks ConfigMap + redis password, deliberately unsuffixed so dev cannot diverge |
| `oicm-litellm-layer/deploy/base/gateway/kustomization.yaml` | the per-environment gateway set |
| `oicm-litellm-layer/deploy/base/gateway/proxy/deployment.yaml` | gateway Deployment, 2 replicas, node pin rationale |
| `oicm-litellm-layer/deploy/base/gateway/proxy/service.yaml` | gateway Service |
| `oicm-litellm-layer/deploy/base/gateway/config/litellm-config.yaml` | gateway config, includes the MCP SSE and catalog URL allow entries |
| `oicm-litellm-layer/deploy/base/gateway/secrets/litellm-mcp-source-broker.yaml` | the `pepper` and `service-token` secret |
| `oicm-litellm-layer/deploy/overlays/prod/gateway/deployment.yaml` | prod-only gateway env and digest pin |
| `oicm-litellm-layer/deploy/overlays/prod/gateway/config.yaml` | prod gateway config patch, including JWT and SSO settings |
| `oicm-litellm-layer/deploy/overlays/dev/gateway/deployment.yaml` | dev gateway env, Microsoft OIDC, and local base URL |
| `oicm-litellm-layer/deploy/overlays/dev/gateway/config.yaml` | dev gateway config patch, independent of prod granting |
| `oicm-litellm-layer/deploy/overlays/prod/mcp-source-broker/broker.yaml` | the broker Pod: Python container, Go container, health probes, resources |
| `oicm-litellm-layer/deploy/overlays/prod/mcp-source-broker/source-link-patch.yaml` | patches the Go sidecar and the Service's data/tunnel/control ports |
| `oicm-litellm-layer/deploy/overlays/prod/mcp-source-broker/network-policy.yaml` | ingress and egress rules for the broker and RustFS |
| `oicm-litellm-layer/deploy/overlays/prod/mcp-source-broker/Dockerfile` | the auxiliary broker image |
| `oicm-litellm-layer/deploy/prod/litellm-ingress.yaml` | normal Ingress, `/` to gateway 4000, unlimited body |
| `oicm-litellm-layer/deploy/prod/mcp-tunnel-ingress.yaml` | separate GRPC Ingress, `/grpctunnel.v1.TunnelService/` to worker 8093 |
| `oicm-litellm-layer/deploy/dev/mcp-source-broker/*` | dev broker: same shape, no ingress, port-forward only |
| `oicm-litellm-layer/deploy/prod/litellm-postgres-cluster.yaml` | the CNPG database both gateway and broker use |
| `oicm-litellm-layer/deploy/base/rustfs/rustfs.yaml` | object store on 9000 |

## Go side (`oicm-litellm-layer/mcp-source-link/`)

| Path | Purpose |
|---|---|
| `main.go` | wiring only: CLI parse, `runWorker`, `runConnector`, `runAccountAdd`, `runSourceAdd`, `signalReload`, `reconcileLoop` |
| `dispatchpb/dispatch.proto` | the wire contract: Admit, Probe, Execute, ExecuteStream, additive-only evolution rules |
| `internal/wire/wire.go` | shared envelope sizes, deadlines, framing headers to strip |
| `internal/config/config.go` | env to validated config, TLS settings, `Slot()` |
| `internal/control/control.go` | control-plane client: `Refresh`, `Report`, `Peer`, `SameSecret`, `Bearer` |
| `internal/pool/registry.go` | session registry, `DispatchInFlightCap = 0`, `Reconcile`, `Revoke` |
| `internal/pool/eligibility.go` | the single `eligible`/`admitted`/`provenCatalog` predicate |
| `internal/pool/forward.go` | `pick`, `Forward`, `forwardOnce`, header allowlist, redirect drop, `RevokeHTTP` |
| `internal/tunnel/tunnel.go` | grpctunnel runtime, `authStreamInterceptor`, `admit`, `onOpen`, `probeLoop`, `Member`, `RoundTrip` |
| `internal/connector/tunnel.go` | connector side: `DialTarget`, `Server`, `Admit`, `Probe`, `Execute`, `ExecuteStream` |
| `internal/connector/connector.go` | `RunAll`, per-source tunnel lifecycle |
| `internal/connector/mcp_health.go` | MCP JSON-RPC health session, SSE incremental parsing |
| `internal/listen/listen.go` | listener construction, h2c config, protocol set |
| `internal/catalog/catalog.go` | canonicalisation and fingerprint verification |
| `internal/target/target.go` | target parsing and canonicalisation, shared grammar with Python |
| `internal/state/state.go` | local account store and source registry, mode 0600 atomic writes |
| `internal/setup/setup.go` | interactive `account add`, `source add`, `SignalConnector` |
| `internal/setup/spec.go` | OpenAPI document fetch from the origin |
| `internal/auth/auth.go` | `/sso/cli/password` exchange |
| `internal/bootstrap/client.go` | `Enroll`, `IssueWorker`, `Register`, `WorkerLabel`, `validateIssued` |
| `internal/harness/` | test-only fixtures, never linked into the binary |
| `compat/compat_test.go` | old-connector against current-broker compatibility check |
| `README.md` | build, test, lint, proto regeneration |
| `GRPCTUNNEL-TRANSITION.md` | the transition plan and its outcome |
| `BOOTSTRAP-GAPS.md` | B1 to B6 bootstrap defects and status |
| `FOLLOWUP-GAPS-2026-11-19.md` | G1 to G14 connector gaps and remediation records |
| `MULTI-ACCOUNT-SOURCE-GAPS.md` | N1 to N6 multi-account requirements |
| `REVIEW-2026-11-19.md` | independent review of approval and credential drift traps |

## Python side (`litellm/proxy/`)

| Path | Purpose |
|---|---|
| `mcp_source_broker/main.py` | the broker process: Prisma connect, store, remote control app on 9091 |
| `mcp_source_broker/gateway.py` | `POST /mcp-sources` self-service enrollment, `mount_mcp_sources`, `mount_mcp_sources_when_enabled`, `route_source` |
| `mcp_source_broker/remote_gateway.py` | `/mcp-source-link-sources/{id}/{selection,workers,...}`, admin auth, loopback revoke fan-out |
| `mcp_source_broker/remote_store.py` | peer rows, health reporting, lifecycle, generation fencing, `create_peer`, `resume_peer` |
| `mcp_source_broker/store.py` | `SourceStore`: enroll, idempotency, conflicts, routing |
| `mcp_source_broker/models.py` | frozen models, target grammar, alias grammar, route constants |
| `mcp_source_broker/keys.py` | pepper-derived credential digest, the kept `remotedialer-credential` HMAC domain |
| `client_distribution/source_agent_distribution.py` | `/install/source-agent` rendering, versioned asset proxy, release publish and activate |
| `client_distribution/storage.py` | RustFS client and config from env |
| `proxy_server.py` | calls `mount_mcp_sources_when_enabled` when a Prisma client exists |
| `auth/route_checks.py` | the `/mcp-sources` and `/workers` authz carve-outs |
| `auth/login_utils.py` | `verify_password_only`, rejects admin roles before key issuance |
| `_types.py` | `mcp_management_routes` including the source routes |
| `_experimental/mcp_server/*` | the underlying MCP server manager, DB, catalog, approvals |

## Templates and scripts

| Path | Purpose |
|---|---|
| `oicm-litellm-layer/templates/source-agent-install.sh.tmpl` | installer template with the four placeholders |
| `oicm-litellm-layer/scripts/publish-source-agent-release.sh` | publishes a release, optional explicit gateway URL |
| `oicm-litellm-layer/Makefile` | `mcp-sidecar-{build,check,push,pin,release}`, `mcp-broker-{build,push,pin,release-dev}`, `mcp-source-link-compat` |

## Tests

| Path | Purpose |
|---|---|
| `tests/unit/proxy/mcp_source_broker/` | gateway, models, remote_gateway, remote_store, store, target parity |
| `tests/unit/proxy/client_distribution/` | generator, routes, source agent distribution |
| `oicm-litellm-layer/tests/deploy/test_source_link_ingress.py` | the tunnel Ingress contract, including the body-size pin |
| `oicm-litellm-layer/tests/deploy/test_dev_prod_parity.py` | dev and prod object declaration parity |
| `oicm-litellm-layer/mcp-source-link/internal/*/\*_test.go` | Go behavior, race, and stream tests |
| `.github/workflows/test-mcp-source-link.yml` | gofmt, vet, staticcheck, race, proto drift, pinned protoc |

## Docs that own the story

| Path | Purpose |
|---|---|
| `oicm-litellm-layer/docs/mcp-source-broker/README.md` | slice index |
| `oicm-litellm-layer/docs/mcp-source-broker/running-system/README.md` | owning implementation record, status register, port map, step evidence |
| `oicm-litellm-layer/docs/mcp-source-broker/measurements/go-driver-concurrency-2026-11-18.md` | concurrency numbers |
| `oicm-litellm-layer/docs/mcp-source-broker/decisions/approaches/*.md` | enrollment, BYOM, multi-replica debates |
| `oicm-litellm-layer/docs/usage-guides/MCP_SOURCE_AGENT_GUIDE.md` | source-broker operator guide, prod vs dev, failure table |
| `oicm-litellm-layer/docs/access-control/sso-application-integration.md` | application SSO/JWT/API-key and resource-provisioning guide |
| `oicm-litellm-layer/docs/mcp/opencode-sso-oauth-skills-pilot.md` | gated OpenCode implementation plan, not completed capability claims |
| `oicm-litellm-layer/docs/mcp/opencode-pilot-evidence.md` | partial OpenCode run evidence; native MCP sign-in works, full execution and private skills remain unfinished |
| `oicm-litellm-layer/docs/docs-map.md` | one-line purpose per doc |
| `oicm-litellm-layer/mkdocs.yml` | nav tree for the docs site |
