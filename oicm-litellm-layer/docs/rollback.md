# Production configuration rollback

Captured read-only from the running production cluster on 2026-11-12, before the
v1.108.5 gateway rollout. The source gateway tag is `jya0-v1.106.6`, and the
source controller tag is `0.1.0-20261111-7a1e97a`. Both are pinned in the backup
with their actual running image digests, so later image-tag changes cannot alter
this snapshot.

The six YAML documents contain 31 production resources:

| File | Contents |
| --- | --- |
| `gateway.yaml` | Gateway Deployment, Service, PDB, settings/hooks/logo and OICM source/exclusion ConfigMaps, plus credential references |
| `controller.yaml` | Controller Deployment, ServiceAccount, Role and RoleBinding |
| `store.yaml` | Redis and RustFS definitions, ConfigMaps and credential references |
| `database.yaml` | Production Postgres Cluster definition, archive PVC and spend-logs CronJob |
| `monitoring.yaml` | Production ServiceMonitor |
| `access.yaml` | Production Ingress and ingress network policy |

Credential-bearing Secrets are represented with `type: Opaque` but deliberately
no `data` or `stringData`. Applying them to the existing production environment
preserves existing credentials, including the master key and salt key. Do not
apply this backup on a fresh cluster without its credential Secrets. Existing
external Secrets, the logo TLS certificate and operator-generated Secrets are
left unchanged. External source credentials and the ingress TLS Secret are left unchanged as well.

This snapshot restores exported Kubernetes configuration, not the contents of
Postgres, Redis, RustFS or persistent volumes. Existing data volumes are
preserved. The completed RustFS bucket-bootstrap Job is intentionally omitted.
Unrelated development resources and model-server policies are excluded.

## Preview and apply

From `oicm-litellm-layer/`, verify the target credentials first. This command
validates against the API server but does not modify the cluster:

```bash
export KUBECONFIG="$HOME/.kube/alain-oicm.conf"
kubectl get nodes
kubectl config current-context
kubectl apply --dry-run=server -f deploy/rollback/
kubectl diff -f deploy/rollback/
```

Review the complete diff carefully. Production configuration may have drifted
from the base manifests. In particular, do not inadvertently revert Secret values,
roll back out-of-band Postgres changes or retain an older static configuration
when this folder was intended to be refreshed. If the correct target is the
current Git release instead of the running state captured on 2026-11-12, use
`make litellm-src-deploy` and `make controller-deploy-prod BUILD=0
TAG=0.1.0-20261111-969bdaf` rather than this directory.

Restore it with:

```bash
export KUBECONFIG="$HOME/.kube/alain-oicm.conf"
kubectl apply -f deploy/rollback/
```

Verify both workload revisions and availability:

```bash
kubectl -n adeo-litellm get deployments litellm-proxy oicm-discovery-controller \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'
kubectl -n adeo-litellm rollout status deployment/litellm-proxy --timeout=300s
kubectl -n adeo-litellm rollout status deployment/oicm-discovery-controller --timeout=300s
kubectl -n adeo-litellm get pods
```

A changed image reference or pod declaration triggers a normal rolling update.
Do not add a blanket `--prune`: this is a restore, not a delete-everything-new
operation. Resources created only by later deployments must be reviewed and
removed separately. Persistent volumes, databases, RustFS objects, registered
DB models and migrations are not rolled back by this snapshot.

The backup was validated with `kubectl apply --dry-run=server -f deploy/rollback/`
against all 31 live production resources before publication. That validates
admission and does not execute a restore roll or substitute for a real database
backup.
