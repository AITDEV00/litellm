# Abu Dhabi OICM REST API over Submariner: findings and blocker

Date: 2026-10-06
Purpose: export the Abu Dhabi OICM REST API and auth so the controller can be
pointed at the Abu Dhabi cluster the same way it is pointed at Al Ain.

Outcome: the exports are created and correct, but the services are **not
reachable**, and the cause is a pre-existing Abu Dhabi fault unrelated to this
work. Details below.

## What was done

Abu Dhabi was reached via the documented relay (ABUDHABI guide A.8), using a
privileged pod on the Al Ain gateway node `adeo-gpu-03` and the Abu Dhabi
kubeconfig shipped in via ConfigMap.

Note: the local `~/.kube/abudhabi-kubeconfig.yaml` is **expired** (Not After
2026-09-29). The copy embedded in `submariner-ABUDHABI-guide.md` is valid until
2027-09-29 and was used instead. Worth refreshing the local file from the guide
or re-exporting.

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

## Why this blocks the OICM work

Even with the exports correct, there is a second, independent problem: Abu Dhabi
runs a much older OICM than Al Ain.

| Component | Abu Dhabi | Al Ain |
|---|---|---|
| `oip-mlops-be-main` (flask-be) | `1.7.1` | `1.15.19` |
| `oip-mlops-nginx-main` | `1.7.1` | `1.15.19` |
| `oip-mlops-consumer` | `1.7.1` | (newer) |

Abu Dhabi's API does not serve the endpoint the controller uses. Probed from a pod
inside Abu Dhabi against `s-mlops-nginx-be-app.mlops.svc.cluster.local`:

```
/api/v1/workspaces                     404
/api/v1/workspaces/x/deployment_summary 404
/api/v1/version                        404
/api/workspace                         405   <- exists, wrong method
/api/workspaces                        404
/api/model(s) /api/deployment(s)       404
```

So Abu Dhabi exposes a different API generation under `/api/workspace`, not the
`/api/v1/workspaces/...` surface the controller is written against. Replicating
the Al Ain behavior on Abu Dhabi needs either a controller API-version adapter or
an Abu Dhabi OICM upgrade.

Keycloak is the good news: `keycloak.keycloak.svc.cluster.local` in Abu Dhabi
serves realm `adeo` and the token endpoint responds (`401` without credentials,
which is correct). So auth would work once the network path exists.

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
   `242.0.0.251` and `242.0.0.252`. Only then are the exports reachable.
2. **Decide on the OICM version gap.** Either ask the platform team to upgrade Abu
   Dhabi OICM, or add a version adapter to the controller. Do not assume the Al
   Ain status code works against `1.7.1`.
3. **Refresh the local Abu Dhabi kubeconfig** from the guide's copy (valid to
   2027-09-29) and note the expiry in the guide so it is not rediscovered.

## What was left behind

Nothing. The debug pod (`ad-debug`) and its ConfigMap in Al Ain, the Abu Dhabi
probe pods (`adprobe`, `adgw`), and the staged kubeconfig on the Al Ain gateway
host were all removed. The two ServiceExports remain, which is the intended
end state, and they are harmless while unreachable.

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
