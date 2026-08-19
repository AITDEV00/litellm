# Deployment & Cluster

How the OICM layer is deployed to the Kubernetes cluster and how to apply /
rollout changes safely.

## Manifests (`deploy/`)

Manifests are grouped by environment: `deploy/prod/`, `deploy/dev/`, `deploy/rollback/`.

| Manifest | Resources | Applies to |
|----------|-----------|-----------|
| `deploy/prod/litellm-proxy.yaml` | Deployment `litellm-proxy`, Secret `litellm-master-key`, Secret `litellm-db-credentials`, ConfigMap `litellm-config`, ConfigMap `litellm-hooks`, Secret `litellm-redis-password`, Service, PDB | `mlops` |
| `deploy/prod/discovery-controller.yaml` | Deployment `oicm-discovery-controller` + RBAC + ServiceAccount | `mlops` |
| `deploy/prod/litellm-redis.yaml` | Redis StatefulSet | `mlops` |
| `deploy/prod/litellm-ingress.yaml` | Ingress | `mlops` |
| `deploy/prod/litellm-servicemonitor.yaml` | Prometheus ServiceMonitor | `mlops` |
| `deploy/dev/litellm-proxy-dev.yaml` | Dev variant of the proxy (extended logs, `--reload`) | `mlops` |
| `deploy/dev/discovery-controller-dev.yaml` | Dev variant of the controller | `mlops` |
| `deploy/rollback/litellm-proxy-rollback-jya0-v1.96.2.yaml` | Rollback manifest pinned to image `v1.96.2` | `mlops` |

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
kubectl -n mlops rollout restart deployment/litellm-proxy
kubectl -n mlops rollout restart deployment/oicm-discovery-controller
kubectl -n mlops rollout status deployment/litellm-proxy
kubectl -n mlops rollout status deployment/oicm-discovery-controller
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