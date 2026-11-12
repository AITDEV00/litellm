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
```

Postgres recovery is a runbook, not a manifest, because a
`spec.bootstrap.recovery` cluster spec rebuilds the live database from a snapshot
and CNPG has no retention for snapshots. See
`docs/runbooks/postgres-backup-and-recovery.md`.

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
| `deploy/rollback/` | 2026-11-12 live-production configuration snapshot: v1.106.6 gateway and controller pinned by digest | `adeo-litellm`, plus watched/network-policy namespaces |

## Configuration rollback

`deploy/rollback/` contains a flat 31-resource snapshot of the **live production
configuration before the v1.108.5 gateway rollout**. This includes the image
digests, ConfigMaps, RBAC and supporting resources. Credential Secrets are
stubs that preserve existing contents when applied to the same cluster. This
is a configuration restore, not a data or fresh-cluster restoration.

```bash
export KUBECONFIG="$HOME/.kube/alain-oicm.conf"
kubectl config current-context
kubectl diff -f deploy/rollback/
kubectl apply -f deploy/rollback/
kubectl -n adeo-litellm rollout status deployment/litellm-proxy --timeout=300s
kubectl -n adeo-litellm rollout status deployment/oicm-discovery-controller --timeout=300s
```

Review the complete diff first. Do not add `--prune`. Later-created resources
need explicit review and deletion. Database records, persistent volumes,
object-store contents and completed Jobs are not restored. The full
[rollback runbook](rollback.md) has the inventory, limitations, and commands.

## Apply

The Makefile composes deployments from per-object-group primitives, so you never
have to apply a whole environment when only one piece changed. Each of these
takes the usual variables (`TAG`, `BUILD`, `NAMESPACE`, `ROLLOUT_TIMEOUT`), see
[Makefile reference](Makefile-reference.md).

```bash
make controller-deploy-prod     # build, push, pin, apply, roll out the controller
make litellm-src-release        # build, push, pin, and roll out the gateway
```

`make deploy` and `make deploy-dev` still work as the convenience compositions
(`prod-controller prod-gateway` and `dev-controller dev-gateway` respectively).
Note that `prod-gateway` and `dev-gateway` only apply the overlay, which does not
restart the gateway, so an unchanged image keeps its running container. Use
`litellm-src-release` when the image should move too.
To apply an already-validated controller tag without rebuilding, pass `BUILD=0`
and the tag:

```bash
make controller-deploy-prod BUILD=0 TAG=0.1.0-20261103-4d922d8
```

For source changes, build the controller first. Do not take a successful
`make dev-controller` / `make prod-controller` as an image deployment: those
primitives apply the manifest's existing pin and the environment's exclusions,
but do not build, push, or repoint the image. Build and validate dev, promote the
same bytes without rebuilding, then wait through a later full reconciliation:

```bash
make controller-build-push TAG=<version>
make controller-deploy-dev TAG=<version>
kubectl -n adeo-litellm logs deploy/oicm-discovery-controller-dev
make controller-deploy-prod BUILD=0 TAG=<the-dev-validated-tag>
kubectl -n adeo-litellm logs deploy/oicm-discovery-controller
```

For aliases, expect `/model/info` to show the canonical and short names with
the same deployment ID as the controller learns them. Exercise real inference
for both names. A subsequent full sync must retain that deployment rather than
logging its deletion or registration; replica readiness and a successful startup
sync alone do not establish long-term behavior.

Gateway and controller roll out independently. Restarting one does not restart
the other.

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

The cluster API is reached through an SSH tunnel to the bastion, which is the
only host that can reach the API server. Corporate DNS resolves
`api.adeoaiengine.ecouncil.ae` to `127.0.0.1` on purpose: the name works only
while the tunnel is up. When kubectl fails with
`dial tcp 127.0.0.1:6443: connection refused`, the tunnel has died; re-open it
with the command in
[Datasource Local Validation](runbooks/DATASOURCE-LOCAL-VALIDATION.md#step-1-open-the-ssh-tunnel).

The kubeconfig on this machine is `~/.kube/alain-oicm.conf`, which is also the
Makefile default for `ALAIN_KUBECONFIG`.

## Monitoring

- Prometheus metrics at `/metrics` (proxy), scraped by a ServiceMonitor.
- The controller exposes `/health` on `HEALTH_PORT` (default 8090), served
  inline from `controller/controller.py` (liveness/readiness probes).