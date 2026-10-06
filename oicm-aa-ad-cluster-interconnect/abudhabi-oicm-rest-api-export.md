# Abu Dhabi OICM REST API over Submariner: findings and blocker

Date: 2026-10-06
Purpose: export the Abu Dhabi OICM REST API and auth so the controller can be
pointed at the Abu Dhabi cluster the same way it is pointed at Al Ain.

Outcome: the exports are created and correct, but the services are **not
reachable**, and the cause is a pre-existing Abu Dhabi fault unrelated to this
work. The OICM API version is **not** a second blocker, contrary to an earlier
revision of this file. Details below.

## What was done

Abu Dhabi was reached via the documented relay (ABUDHABI guide A.8), using a
privileged pod on the Al Ain gateway node `adeo-gpu-03` and the Abu Dhabi
kubeconfig shipped in via ConfigMap.

Note: the local `~/.kube/abudhabi-kubeconfig.yaml` was **expired** (Not After
2026-09-29). It has since been refreshed from the copy embedded in
`submariner-ABUDHABI-guide.md`, which is valid until 2027-09-29, and the local
file now carries that copy.

Two ServiceExports were created on Abu Dhabi:

| Service | Namespace | Global IP allocated |
|---|---|---|
| `s-mlops-nginx-be-app` | `mlops` | `242.0.0.252` |
| `keycloak` | `keycloak` | `242.0.0.251` |

Globalnet processed both correctly:

```
Processing ServiceExport "mlops/s-mlops-nginx-be-app"
Allocated global IP ["242.0.0.252"] for "mlops/s-mlops-nginx-be-app"
Created internal service "mlops/submariner-oelikqosgmo2y7cyqatfljm67mrgsqi7"
Processing ServiceExport "keycloak/keycloak"
Allocated global IP ["242.0.0.251"] for "keycloak/keycloak"
Created internal service "keycloak/submariner-2y7uudg2vnjhe3i45ck6jlemgudqjhtb"
```

The ServiceImport for `mlops/s-mlops-nginx-be-app` is visible from Al Ain, so
Lighthouse is publishing it.

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
RKE2 certs are rotated and the `KUBE-EXT-*` chains appear for `242.0.0.251` and
`242.0.0.252`, the exports are reachable. Pointing the controller at Abu Dhabi
then needs no code change, only the base URL, auth URL, realm, workspace id, and
a service account.

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

1. **Fix the Abu Dhabi cert expiry first.** Rotate the RKE2 node certs (rolling
   restart of `rke2-agent` on workers and `rke2-server` on masters) and restart
   kube-proxy, then confirm `kubectl -n kube-system logs kube-proxy-prd-oi-k8worker01`
   stops printing `Unauthorized` and that the `KUBE-EXT-*` chains appear for
   `242.0.0.251` and `242.0.0.252`. Only then are the exports reachable. This is
   now the only blocker.
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
