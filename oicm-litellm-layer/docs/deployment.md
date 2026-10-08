# Deployment & Cluster

How the OICM layer is deployed to the Kubernetes cluster and how to apply /
rollout changes safely.

## Manifests (`deploy/`)

The gateway is built with Kustomize from one base, so dev and prod are the same
structure with different values. Everything else is grouped by environment.

```
deploy/
  base/
    shared/    litellm-hooks ConfigMap + litellm-redis-password Secret, used by
               BOTH environments unsuffixed, so a dev-only edit cannot change
               what prod serves
    gateway/   the gateway objects with production names and values: the three
               Secrets, litellm-config, Deployment, Service, PDB
  overlays/
    prod/      applies base/ unchanged; renders exactly what prod runs
    dev/       applies base/gateway with nameSuffix: -dev plus two patches
               (gateway/deployment.yaml, gateway/config.yaml) that hold the
               complete dev/prod delta. base/shared comes in unsuffixed.
  prod/, dev/, rollback/   non-gateway resources (controller, redis, ingress,
               postgres, janitor, servicemonitor, rollback sets)
  recovery/    DISASTER RECOVERY RECORDS, never applied. The Cluster spec that
               rebuilds prod Postgres from the litellm-recovery-* snapshots, and
               the PVCs that bind the older PVs still holding pre-migration data.
               Both declare objects that already exist live, so applying them
               overwrites the running cluster instead of creating anything
```

The dev overlay's two patch files are the single place the dev/prod difference
lives. `tests/deploy/test_dev_prod_parity.py` renders both overlays and fails if
they diverge in any way those patches do not declare.

| Manifest | Resources | Applies to |
|----------|-----------|-----------|
| `deploy/overlays/prod/` | Deployment `litellm-proxy`, Secrets `litellm-master-key` / `litellm-salt-key` / `litellm-db-credentials`, ConfigMaps `litellm-config` / `litellm-hooks`, Secret `litellm-redis-password`, Service, PDB | `adeo-litellm` |
| `deploy/overlays/dev/` | The same objects with `-dev` names, except the shared `litellm-hooks` and `litellm-redis-password` | `adeo-litellm` |
| `deploy/prod/discovery-controller.yaml` | Deployment `oicm-discovery-controller` + RBAC + ServiceAccount | `adeo-litellm` (+ ClusterRole bindings reaching `adeo`) |
| `deploy/prod/litellm-redis.yaml` | Redis StatefulSet | `redis` |
| `deploy/prod/litellm-ingress.yaml` | Ingress | `adeo-litellm` |
| `deploy/prod/litellm-servicemonitor.yaml` | Prometheus ServiceMonitor | `adeo-litellm` |
| `deploy/prod/litellm-network-policy-to-adeo.yaml` | NetworkPolicy (controller -> `adeo` namespace) | `adeo` |
| `deploy/prod/litellm-postgres-cluster.yaml` | CNPG Postgres cluster | `adeo-litellm` |
| `deploy/prod/spend-logs-janitor/` | Spend-logs janitor CronJob + PVC + scripts | `adeo-litellm` |
| `deploy/oicm/sources.yaml` | `oicm-sources` ConfigMap (OICM status sources the controller polls) | `adeo-litellm` |
| `deploy/oicm/exclusions.yaml` | `oicm-exclusions` ConfigMap (prod excluded models, empty) | `adeo-litellm` |
| `deploy/dev/oicm-exclusions-dev.yaml` | `oicm-exclusions-dev` ConfigMap (dev excluded models) | `adeo-litellm` |
| `deploy/oicm/provision-*.yaml` | OICM service-account provisioning Jobs | `adeo-litellm` |
| `deploy/oicm/service-account-secret.yaml` | TEMPLATE for the four OICM Secrets (passwords are `REPLACE_ME`); written by `scripts/make_service_account_secrets.sh`, never applied | `adeo-litellm` |
| `deploy/dev/discovery-controller-dev.yaml` | Dev variant of the controller | `adeo-litellm` |
| `deploy/dev/litellm-postgres-dev-cluster.yaml` | Dev Postgres cluster | `adeo-litellm` |
| `deploy/dev/litellm-servicemonitor-dev.yaml` | Dev ServiceMonitor | `adeo-litellm` |
| `deploy/dev/spend-logs-janitor/` | Dev janitor (adds README) | `adeo-litellm` |
| `deploy/rollback/litellm-proxy-rollback-jya0-v1.97.0.yaml` | Rollback manifest pinned to image `jya0-v1.97.0` (newest) | `adeo-litellm` |
| `deploy/rollback/litellm-proxy-rollback-jya0-v1.96.2.yaml` | Rollback manifest pinned to image `jya0-v1.96.2` | `adeo-litellm` |
| `deploy/rollback/litellm-proxy-rollback-key.yaml` | Secret for rollback apply | `adeo-litellm` |
| `deploy/rollback/discovery-controller-rollback-key.yaml` | Secret for controller rollback apply | `adeo-litellm` |
| `deploy/recovery/restore-prod-postgres-from-snapshots.yaml` | DO NOT APPLY. Cluster spec that rebuilds prod Postgres from the `litellm-recovery-*` snapshots | `adeo-litellm` |
| `deploy/recovery/bind-old-postgres-pvs.yaml` | DO NOT APPLY. PVCs binding the older PVs that still hold pre-migration data | `adeo-litellm` |

## Apply

```bash
# from oicm-litellm-layer/
kubectl apply -k deploy/overlays/prod      # gateway
kubectl apply -f deploy/prod/discovery-controller.yaml
```

or via the Makefile:

```bash
make deploy
```

To see what an overlay would produce without applying it:

```bash
kubectl kustomize deploy/overlays/prod
kubectl kustomize deploy/overlays/dev
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