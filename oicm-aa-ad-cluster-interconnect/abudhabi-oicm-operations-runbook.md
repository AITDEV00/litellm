# Abu Dhabi OICM operations runbook: stuck deployments and the service account

Date: 2026-10-06
Cluster: Abu Dhabi (`prd-oi-*` nodes), OICM 1.7.1, namespace `mlops` + `adeo`
Purpose: two operational fixes that came out of the Abu Dhabi integration.

1. Clearing a deployment stuck in `Undeploying`, which no API call can remove.
2. Creating the `svc-litellm-controller` service account the controller needs.

Everything here is driven from Al Ain, because the Abu Dhabi API server is only
reachable from the Al Ain gateway node. See
`abudhabi-rke2-cert-renewal-runbook.md` for the relay pod setup, and
`submariner-SHARED-reference.md` section 8 for the no-SSH access pattern.

## Part 1: a deployment stuck in `Undeploying`

### Symptom

The OICM UI shows the deployment with an `Undeploying` badge and a red
"Deployment failed" banner. Its `error_msg` reads:

```
401: API error when deleting resources. [Request ID: a85ec8a9-1c1a-48ad-b6fc-e2e8822cf937]
```

Both delete routes refuse it:

```
DELETE /v1/workspaces/{ws}/deployments/{id}/undeploy  -> 400 {"message":"Can't undeploy now. Try again later"}
DELETE /v1/workspaces/{ws}/deployments/{id}           -> 400 {"message":"Can't delete deployment while it's in 'Undeploying' status"}
```

### Why it cannot be fixed through the API

This is a deadlock in the OICM 1.7.1 backend, not a permissions problem. Read
from the shipped bytecode, `DeploymentService` and `DeploymentEntity` define two
disjoint sets of acceptable statuses:

- `DeploymentEntity.can_undeploy()` returns true only for
  `(AVAILABLE, READY, QUEUED, DEPLOYING, INITIALIZING)`.
- `DeploymentService.IDLE_STATUSES`, the set `delete` requires, is
  `(CREATED, STOPPED, FAILED)`.

`Undeploying` is in neither set, so `undeploy` says "try again later" and
`delete` says "can't delete while Undeploying". Neither ever succeeds, and there
is no API path out. `X-Soft-Delete` does not help: that header is declared only
on `/workspaces/{id}/delete`, not on any deployment route.

The status the guards read is `DeploymentEntity.status`, a computed property
that falls through to `oicm_status` when the three in-progress flags are false.
That is the single field worth changing.

### Why the record got that way

On 2026-06-17 the delete-by-label call failed with a transient Kubernetes auth
error. `K8sDeleteClient.delete_resources_by_label` catches it and writes the
message into `error_msg`. The Kubernetes objects were eventually removed anyway,
but nothing reset the record, so it froze in `Undeploying` with an 80 GiB PVC
left behind.

### Confirm the k8s side is already clean before touching anything

Check for surviving objects, and for the PVC the workload run tracks:

```bash
export KUBECONFIG=~/.kube/alain-oicm.conf
AD='KUBECONFIG=/tmp/adkc kubectl'
kubectl -n oik8s-cilium-system exec adrelay -- chroot /host sh -c \
  "$AD -n adeo get deploy,pod,svc,pvc --no-headers | grep -i <deployment-id-prefix>"
```

In the case this was written from, only the PVC remained. Confirm nothing mounts
it, and that the PV will be reclaimed:

```bash
kubectl -n oik8s-cilium-system exec adrelay -- chroot /host sh -c \
  "$AD -n adeo get pvc <pvc-name> --no-headers; $AD get pv <pv-name> --no-headers"
```

An empty owner-reference list and `RECLAIMPOLICY=Delete` means deleting the PVC
also removes the backing PV.

### The fix

Read the current state first, so the change is auditable:

```bash
kubectl -n oik8s-cilium-system exec adrelay -- chroot /host sh -c \
  "$AD -n mongodb exec mongodb-0 -c mongodb -- mongosh '<mlops-uri>' --quiet --eval \
   'printjson({s: db.deployment.findOne({id:\"<id>\"}).oicm_status})'"
```

Set `oicm_status` to a value the delete guard accepts. `Stopped` is the honest
choice for a workload that is already gone:

```javascript
db.deployment.updateOne({ id: "<deployment-id>" }, { $set: { oicm_status: "Stopped" } })
```

Do the API delete immediately, because the workload scanner may rewrite
`oicm_status` back to `Undeploying`:

```bash
# through the controller pod, which can already reach the clusterset names
curl -sS -X DELETE -H "Authorization: Bearer $TOKEN" \
  "$API/api/v1/workspaces/$WS/deployments/$DEPLOYMENT_ID"
```

A `200 {"message":"Deployment deleted successfully"}` soft-deletes the record:
`_deleted_at` is set and `_lifecycle_stage` becomes `deleted`. Then remove the
PVC:

```bash
kubectl -n oik8s-cilium-system exec adrelay -- chroot /host sh -c \
  "$AD -n adeo delete pvc <pvc-name> --wait=true"
```

### Verify

```bash
# 1 deployment left, and the stuck one gone from every route
curl -sS -H "Authorization: Bearer $TOKEN" "$API/api/v1/workspaces/$WS/deployments"
curl -sS -H "Authorization: Bearer $TOKEN" "$API/api/v1/workspaces/$WS/deployment_summary"
```

Expect the workspace list to be clean and `deployment_summary` to return only
the deployments that are actually live.

### Known residue

The `workload_run` document keeps `status: "Terminating"` with
`is_terminating: true`. It is driven by the workload scanner and the deployment
no longer reads it, so it is cosmetic. `celery_taskmeta` also still holds the
original `tasks.start_deployment` entry with status `SUCCESS`.

## Part 2: the `svc-litellm-controller` service account

Abu Dhabi realm `adeo` had no service account, so the controller could only
authenticate as a person. This creates one with the same shape Al Ain uses.

### Prerequisites

Keycloak admin, from `keycloak-secret` key `admin-password` in namespace
`keycloak`, realm `master`, client `admin-cli`. Realm `adeo` client `adeo` is
already public with `directAccessGrantsEnabled: true`, which is what the
password grant needs. Do not enable `serviceAccountsEnabled`; the controller
uses the password grant.

### Steps

Create the user, set a non-temporary password, and assign the three client
roles:

```
POST /admin/realms/adeo/users                     -> 201
PUT  /admin/realms/adeo/users/{id}/reset-password -> 204   {type: password, temporary: false}
POST /admin/realms/adeo/users/{id}/role-mappings/clients/{clientUuid} -> 204
     roles: admin, default, rsc_groups_full_access
```

### The trap: the declarative user profile

Creating the user and setting the password is not enough. Realm `adeo` on
Keycloak 25.0.4 uses a declarative user profile in which `email` is
`required: {roles: ["user"]}`. A user created without an email has
`requiredActions: []` and looks complete, but the password grant fails:

```
400 {"error":"invalid_grant","error_description":"Account is not fully set up"}
```

Because `requiredActions` is empty, checking it gives a false all-clear. Inspect
the profile instead:

```bash
curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$KC/admin/realms/adeo/users/profile"
```

Set an email and mark it verified, then retry:

```
PUT /admin/realms/adeo/users/{id}   {email: "...", emailVerified: true, ...}  -> 204
```

### Verify

The password grant must return a token, and that token must reach every route
the controller uses:

```
POST /realms/adeo/protocol/openid-connect/token  -> 200
GET  /api/v1/workspaces/{ws}/deployment_summary  -> 200
GET  /api/v1/workspaces/{ws}/deployments         -> 200
GET  /api/v1/workspaces/{ws}/deployments/{id}    -> 200
GET  /api/v1/workspaces/{ws}/deployments/{id}/health -> 200, is_ready true
GET  /api/entities/workspace                     -> 200
```

Confirm the token carries `resource_access.adeo.roles` with all three roles.

### Where the credentials live

Secret `ad-oicm-service-account` in namespace `adeo-litellm`, keys `username`
and `password`. Never commit the password, and do not put it in a doc or an
issue. To rotate, reset the password through the admin API and update the
secret.

## Controller configuration for Abu Dhabi

| Variable | Value |
|---|---|
| `OICM_BASE_URL` | `http://s-mlops-nginx-be-app.mlops.svc.clusterset.local` |
| `OICM_AUTH_URL` | `http://keycloak.keycloak.svc.clusterset.local` |
| `OICM_REALM` | `adeo` |
| `OICM_CLIENT_ID` | `adeo` |
| `OICM_AUTH_GRANT_TYPE` | `password` |
| `OICM_WORKSPACE_ID` | `d7bbdde9-c8e2-4c43-8b63-4fc1f0545686` |
| `OICM_USERNAME` / `OICM_PASSWORD` | from `ad-oicm-service-account` |

The `clusterset.local` names resolve only from pods on `adeo-gpu-03`. A
controller pod scheduled anywhere else will not reach them.

## Still outstanding

`is_deployment_available()` requires `status_detail[].metadata.ready`, which Abu
Dhabi's OICM does not populate. A Ready deployment with a Running pod therefore
evaluates to `serving_available=False`. See
`abudhabi-oicm-rest-api-export.md` for the detail. This is a code change, not an
operational one.
