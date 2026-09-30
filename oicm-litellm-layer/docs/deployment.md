# Deployment & Cluster

How the OICM layer is deployed to the Kubernetes cluster and how to apply /
rollout changes safely.

## Manifests (`deploy/`)

Manifests are grouped by environment: `deploy/prod/`, `deploy/dev/`, `deploy/rollback/`.

| Manifest | Resources | Applies to |
|----------|-----------|-----------|
| `deploy/prod/litellm-proxy.yaml` | Deployment `litellm-proxy`, Secret `litellm-master-key`, Secret `litellm-db-credentials`, ConfigMap `litellm-config`, ConfigMap `litellm-hooks`, Secret `litellm-redis-password`, Service, PDB | `adeo-litellm` |
| `deploy/prod/discovery-controller.yaml` | Deployment `oicm-discovery-controller` + RBAC + ServiceAccount | `adeo-litellm` (+ ClusterRole bindings reaching `adeo`) |
| `deploy/prod/litellm-redis.yaml` | Redis StatefulSet | `redis` |
| `deploy/prod/litellm-ingress.yaml` | Ingress | `adeo-litellm` |
| `deploy/prod/litellm-servicemonitor.yaml` | Prometheus ServiceMonitor | `adeo-litellm` |
| `deploy/prod/litellm-network-policy-to-adeo.yaml` | NetworkPolicy (controller -> `adeo` namespace) | `adeo` |
| `deploy/prod/litellm-postgres-cluster.yaml` | CNPG Postgres cluster | `adeo-litellm` |
| `deploy/prod/litellm-postgres-recovery.yaml` | Postgres recovery resources | `adeo-litellm` |
| `deploy/prod/old-postgres-pvcs.yaml` | Old Postgres PVCs (recovery leftovers) | `adeo-litellm` |
| `deploy/prod/spend-logs-janitor/` | Spend-logs janitor CronJob + PVC + scripts | `adeo-litellm` |
| `deploy/dev/litellm-proxy-dev.yaml` | Dev variant of the proxy (extended logs, `--reload`) | `adeo-litellm` |
| `deploy/dev/litellm-config-dev.yaml` | Dev ConfigMap (separate from prod so config changes are testable on dev) | `adeo-litellm` |
| `deploy/dev/discovery-controller-dev.yaml` | Dev variant of the controller | `adeo-litellm` |
| `deploy/dev/litellm-postgres-dev-cluster.yaml` | Dev Postgres cluster | `adeo-litellm` |
| `deploy/dev/litellm-servicemonitor-dev.yaml` | Dev ServiceMonitor | `adeo-litellm` |
| `deploy/dev/spend-logs-janitor/` | Dev janitor (adds README) | `adeo-litellm` |
| `deploy/rollback/litellm-proxy-rollback-jya0-v1.97.0.yaml` | Rollback manifest pinned to image `jya0-v1.97.0` (newest) | `adeo-litellm` |
| `deploy/rollback/litellm-proxy-rollback-jya0-v1.96.2.yaml` | Rollback manifest pinned to image `jya0-v1.96.2` | `adeo-litellm` |
| `deploy/rollback/litellm-proxy-rollback-key.yaml` | Secret for rollback apply | `adeo-litellm` |
| `deploy/rollback/discovery-controller-rollback-key.yaml` | Secret for controller rollback apply | `adeo-litellm` |

## Apply

```bash
# from oicm-litellm-layer/
kubectl apply -f deploy/prod/litellm-proxy.yaml
kubectl apply -f deploy/prod/discovery-controller.yaml
```

or via the Makefile:

```bash
make deploy
```

## Rollout restart

Env vars from `secretKeyRef` are snapshotted when a pod is created. If you
change a Secret value, **you must restart the Deployment** for running pods to
pick up the new value. Kubernetes does not auto-restart on secret change.

```bash
kubectl -n adeo-litellm rollout restart deployment/litellm-proxy
kubectl -n adeo-litellm rollout restart deployment/oicm-discovery-controller
kubectl -n adeo-litellm rollout status deployment/litellm-proxy
kubectl -n adeo-litellm rollout status deployment/oicm-discovery-controller
```

!!! danger "Rotating the master key breaks both"
    `litellm-master-key` is read by **both** the proxy and the controller. If you
    only restart the proxy, the controller keeps sending the old key and model
    discovery breaks. See [Credentials](credentials.md) for the full runbook.

## Cluster access

The cluster API is reached through an SSH tunnel to the VM that has the
kubeconfig. See `COMMANDS-CONTEXT.txt` at the repo root for the tunnel command
and the `~/.kube/oicm-alain.conf` kubeconfig.

## Monitoring

- Prometheus metrics at `/metrics` (proxy), scraped by a ServiceMonitor.
- The controller exposes `/health` on `HEALTH_PORT` (default 8090), served
  inline from `controller/controller.py` (liveness/readiness probes).