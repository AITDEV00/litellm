# Abu Dhabi OICM REST API over Submariner: findings and blocker

Date: 2026-10-06
Purpose: export the Abu Dhabi OICM REST API and auth so the controller can be
pointed at the Abu Dhabi cluster the same way it is pointed at Al Ain.

Outcome: the network is **fixed**, the exports are reachable, and the
**credential blocker is resolved**. A tenant-realm account works end to end: the
controller's own endpoint returns 200 with real deployments. One API-shape gap
remains and is documented below.

## Status after the certificate fix

The RKE2 certificate fault that made the exports unreachable is fixed. See
`abudhabi-rke2-cert-renewal-runbook.md`. With that done, from the Al Ain gateway
host, and from a pod on it:

| Target | Result |
|---|---|
| `242.0.0.251/realms/adeo` (keycloak) | 200 |
| `242.0.0.252/api/openapi/openapi.json` | 200 |
| `242.0.0.252/api/v1/workspaces/{ws}/deployment_summary` | 403 pre-auth, **200 with 10 deployments post-auth** |

The clusterset DNS names resolve from inside a pod on the gateway node, so the
controller can use them directly:

```
http://s-mlops-nginx-be-app.mlops.svc.clusterset.local
http://keycloak.keycloak.svc.clusterset.local
```

Note these resolve only from pods on `adeo-gpu-03`, not from the host itself,
because that is where the Cilium BPF path to globalnet IPs exists.

## Credentials: resolved

OICM derives the tenant from the Keycloak realm in the JWT. The realm name **is**
the tenant name, and no tenant header or query parameter exists to override it.
Verified against Abu Dhabi's OpenAPI document: the global `security` is null, and
the only header parameter in the whole spec is `X-Soft-Delete`.

A user account in the `adeo` realm works. Authenticating as `jyao@ecouncil.ae`
against client `adeo` with a password grant returns a token, and that token sees
the tenant's workspaces and deployments:

```
POST /realms/adeo/protocol/openid-connect/token  -> 200, access_token
GET  /api/entities/workspace                     -> 200, 2 rows (both adeo)
GET  /api/v1/workspaces/{ws}/deployment_summary  -> 200, 10 items
```

The two workspace-scoped routes that 404'd before now resolve, because the token
is scoped to tenant `adeo` rather than `admin`.

Two credential sets that do **not** work, recorded so nobody retries them:

| Credential | Realm | Result |
|---|---|---|
| `oicm-admin` (found in `OICM_KEYCLOAK__PWD`) | `admin` | Token issued, `super_admin` role, but tenant data is invisible |
| `oiansible` infra account | `adeo` | `invalid_grant`, it is a host account, not a Keycloak user |

The `admin` realm token authenticates but is scoped to tenant `admin`, which has
no workspaces. Measured with it:

```
/api/entities/tenant     -> 200, 3 tenants visible (adeo, e2e, tenant02)
/api/entities/workspace  -> 200, 0 rows
/api/v1/workspaces/{any}/deployment_summary -> 404
```

The tenants are visible because `super_admin` can list them, but the workspaces
are not, and every workspace-scoped route 404s. That is tenant scoping working as
designed, not a broken API.

Realm `adeo` has 22 users and **no service account client**: its only clients are
`account`, `account-console`, `adeo` (public, direct grants on), `admin-cli`,
`broker`, `realm-management`, and `security-admin-console`, none with service
accounts enabled. A password-grant user account is therefore the only workable
shape today. Creating a dedicated service account in realm `adeo` remains the
cleaner long-term option, since it decouples the controller from a person's
account.

The account password is not recorded here. It belongs in the controller's
`oicm-status-api` secret only.

## What the data looks like

The workspace and deployment records were read directly from the OICM MongoDB to
establish ground truth, since the API would not show them:

| Workspace id | Name | Tenant | Deployments |
|---|---|---|---|
| `d7bbdde9-c8e2-4c43-8b63-4fc1f0545686` | Models | `adeo` | 39 |
| `04c61807-999b-4eb0-a539-3e13a9027fd1` | Models | `e2e` | 17 |
| `1329d700-880f-4143-8248-540f26ea14d9` | my_workspace | `adeo` | 0 |
| `3a35205f-eee0-4fcc-93fd-590e71f9b5bf` | test-workspace | `tenant02` | 0 |

So the workspace the controller needs is `d7bbdde9-c8e2-4c43-8b63-4fc1f0545686`
in tenant `adeo`. All four ids 404 with the `admin` token.

The live API returns 10 deployments for that workspace, not 39. The MongoDB count
includes soft-deleted rows; `deployment_summary` returns only live ones. Status
distribution is 1 Ready, 8 Stopped, 1 Undeploying.

The one Ready deployment is the reason the Submariner tunnel exists:

```json
{
  "deployment_id": "766b1720-f516-4077-b22c-6ce97c045470",
  "model_name": "zai-org/GLM-5.2-FP8",
  "model_server_name": "vLLM",
  "status": "Ready",
  "replicas": 1,
  "inference_task": "Text Generation",
  "resources": {"accelerator": "h200", "accelerator_count": 8, "use_gpu": true, "memory": 256, "storage": 1000}
}
```

Its pod `j-766b1720-...-lzfmt` is Running on `prd-infr-k8h200`, which matches the
ServiceImport `s-766b1720-f516-4077-b22c-6ce97c045470` and global IP
`242.0.0.253`. Deployment names in general look like
`Qwen/Qwen3-Next-80B-A3B-Instruct-deeeswo`, the same "GUI deployment name with a
random suffix" problem already documented for Al Ain, not the served model id.

## API-shape gap: `status_detail` has no `metadata`

The controller's availability logic reads `metadata.ready` off each
`status_detail` entry. Abu Dhabi's OICM does not populate `metadata` at all. Every
entry there carries only `kind`, `name`, `node`, `status`, and `status_msg`.

Side by side for the same object kind:

```
Al Ain OICM 1.15.19
  {"kind": "Pod", "name": "j-e1192ba7-...", "node": "adeo-gpu-b300-01", "status": "Running", "metadata": {"ready": true}}

Abu Dhabi OICM 1.7.1
  {"kind": "Pod", "name": "j-766b1720-...", "node": "prd-infr-k8h200", "status": "Running", "status_msg": null}
```

Parsing still succeeds, because `metadata` is optional. Availability does not.
Run the live Abu Dhabi payload through the current logic and a genuinely serving
deployment reports unavailable:

```
deployment 766b1720  OICM status='Ready'
   DeploymentStatusDetail: node=None status='Ready' metadata=None
   PodStatusDetail: node='prd-infr-k8h200' status='Running' metadata=None
-> is_deployment_available = False
```

That `False` flows into `OicmStatusSnapshot.serving_available`, so
`gateway_status` would report a Ready, pod-running deployment as not serving.

Note this is the same class of gap the availability docstring already calls out
for `apiVersion`: the code deliberately tolerates one missing field because OICM
leaves it null, and Abu Dhabi leaves a different one null. The fix is to treat
`metadata` as absent-safe in `_is_ready`, falling back to the entry's own
`status` and `node` when `metadata` is missing. `status` is populated in both
versions, so the fallback works for both.

## Controller configuration for Abu Dhabi

The controller needs no code change to authenticate. Every value is already a
supported environment variable; only the endpoints and the workspace id differ
from the Al Ain deployment:

| Variable | Al Ain (current) | Abu Dhabi |
|---|---|---|
| `OICM_BASE_URL` | `http://s-mlops-nginx-be-app.mlops.svc.cluster.local` | `http://s-mlops-nginx-be-app.mlops.svc.clusterset.local` |
| `OICM_AUTH_URL` | `http://oicm-keycloak-keycloakx-http.oicm-keycloak.svc.cluster.local` | `http://keycloak.keycloak.svc.clusterset.local` |
| `OICM_REALM` | `adeo` | `adeo` |
| `OICM_CLIENT_ID` | `adeo` | `adeo` |
| `OICM_AUTH_GRANT_TYPE` | `password` | `password` |
| `OICM_WORKSPACE_ID` | `dfec2a9f-...` | `d7bbdde9-c8e2-4c43-8b63-4fc1f0545686` |
| `OICM_USERNAME` / `OICM_PASSWORD` | from `oicm-status-api` | needs an `adeo` realm account |

`OICM_VERIFY_TLS=true` stays as is, since both endpoints are plain HTTP on the
clusterset network and TLS is not involved.

The `clusterset.local` names resolve only from pods on `adeo-gpu-03`, which is
where the Cilium BPF path to globalnet IPs exists. A controller pod scheduled
anywhere else will not reach them.

Still outstanding, independent of credentials:

1. **Make `metadata` absence-safe in `_is_ready`** so Abu Dhabi's payload does not
   report a serving deployment as unavailable. This is a code change.
2. **Create a service account in Abu Dhabi's realm `adeo`** and grant it
   workspace read, to replace the personal account. Needs Keycloak admin, which
   is available via `keycloak-secret/admin-password` in the `keycloak` namespace.

## Reusable command reference

## The blocker: Abu Dhabi kube-proxy is unauthorized

Both new global IPs are unreachable from Al Ain **and from the Abu Dhabi gateway
node itself**, while the pre-existing GLM export on `242.0.0.253` works fine from
both.

The reason is in the datapath. On the Abu Dhabi gateway node (`prd-oi-k8worker01`)
kube-proxy has a translation chain for the working export and none for the new
ones:

```
242.0.0.253  ->  1 nft rule,  jump KUBE-EXT-HR2T3QCYOBF42QT3
242.0.0.252  ->  0 nft rules
242.0.0.251  ->  0 nft rules
```

`KUBE-EXT-*` chains exist only for services kube-proxy has processed. The GLM
chain was programmed 88 days ago, when kube-proxy's credentials still worked.

kube-proxy on that node is failing every informer watch with `Unauthorized`:

```
W reflector.go:561 failed to list *v1.EndpointSlice: Unauthorized
E "Unhandled Error" err="Failed to watch *v1.EndpointSlice: failed to list *v1.EndpointSlice: Unauthorized"
W reflector.go:561 failed to list *v1.Service: Unauthorized
E "Unhandled Error" err="Failed to watch *v1.Service: failed to list *v1.Service: Unauthorized"
W reflector.go:561 failed to list *v1.Node: Unauthorized
```

All 200 sampled log lines are `Unauthorized`, so it is total, not intermittent.
The node's kube-proxy shows 11 restarts, the last 200 days ago.

This is almost certainly the same certificate-expiry class of fault documented in
ABUDHABI guide A.9: the RKE2 internal client certs (`system:kube-proxy`,
`system:rke2-controller`) were issued at cluster build time with 1-year validity
and expired. A.9 records that the same expiry already broke the control plane and
caused the lost globalnet route incident on 2026-10-05.

Consequence: **any newly exported service in Abu Dhabi is unreachable**, because
kube-proxy cannot program it. Existing services keep working because their chains
were programmed before the credentials expired.

## The OICM API version is NOT a blocker (corrected)

An earlier revision of this file claimed Abu Dhabi's older OICM serves a
different API surface than the controller is written against. That claim was
wrong. It came from probing the Abu Dhabi ClusterIP through the relay while the
network path was broken, so the probe returned connection failures (`000`) and a
stray `404` rather than real status codes, and the conclusion was drawn from
those.

Probed side by side with identical paths and no credentials:

| Path | Abu Dhabi (external, `oicm.ai.ecouncil.ae`) | Al Ain (internal ClusterIP) |
|---|---|---|
| `/api/v1/workspaces` | 404 | 404 |
| `/api/v1/workspaces/{ws}/deployment_summary` | 403 | 403 |
| `/api/v1/workspaces/bogus-uuid/deployment_summary` | 403 | 403 |

Both clusters answer identically on every path. `404` on the collection route
means that route does not exist in either version. `403` on `deployment_summary`
means it exists and needs auth, and it is returned for a bogus workspace id too,
so the auth check runs before any lookup. The body is
`{"error_code": 403, "message": "Missing Authorization token"}` on both.

Abu Dhabi's own OpenAPI document settles it. Fetched from
`https://oicm.ai.ecouncil.ae/api/openapi/openapi.json` (1,328,907 bytes, title
`MLOps Backend`), it declares `servers: [{url: "/api/"}]` and contains
`GET /v1/workspaces/{workspace_id}/deployment_summary` with the summary "Get
deployment summary for all active deployments in the workspace", tagged
`Model Deployment`, and params `workspace_id, order_by, order_direction, offset,
limit, search`. It does not contain `GET /v1/workspaces`, matching the `404` both
clusters give. The spec has 398 paths and the same four bearer JWT security
schemes the controller expects.

So there is nothing to adapt. The controller's path and payload contract work
against Abu Dhabi's `1.7.1` unchanged, and the version gap is not a blocker.

| Component | Abu Dhabi | Al Ain |
|---|---|---|
| `oip-mlops-be-main` (flask-be) | `1.7.1` | `1.15.19` |
| `oip-mlops-nginx-main` | `1.7.1` | `1.15.19` |
| `oip-mlops-consumer` | `1.7.1` | (newer) |

Keycloak is the same story. Abu Dhabi's realm `adeo` at
`https://auth.ai.ecouncil.ae/realms/adeo/protocol/openid-connect/token` exists,
and unknown credentials get `invalid_grant` rather than a routing error. The only
thing missing to make a real authenticated call is a service account that exists
in Abu Dhabi's realm. The Al Ain credentials do not, which is expected since the
two clusters have separate Keycloaks. Internally, Abu Dhabi's
`keycloak.keycloak.svc.cluster.local` serves realm `adeo` and returns `401`
without credentials, which is correct.

## Why the network path is the only remaining blocker

With the API question resolved, the kube-proxy `Unauthorized` failure above is
the sole thing standing between the exports and a working status sync. Once the
expired RKE2 agent certificates are renewed and the `KUBE-EXT-*` chains appear
for `242.0.0.251` and `242.0.0.252`, the exports are reachable. Pointing the
controller at Abu Dhabi then needs no code change, only the base URL, auth URL,
realm, workspace id, and a service account.

The certificate fault is now diagnosed and confirmed. The agent nodes'
`client-kube-proxy` and `client-rke2-controller` certificates expired on
2026-09-29 while the control plane's were renewed that same day, which is why
kube-proxy cannot authenticate. See `abudhabi-rke2-cert-renewal-runbook.md` for
the evidence and the fix.

## Service port note

The exported services carry different ports than the existing GLM export, which
matters when pointing a client at them:

| Export | Service port | targetPort |
|---|---|---|
| GLM `s-766b1720...` | 8080 | `http` |
| `s-mlops-nginx-be-app` | 80 | 8080 |
| `keycloak` | 80 | `http` |

So from Al Ain the endpoints would be
`http://s-mlops-nginx-be-app.mlops.svc.clusterset.local:80` and
`http://keycloak.keycloak.svc.clusterset.local:80`.

## What to do next

1. **Renew the expired RKE2 agent certificates.** This is the only blocker and it
   is fully diagnosed. See `abudhabi-rke2-cert-renewal-runbook.md`: restart
   `rke2-agent` on each agent node, one at a time, which reissues the expired
   leaf certificates. Then confirm the `KUBE-EXT-*` chains appear for
   `242.0.0.251` and `242.0.0.252`.
2. **Obtain a service account on Abu Dhabi's Keycloak.** The API contract needs no
   change, but the Al Ain credentials do not exist in Abu Dhabi's realm `adeo`.
   Either create an equivalent service account there or establish that a shared
   identity is intended, then set `OICM_USERNAME` and `OICM_PASSWORD` for that
   cluster.
3. **Re-run the reachability probe with a real token** once both of the above are
   done, and capture the `deployment_summary` response shape to confirm it matches
   what the controller parses.

## What was left behind

Nothing. The debug pod (`ad-debug`, later `adrelay`) and its ConfigMap
(`abudhabi-kubeconfig`, later `adkc`) in Al Ain, the Abu Dhabi probe pods
(`adprobe`, `adgw`, later `adprobe2`), and the staged kubeconfig on the Al Ain
gateway host were all removed. The two ServiceExports remain, which is the
intended end state, and they are harmless while unreachable.

## Reusable command reference

The relay procedure, abbreviated. Substitute the debug pod name and namespace.

```bash
# 1. ship the Abu Dhabi kubeconfig into a pod on the Al Ain gateway node
#    (extract the valid copy from the guide, see above)
kubectl -n oik8s-cilium-system create configmap abudhabi-kubeconfig \
  --from-file=abudhabi-kubeconfig-fixed=/tmp/abudhabi-kubeconfig.yaml

# 2. run the privileged relay pod on adeo-gpu-03 (see ABUDHABI guide A.8 for the
#    full manifest: hostNetwork, hostPID, privileged, /host mounted)

# 3. stage the kubeconfig where chroot can read it
kubectl -n oik8s-cilium-system exec ad-debug -- \
  sh -c 'cp /kubeconfig/abudhabi-kubeconfig-fixed /host/tmp/abudhabi-kubeconfig'

# 4. run kubectl against Abu Dhabi
kubectl -n oik8s-cilium-system exec ad-debug -- chroot /host sh -c \
  'KUBECONFIG=/tmp/abudhabi-kubeconfig kubectl get nodes'

# 5. create an export
kubectl -n oik8s-cilium-system exec ad-debug -- chroot /host sh -c \
  'KUBECONFIG=/tmp/abudhabi-kubeconfig kubectl apply -f - <<EOF
apiVersion: multicluster.x-k8s.io/v1alpha1
kind: ServiceExport
metadata:
  name: s-mlops-nginx-be-app
  namespace: mlops
EOF'
```

Useful diagnostics for this failure mode:

```bash
# is kube-proxy healthy on the exporting cluster's gateway node?
kubectl -n kube-system logs kube-proxy-<gateway-node> --tail=50 | grep -c Unauthorized

# does the datapath have a chain for the global IP? (on the gateway node)
nft list ruleset | grep -E 'KUBE-EXT-|242\.0\.0\.25'

# is globalnet allocating and creating the internal service?
kubectl -n submariner-operator logs -l app=submariner-globalnet --tail=30
```

## Telling a network fault from an API fault

Both failure modes look like "the endpoint does not work", and confusing them
cost an earlier revision of this file a wrong conclusion. A status code alone is
not enough: a broken network path yields `000` and a wrong path yields `404`, and
both get read as "missing".

The reliable discriminator is to probe the same path against a cluster known to
work and compare status codes with credentials omitted:

```bash
# against each cluster, with no Authorization header
for p in /api/v1/workspaces \
         /api/v1/workspaces/$WS/deployment_summary \
         /api/v1/workspaces/bogus-uuid/deployment_summary; do
  printf '%-58s -> %s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code}' -m 10 -k "$BASE$p")"
done
```

Identical status codes mean the API surface is the same and only the network
path or the credentials differ. Different codes on the same path mean a genuine
API-version gap. A `403` on the real path and on a bogus id alike means the route
exists and the auth check runs before any lookup, so the API is fine and the
problem is auth or network.

The cluster's own OpenAPI document is the tie-breaker when a route is in doubt.
It is served at `<base>/api/openapi/openapi.json` and its `servers[].url` gives
the prefix that the declared `paths` are relative to.
