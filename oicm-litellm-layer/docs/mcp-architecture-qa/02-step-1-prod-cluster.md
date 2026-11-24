# Step 1: what is already standing in the prod cluster

Before any user acts, prod has three things: a gateway that knows a broker exists, a broker Pod with a Python brain and a Go muscle, and a public GRPC doorway on 443.

## How prod is assembled

Prod is `deploy/overlays/prod/kustomization.yaml`, which layers four resource groups and one patch:

```yaml
resources:
  - ../../base/shared      # litellm-hooks ConfigMap, redis password, UNSUFFIXED
  - ../../base/gateway     # gateway Deployment, Service, PDB, 4 Secrets, config
  - ../../base/rustfs      # object store
  - mcp-source-broker      # the feature
patches:
  - path: gateway/deployment.yaml   # prod-only gateway env + pinned digest
```

The ingresses are **not** in this kustomization. `deploy/prod/litellm-ingress.yaml` and `deploy/prod/mcp-tunnel-ingress.yaml` are applied separately. That is why you can render prod cleanly and still be missing the tunnel route.

## Pod A: the gateway

- File: `deploy/base/gateway/proxy/deployment.yaml`, patched by `deploy/overlays/prod/gateway/deployment.yaml`.
- Two replicas, both pinned to node `adeo-gpu-03` because of a Cilium BPF and Submariner return-path limitation documented in the manifest header. Do not add a second gateway node without addressing that.
- Listens on 4000.
- Prod turns the feature on and wires it with five env vars added by the patch:

| Env var | Value | Purpose |
|---|---|---|
| `LITELLM_PUBLIC_BASE_URL` | `https://litellm.ecouncil.ae` | what the gateway tells clients and renders into the installer |
| `MCP_SOURCE_BROKER_ENABLED` | `true` | gates `mount_mcp_sources_when_enabled` in `proxy_server.py`, so the `/mcp-sources` and `/mcp-source-link-sources/*` routes exist at all |
| `MCP_SOURCE_BROKER_DATA_PLANE_URL` | `http://litellm-mcp-broker.adeo-litellm.svc.cluster.local:8888` | the URL written into every MCP server row as the dispatch target |
| `MCP_SOURCE_BROKER_PEPPER` | secret `litellm-mcp-source-broker` key `pepper` | derives and verifies worker credentials |
| `MCP_SOURCE_BROKER_SERVICE_TOKEN` | same secret key `service-token` | the bearer the gateway and worker exchange on the data plane |

The image is pinned to a digest `litellm-src:jya0-v1.108.5-prod@sha256:c2beba4b...` because dev rebuilds would otherwise overwrite the mutable tag.

## Pod B: the broker (two containers, one Pod)

- Files: `deploy/overlays/prod/mcp-source-broker/broker.yaml` plus the patch `source-link-patch.yaml`.

Container `broker` (Python):

- Image `litellm-mcp-broker:jya0-v1.108.5-prod@sha256:2728d6a7...`
- Command `python -m litellm.proxy.mcp_source_broker.main`
- Env: `MCP_SOURCE_BROKER_ENABLED=true`, `MCP_SOURCE_LINK_ENABLED=true`, the pepper and service token, and `DATABASE_URL` from secret `litellm-db-credentials` (the same one the gateway uses).
- Port 9091, readiness and liveness on `/health`.
- Owns the peer list, health ingestion, lifecycle, and the loopback revoke fan-out.

Container `source-link` (Go):

- Image `mcp-source-link-worker:jya0-v1.108.5-prod@sha256:ce3c214c...`
- Command `["/source-link", "worker"]`
- Env: `MCP_SOURCE_LINK_CONTROL_URL=http://127.0.0.1:9091`, service token, `MCP_SOURCE_LINK_DATA_LISTEN=0.0.0.0:9092`, `MCP_SOURCE_LINK_REVERSE_LISTEN=0.0.0.0:8093`.
- Ports 9092 `link-data`, 8093 `link-tunnel`, readiness `/readyz` on 9092.
- Runs as `runAsNonRoot`, uid 10001. The image is `FROM scratch`, so `kubectl exec` has no shell. Debug with `kubectl logs` or `kubectl debug`, not `exec sh`.

Why one Pod: the Go revoke endpoint accepts **loopback callers only**, and the Python control app reaches it at `127.0.0.1:9092`. Two Deployments would break that contract.

Service `litellm-mcp-broker` final port set after the kustomize merge:

| Port | Name | Target |
|---|---|---|
| 8888 | `data` | `link-data` (9092) |
| 8093 | `link-tunnel` | 8093 |
| 9091 | `remote-control` | 9091 |

## Pod C: public ingress path

Not a Pod, but the third pillar. Two Ingresses, one hostname:

- `litellm-proxy`, path `/` to `litellm-proxy:4000`, HTTP, from `deploy/prod/litellm-ingress.yaml`. Also sets `proxy-body-size: "0"`, which is what keeps the long-lived tunnel POST from being cut at 1 MiB.
- `litellm-mcp-tunnel`, path `/grpctunnel.v1.TunnelService/` to `litellm-mcp-broker:link-tunnel` (8093), `GRPC`, from `deploy/prod/mcp-tunnel-ingress.yaml`.

The tunnel Ingress needs its own object because `backend-protocol: GRPC` is per-Ingress, not per-path.

## NetworkPolicy: what may talk to what

File `deploy/overlays/prod/mcp-source-broker/network-policy.yaml`.

| Direction | From | To | Ports | Rationale |
|---|---|---|---|---|
| ingress | `app: litellm-proxy` | broker pod | 9091, 9092 | the gateway's data-plane dispatch and admin control |
| ingress | any | broker pod | 8093 | connectors dial in from unknown addresses, so this must be open |
| egress | broker pod | kube-dns | 53 | resolve `litellm-mcp-broker`, Postgres, RustFS names |
| egress | broker pod | any pod in ns | 5432, 9000 | Postgres and RustFS |
| ingress | gateway or broker pod | `app: rustfs` | 9000 | artifact and release reads and writes |

## Secrets both processes share

Secret `litellm-mcp-source-broker`, from `deploy/base/gateway/secrets/litellm-mcp-source-broker.yaml`. Two keys:

- `pepper`: derives the worker credential at issuance and re-derives it to verify at tunnel open. If gateway and broker read different pepper values, every connector gets `connector unauthorized` even though the peer list is non-empty.
- `service-token`: the bearer shared between gateway and worker on the data plane, and between worker and broker on loopback.

## What is NOT here yet at the end of step 1

No source is enrolled. No connector is connected. Nothing listens per source. The broker Pod is up and idle, the worker is up with zero members, and the gateway has the routes mounted but the MCP server table has no source rows. The system is armed, not loaded.

## Where to verify this yourself

```sh
kubectl -n adeo-litellm get deploy litellm-proxy litellm-mcp-broker -o wide
kubectl -n adeo-litellm get svc litellm-proxy litellm-mcp-broker
kubectl -n adeo-litellm get ingress litellm-proxy litellm-mcp-tunnel
kubectl -n adeo-litellm get networkpolicy litellm-mcp-broker litellm-rustfs-source-broker
kubectl -n adeo-litellm get secret litellm-mcp-source-broker -o jsonpath='{.data}' | head
kubectl kustomize deploy/overlays/prod | grep -A3 'name: litellm-mcp-broker'
```

## Files referenced in this step

- `oicm-litellm-layer/deploy/overlays/prod/kustomization.yaml`
- `oicm-litellm-layer/deploy/overlays/prod/gateway/deployment.yaml`
- `oicm-litellm-layer/deploy/overlays/prod/mcp-source-broker/broker.yaml`
- `oicm-litellm-layer/deploy/overlays/prod/mcp-source-broker/source-link-patch.yaml`
- `oicm-litellm-layer/deploy/overlays/prod/mcp-source-broker/network-policy.yaml`
- `oicm-litellm-layer/deploy/base/gateway/proxy/deployment.yaml`
- `oicm-litellm-layer/deploy/base/gateway/secrets/litellm-mcp-source-broker.yaml`
- `oicm-litellm-layer/deploy/prod/litellm-ingress.yaml`
- `oicm-litellm-layer/deploy/prod/mcp-tunnel-ingress.yaml`
