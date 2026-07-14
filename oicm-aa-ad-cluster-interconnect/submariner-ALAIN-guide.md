# Submariner Deployment — AL AIN Cluster (Joins the Abu Dhabi Broker)

Complete, self-contained runbook for joining the air-gapped Al Ain OICM cluster to the
existing Abu Dhabi Submariner broker (0.24.0). Al Ain runs the **operator chart only** —
the broker already exists in Abu Dhabi. End goal: Al Ain's LiteLLM gateway reaches the
GLM 5.2 model in Abu Dhabi over an encrypted tunnel.

> Al Ain is the harder side. It hit **six** distinct problems the first time. Every one is
> pre-empted below. Do the steps in order — especially the PSA label (§4) BEFORE install.

---

## 0. Environment

| Item | Value |
|---|---|
| Role | Member (joins Abu Dhabi broker) |
| K8s | v1.34.1 |
| CNI | **Cilium** — Submariner can't detect it, falls back to `generic` (cosmetic; tunnel works anyway) |
| Pod / Service CIDR | `10.42.0.0/16` / `10.43.0.0/16` (identical to Abu Dhabi → Globalnet) |
| Global CIDR (this cluster) | `242.0.1.0/24` (must NOT overlap Abu Dhabi's `242.0.0.0/24`) |
| Gateway node (chosen) | `adeo-gpu-03` — `10.34.104.19` (GPU node; taints tolerated by DaemonSets) |
| Other nodes | masters `.11 .12 .13`; storage `.14 .15 .16`; gpu `.17 .18 .19` (all `10.34.104.0/24`) |
| Harbor | `registry.adeoaiengine.ecouncil.ae` |
| Bastion | `aitdev00`, context `kubernetes-admin@adeoaiengine` |
| Privileged namespace (for debug pods) | `oik8s-cilium-system` |
| Cable driver | WireGuard — tunnel ports **UDP 4500 + 4490**; intra-cluster VXLAN **UDP 4800** |

---

## 1. Prerequisites

1. **Abu Dhabi broker already deployed** and healthy (see the Abu Dhabi guide).
2. **Firewall** — bidirectional UDP 4500 + 4490 between `10.34.104.19` ↔ `10.10.128.72`, and
   Al Ain → `10.10.128.71:6443` TCP. See §10. The tunnel will sit `connecting`/`error` until this is open.
3. **The SAME PSK** used in Abu Dhabi (§4 of the Abu Dhabi guide). You need that exact string here.
4. **Operator source** for the globalnet RBAC (§7): `submariner-operator/config/rbac/submariner-globalnet/`.
5. Tooling on `aitdev00`: `helm`, `subctl` v0.24.0, and `submariner-operator-0.24.0.tgz`
   (broker chart NOT needed). `wireguard-tools` (`apt-get install -y wireguard-tools`) is useful
   for `wg show` debugging via the technique in §8.

---

## 2. Mirror images to Al Ain Harbor

Same 8 images as Abu Dhabi, into `registry.adeoaiengine.ecouncil.ae/submariner/`:
```
submariner-operator, submariner-gateway, submariner-route-agent, submariner-globalnet,
lighthouse-agent, lighthouse-coredns, nettest, subctl   (all :0.24.0)
```
Pull/tag/push exactly as in the Abu Dhabi guide §2, substituting the Al Ain Harbor host.

Verify the cluster can pull:
```bash
kubectl run pulltest --image=registry.adeoaiengine.ecouncil.ae/submariner/nettest:0.24.0 \
  --restart=Never --command -- sleep 15
kubectl describe pod pulltest | grep -A5 -i events    # want: Successfully pulled image
kubectl delete pod pulltest --ignore-not-found
```

---

## 3. Confirm WireGuard on the gateway node (no SSH needed)

`modprobe wireguard` may say "module not found" — that's fine on kernel 6.8 (it's built-in). The
authoritative test is `ip link add type wireguard`:
```bash
kubectl run wgcheck -n oik8s-cilium-system --restart=Never -i --rm \
  --image=registry.adeoaiengine.ecouncil.ae/submariner/submariner-gateway:0.24.0 \
  --overrides='{"spec":{"hostNetwork":true,"nodeName":"adeo-gpu-03","tolerations":[{"operator":"Exists"}],"containers":[{"name":"wg","image":"registry.adeoaiengine.ecouncil.ae/submariner/submariner-gateway:0.24.0","command":["/bin/sh","-c","ip link add wgtest type wireguard 2>&1 && ip link del wgtest && echo WIREGUARD_OK || echo WIREGUARD_FAIL"],"securityContext":{"privileged":true}}]}}' 2>&1
# want: WIREGUARD_OK
```

---

## 4. Pre-create the namespace as PRIVILEGED (Al Ain enforces Pod Security Admission)

Al Ain enforces PSA `baseline`. Submariner's gateway/routeagent/globalnet are privileged
host-network pods and are **rejected** unless the namespace is `privileged`. Do this **before**
install, or the daemonsets create 0 pods and you get a half-reconciled mess:
```bash
kubectl create namespace submariner-operator
kubectl label --overwrite namespace submariner-operator \
  pod-security.kubernetes.io/enforce=privileged \
  pod-security.kubernetes.io/warn=privileged \
  pod-security.kubernetes.io/audit=privileged
```

---

## 5. Transfer broker credentials CLEANLY (terminal paste corrupts them)

Pasting the CA/token through a terminal injects whitespace/line-wraps → base64 CA fails to decode
(`illegal base64 data`), and the token becomes stale/invalid (`Unauthorized`). Both cost real time.
Use the double-base64 paste-safe method with sanity gates.

```bash
# ON ABU DHABI — print a paste-safe block (double-wrapped):
BROKER_NS=submariner-k8s-broker
URL=$(kubectl -n default get endpoints kubernetes -o jsonpath="{.subsets[0].addresses[0].ip}:{.subsets[0].ports[?(@.name=='https')].port}")
CAW=$(kubectl -n "$BROKER_NS" get secret submariner-k8s-broker-client-token -o jsonpath='{.data.ca\.crt}' | tr -d '[:space:]' | base64 -w0)
TOKW=$(kubectl -n "$BROKER_NS" get secret submariner-k8s-broker-client-token -o jsonpath='{.data.token}' | base64 -d | tr -d '[:space:]' | base64 -w0)
printf '===BLOB-START===\nURL=%s\nCAW=%s\nTOKW=%s\n===BLOB-END===\n' "$URL" "$CAW" "$TOKW"
```

```bash
# ON AL AIN — paste the block into /tmp/broker-blob.txt (Ctrl-D), then reconstruct + GATE:
URL=$(grep -a '^URL='  /tmp/broker-blob.txt | head -1 | cut -d= -f2- | tr -d '[:space:]')
CAW=$(awk '/^CAW=/{f=1} /^TOKW=/{f=0} f' /tmp/broker-blob.txt | sed 's/^CAW=//' | tr -d '[:space:]')
TOKW=$(awk '/^TOKW=/{f=1} /BLOB-END/{f=0} f' /tmp/broker-blob.txt | sed 's/^TOKW=//' | tr -d '[:space:]')
CA=$(echo "$CAW" | base64 -d | tr -cd '[:print:]')
TOKEN=$(echo "$TOKW" | base64 -d | tr -cd '[:print:]')
echo "TOKEN len=${#TOKEN}"                        # MUST equal the broker's token length (e.g. 1013)
echo "$CA" | base64 -d | head -1                  # MUST print: -----BEGIN CERTIFICATE-----
echo "$TOKEN" | awk -F. '{print "segments="NF}'   # MUST print: segments=3
echo "$TOKEN" | tr -d '[:print:]' | wc -c         # MUST print: 0  (strip any stray byte)
```

Write the passing values into `broker-creds.yaml` (quoted strings — NEVER pass CA/token via `--set`,
Helm parses `/ . =` and breaks them → `YAML parse error line 11`):
```yaml
broker:
  namespace: submariner-k8s-broker
  server: "PASTE_URL"
  token:  "PASTE_TOKEN"
  ca:     "PASTE_CA"
```

---

## 6. Install the operator

```bash
helm install submariner-operator ./submariner-operator-0.24.0.tgz \
  --namespace submariner-operator \
  -f broker-creds.yaml \
  --set operator.image.repository=registry.adeoaiengine.ecouncil.ae/submariner/submariner-operator \
  --set operator.image.tag=0.24.0 \
  --set submariner.images.repository=registry.adeoaiengine.ecouncil.ae/submariner \
  --set submariner.images.tag=0.24.0 \
  --set submariner.clusterId=alain \
  --set submariner.clusterCidr=10.42.0.0/16 \
  --set submariner.serviceCidr=10.43.0.0/16 \
  --set submariner.globalnet=true \
  --set submariner.globalCidr=242.0.1.0/24 \
  --set submariner.serviceDiscovery=true \
  --set submariner.natEnabled=false \
  --set submariner.cableDriver=wireguard \
  --set submariner.airGappedDeployment=true \
  --set submariner.ceIPSecPSK="<SAME-PSK-AS-ABU-DHABI>"

kubectl label node adeo-gpu-03 submariner.io/gateway=true
```

> **Differences from Abu Dhabi:** `clusterId=alain`, `globalCidr=242.0.1.0/24` (must not overlap),
> Al Ain Harbor, and NO broker chart (joins the existing one).
> `ceIPSecPSK` MUST equal Abu Dhabi's exact string.
> If `ipsec.psk`/`ceIPSecPSK` ends up null the CR is rejected: `spec.ceIPSecPSK ... must be of type string: "null"`.

---

## 7. Post-install fixes — Al Ain needs several. Apply in this order.

### 7a. Gateway CrashLoop: `could not determine public IPv4` (air-gapped flag)
```bash
kubectl -n submariner-operator get submariner -o jsonpath='{.items[0].spec.airGappedDeployment}{"\n"}'
kubectl -n submariner-operator patch submariner submariner --type=merge -p '{"spec":{"airGappedDeployment":true}}'
kubectl -n submariner-operator rollout restart daemonset/submariner-gateway
```

### 7b. Broker auth errors — `illegal base64 data` (CA) or `Unauthorized` (token)
Caused by paste-corrupted creds (§5). If it slipped through, strip and re-patch the CR:
```bash
CA=$(kubectl -n submariner-operator get submariner -o jsonpath='{.items[0].spec.brokerK8sCA}' | tr -cd '[:print:]')
echo "$CA" | base64 -d | head -1    # must be BEGIN CERTIFICATE
# if the TOKEN is stale/wrong length, re-fetch from Abu Dhabi's broker (must match its token length exactly)
kubectl -n submariner-operator patch submariner submariner --type=merge \
  -p "{\"spec\":{\"brokerK8sCA\":\"${CA}\",\"brokerK8sApiServerToken\":\"${TOKEN}\"}}"
kubectl -n submariner-operator rollout restart daemonset/submariner-gateway
```

### 7c. **Missing globalnet RBAC** — `serviceaccount "submariner-globalnet" not found`
**The Helm chart never creates the globalnet RBAC.** The globalnet daemonset shows `DESIRED=1
CURRENT=0` and can't schedule → the `submariner` interface never gets its global IP → the
health-check ping (`242.0.1.254` / remote `242.0.0.254`) fails and the connection stays `error`
even after the tunnel handshake works. **Do NOT wait for self-heal — create the 5 objects from source:**
```bash
kubectl apply -f submariner-operator/config/rbac/submariner-globalnet/
#   ServiceAccount, Role, RoleBinding, ClusterRole, ClusterRoleBinding (all named submariner-globalnet)
kubectl -n submariner-operator get sa submariner-globalnet          # confirm exists
kubectl -n submariner-operator rollout restart ds submariner-globalnet
kubectl -n submariner-operator get pods -l app=submariner-globalnet -o wide   # should be Running
```
> If you don't have the source dir, the 5 objects are: SA `submariner-globalnet`; a Role (configmaps
> get/list/watch, gateways get/list/watch/update, leases CRUD); matching RoleBinding; a ClusterRole
> (pods get/list/watch; services+endpoints CRUD; submariner.io clusters/endpoints get/list/watch;
> the globalegressips/globalingressips/clusterglobalegressips CRDs CRUD; serviceexports get/list/watch);
> matching ClusterRoleBinding. **Persist them — a future `helm upgrade` won't recreate them.**

### 7d. **UFW blocking intra-cluster VXLAN (UDP 4800)** — the tunnel connects but pinger fails
Route-agent's VXLAN overlay (`vx-submariner`) uses **UDP 4800** (not 8472). If the gateway node
`adeo-gpu-03` runs UFW with `policy DROP`, VXLAN packets from other Al Ain nodes are dropped before
reaching the gateway → `vx-submariner` RX=0 → pinger fails "more than 5 packets lost". No SSH? Use a
privileged hostPath pod + chroot to run the host's own `ufw` (writes PERSISTENT host rules):

```bash
# 1) create a privileged debug pod that mounts the host root at /host, pinned to the gateway node:
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata: { name: host-debug, namespace: oik8s-cilium-system }
spec:
  hostNetwork: true
  hostPID: true
  nodeSelector: { kubernetes.io/hostname: adeo-gpu-03 }
  tolerations: [{ operator: Exists }]
  containers:
  - name: debug
    image: registry.adeoaiengine.ecouncil.ae/submariner/nettest:0.24.0
    command: ["sleep","600"]
    securityContext: { privileged: true }
    volumeMounts: [{ name: host, mountPath: /host }]
  volumes: [{ name: host, hostPath: { path: /, type: Directory } }]
  restartPolicy: Never
EOF
kubectl -n oik8s-cilium-system wait --for=condition=Ready pod/host-debug --timeout=30s

# 2) check + fix UFW on the HOST (chroot runs the host's ufw, editing /etc/ufw/user.rules → persists across reboot):
kubectl -n oik8s-cilium-system exec host-debug -- sh -c 'chroot /host /usr/sbin/ufw status numbered'
kubectl -n oik8s-cilium-system exec host-debug -- sh -c 'chroot /host /usr/sbin/ufw allow from 10.34.104.0/24 to any port 4800 proto udp'
kubectl -n oik8s-cilium-system exec host-debug -- sh -c 'chroot /host /usr/sbin/ufw allow to any port 4800 proto udp'
kubectl -n oik8s-cilium-system exec host-debug -- sh -c 'chroot /host /usr/sbin/ufw status numbered | grep 4800'
```
> Only the **gateway node** needs 4800 inbound for basic tunnel health (non-gateway nodes only send
> *to* it). If you add a second gateway for HA later, that node needs the same rule. Note that even
> with 4800 open on all nodes, non-gateway pods still cannot reach globalnet IPs due to the Cilium
> BPF limitation documented in §12.1; services that need cross-cluster connectivity must run on the
> gateway node itself.

### 7e. **Pin lighthouse pods to the gateway node** — clean broker access without NAT

The broker API server (`10.10.128.71:6443`) is only reachable from `adeo-gpu-03` (`10.34.104.19`)
through the firewall. The lighthouse-agent and lighthouse-coredns pods need broker access to sync
ServiceImports. By default they land on master nodes that can't reach the broker.

The clean fix: pin them to `adeo-gpu-03` using the Submariner CR's built-in `nodeSelector` and
`tolerations` fields. The operator propagates these to the ServiceDiscovery CR, which propagates
them to the lighthouse deployments. Pod traffic to the broker is SNAT'd by Cilium's default
MASQUERADE rule to the node IP `10.34.104.19`, which the firewall allows.

```bash
# 1) verify the node has the hostname label and gateway label:
kubectl get node adeo-gpu-03 -o jsonpath='{.metadata.labels.kubernetes\.io/hostname}{"\n"}'   # adeo-gpu-03
kubectl get node adeo-gpu-03 -o jsonpath='{.metadata.labels.submariner\.io/gateway}{"\n"}'    # true

# 2) verify a pod on adeo-gpu-03 can reach the broker (Cilium SNAT -> node IP -> firewall):
kubectl -n oik8s-cilium-system exec <debug-pod> -- timeout 5 bash -c \
  'echo | nc -w 3 10.10.128.71 6443 && echo REACHABLE || echo UNREACHABLE'

# 3) patch the Submariner CR (NOT the ServiceDiscovery CR; the Submariner controller overwrites SD spec):
kubectl -n submariner-operator patch submariner submariner --type=merge -p '{
  "spec": {
    "nodeSelector": { "kubernetes.io/hostname": "adeo-gpu-03" },
    "tolerations": [ { "operator": "Exists" } ]
  }
}'

# 4) wait for the operator to reconcile (it propagates Submariner.spec.nodeSelector -> ServiceDiscovery.spec -> deployments):
sleep 15
kubectl -n submariner-operator get deploy submariner-lighthouse-agent -o jsonpath='{.spec.template.spec.nodeSelector}{"\n"}'
#   should show: {"kubernetes.io/hostname":"adeo-gpu-03"}

# 5) if the pods don't reschedule within 30s, force a rollout:
kubectl -n submariner-operator rollout restart deploy/submariner-lighthouse-agent deploy/submariner-lighthouse-coredns
sleep 30
kubectl -n submariner-operator get pods -l 'app in (submariner-lighthouse-agent,submariner-lighthouse-coredns)' -o wide
#   all pods should show NODE=adeo-gpu-03
```

> **Why not the ServiceDiscovery CR?** The Submariner controller's `serviceDiscoveryReconciler`
> rebuilds the ServiceDiscovery spec from `submariner.spec` on every reconcile, so any fields you set
> directly on the ServiceDiscovery CR get overwritten. Set them on the Submariner CR and they
> propagate downward automatically.
>
> **Why not a NAT gateway?** A manual iptables MASQUERADE + static routes on every node is fragile
> (not persistent across reboots, not managed by Kubernetes, breaks on node replacement). Pinning the
> pods to the gateway node is a one-line CR patch that survives operator restarts and Helm upgrades.

### 7f. **TLS certificate mismatch** — `brokerK8sInsecure=true`

If the broker API server's TLS certificate doesn't match the IP the lighthouse agent connects to
(common when using an IP instead of a hostname), the agent gets `x509: certificate signed by
unknown authority` or `x509: hostname mismatch` errors. The fix is to set `brokerK8sInsecure` on
the Submariner CR:

```bash
kubectl -n submariner-operator patch submariner submariner --type=merge \
  -p '{"spec":{"brokerK8sInsecure":true}}'
# verify it propagated to the ServiceDiscovery CR:
kubectl -n submariner-operator get servicediscovery service-discovery \
  -o jsonpath='{.spec.brokerK8sInsecure}{"\n"}'   # true
# restart lighthouse agent to pick up the change:
kubectl -n submariner-operator rollout restart deploy/submariner-lighthouse-agent
```

### 7g. **ServiceExport lost** — re-create it if the globalnet IP disappears

If the ServiceExport is deleted (or never created), the globalnet daemon won't allocate a global
ingress IP, and cross-cluster DNS resolution fails. On the **Abu Dhabi** cluster (where the service
lives):

```bash
# check if the ServiceExport exists:
kubectl get serviceexport -A | grep <service-name>
# if missing, create it:
kubectl apply -f - <<EOF
apiVersion: multicluster.x-k8s.io/v1alpha1
kind: ServiceExport
metadata:
  name: <service-name>
  namespace: <service-namespace>
EOF
# verify the globalnet IP was allocated (on Abu Dhabi):
kubectl get globalingressip -A | grep <service-name>
# verify from Al Ain:
kubectl get serviceimport -A | grep <service-name>
kubectl -n oik8s-cilium-system exec <debug-pod> -- nslookup <service-name>.<namespace>.svc.clusterset.local
```

---

## 8. Debugging with `wg show` (no SSH, no wg in the image)

The gateway image lacks `wg`. Get the binary onto the node via a ConfigMap + chroot:
```bash
# on aitdev00 (has wireguard-tools):
base64 /usr/bin/wg > /tmp/wg.b64
kubectl -n oik8s-cilium-system create configmap wg-binary --from-file=wg.b64=/tmp/wg.b64
# add a wg-cm configMap volume (mount /wg-cm) to the host-debug pod from §7d, then:
kubectl -n oik8s-cilium-system exec host-debug -- sh -c \
  'base64 -d /wg-cm/wg.b64 > /tmp/wg && chmod +x /tmp/wg && cp /tmp/wg /host/tmp/wg'
kubectl -n oik8s-cilium-system exec host-debug -- sh -c 'chroot /host /tmp/wg show submariner'
```
Read the peer block: **`latest handshake:` recent + RX bytes climbing = tunnel up.** No handshake +
RX ~0 = handshake failing (PSK mismatch, or return path blocked). `preshared key: (hidden)` on one
side only is a PSK hint.

Interface counters without wg (always works):
```bash
RA=$(kubectl -n submariner-operator get pod -l app=submariner-routeagent --field-selector spec.nodeName=adeo-gpu-03 -o name | head -1)
kubectl -n submariner-operator exec "$RA" -c submariner-routeagent -- ip -s link show submariner | grep -A2 'RX\|TX'
```

---

## 9. Bring up + verify the tunnel

With firewall open, PSK matched, RBAC applied, and UFW 4800 allowed, force a fresh handshake on
BOTH gateways:
```bash
kubectl -n submariner-operator delete pod -l app=submariner-gateway                    # Al Ain
kubectl --context default -n submariner-operator delete pod -l app=submariner-gateway  # Abu Dhabi
sleep 40
subctl show connections     # STATUS: connected, RTT ~4-5ms
subctl show all
```
Fully working end-state:
- `subctl show connections` → `connected`, RTT ~4.5ms.
- `subctl show all` gateway summary → "All connections (1) are established".
- ping the remote health-check IP `242.0.0.254` → 0% loss (via the host-debug pod).
- (`Network plugin: generic` still shows — cosmetic; the tunnel works regardless.)

> **Verify from the gateway node only.** Cross-cluster connectivity from non-gateway pods does not
> work due to the Cilium BPF limitation in §12.1. All verification commands above use
> `nodeSelector: adeo-gpu-03` for this reason. If you test from any other node the connection will
> time out even when the tunnel is perfectly healthy.

---

## 10. Firewall summary

| Source | Destination | Port | Proto | Purpose |
|---|---|---|---|---|
| `10.34.104.19` ↔ `10.10.128.72` (both ways) | | **4500** | UDP | WireGuard tunnel data |
| `10.34.104.19` ↔ `10.10.128.72` (both ways) | | **4490** | UDP | NAT-traversal discovery |
| `10.34.104.19` → `10.10.128.71` | | **6443** | TCP | Broker API (lighthouse agent, pinned to `adeo-gpu-03` via §7e) |
| Al Ain nodes `10.34.104.0/24` → `adeo-gpu-03` | | **4800** | UDP | **Intra-cluster VXLAN (UFW, §7d)** |

Bidirectional is required for 4490/4500 (WireGuard reply must return; stateful rules can expire in
the handshake gaps). **51820 is NOT used.** Only `adeo-gpu-03` needs TCP 6443 to the broker because
the lighthouse pods are pinned there (§7e); other nodes don't need broker access. The same rule
also lets you run `kubectl` against the Abu Dhabi cluster from `adeo-gpu-03` for ops/debugging
(see Appendix A.11). Even with 4800 open on all nodes, non-gateway pods cannot reach globalnet IPs
due to the Cilium BPF limitation (§12.1); cross-cluster services must run on `adeo-gpu-03`.

---

## 11. Root causes of the outage (reference)

Six independent bugs, stacked — each hid the next, so fixing one alone still showed `error`:

1. **PSK mismatch** — Abu Dhabi had no PSK, Al Ain did → WireGuard silently dropped handshake responses (MAC verify fails). Fix: identical `ceIPSecPSK` both clusters.
2. **UFW blocking UDP 4800** — intra-cluster VXLAN dropped on the gateway node → pinger failed. Fix: `ufw allow ... 4800/udp` on `adeo-gpu-03` (via chroot).
3. **Missing globalnet RBAC** — chart never created it → globalnet pod never scheduled → `submariner` interface had no global IP → health check failed. Fix: apply the 5 RBAC objects from source.
4. **TLS certificate mismatch** — broker API cert didn't match the IP → lighthouse agent got x509 errors. Fix: `brokerK8sInsecure=true` on the Submariner CR (§7f).
5. **ServiceExport lost** — no globalnet IP allocated, cross-cluster DNS failed. Fix: re-create the ServiceExport on Abu Dhabi (§7g).
6. **Lighthouse pods on wrong node** — broker API (10.10.128.71:6443) only reachable from `adeo-gpu-03` through the firewall, but lighthouse pods landed on master nodes. Fix: pin lighthouse pods to `adeo-gpu-03` via Submariner CR `nodeSelector`/`tolerations` (§7e).

Empirical lessons that cracked it: tcpdump proved packets *were* arriving (ruled out "firewall
return-path"); `wg show` (via chroot) proved the handshake state; reading the operator RBAC from
source revealed the chart gap; checking UFW on the actual host (not the pod) found the 4800 block.
`subctl diagnose all` independently flagged the VXLAN(4800) and CNI issues. Ground-truth inspection
beat reasoning-from-symptoms every time. For the broker access problem, reading the Submariner
operator source code revealed that `submariner.spec.nodeSelector` propagates to the ServiceDiscovery
CR and then to the lighthouse deployments — a clean, Kubernetes-native fix that replaces the
fragile manual NAT gateway (iptables MASQUERADE + static routes on every node).

---

## 12. Final step — export GLM 5.2 and wire LiteLLM

The tunnel gives connectivity; the model service must be **exported** so Lighthouse publishes it.
On the **Abu Dhabi** cluster:
```bash
kubectl get svc -A | grep -iE 'glm|inference|model'     # find the GLM service + namespace
kubectl apply -f - <<EOF
apiVersion: multicluster.x-k8s.io/v1alpha1
kind: ServiceExport
metadata:
  name: <glm-service-name>
  namespace: <glm-namespace>
EOF
```
From Al Ain it resolves as `<glm-service>.<glm-namespace>.svc.clusterset.local` (Globalnet returns a
`242.x` global ingress IP behind that name). Point Al Ain LiteLLM's GLM 5.2 upstream at
`http://<glm-service>.<glm-namespace>.svc.clusterset.local:<port>/v1`.

Verify from an Al Ain pod:
```bash
kubectl get serviceimports -A | grep -i <glm-service-name>
kubectl -n oik8s-cilium-system exec host-debug -- \
  nslookup <glm-service>.<glm-namespace>.svc.clusterset.local
```

---

## 12.1. Cilium BPF limitation: non-gateway pods cannot reach globalnet IPs

Submariner's cross-cluster traffic works perfectly from the **gateway node** (`adeo-gpu-03`). Pods
running on any other Al Ain node, however, cannot reach Abu Dhabi globalnet IPs (`242.0.0.0/24`).
This is a fundamental interaction between Submariner's Globalnet datapath and Cilium's BPF
kube-proxy replacement, not a configuration error.

### Root cause

The outbound and return paths use different datapaths, and Cilium's BPF drops the return packet:

1. **Outbound** (pod on non-gateway node → Abu Dhabi globalnet IP): Cilium BPF routes the pod's
   packet through `cilium_host`, Submariner's nftables SNATs the source to a global IP, and the
   packet exits via `vx-submariner` to the gateway node, then via the WireGuard tunnel to Abu Dhabi.
   This works.

2. **Return** (Abu Dhabi → non-gateway pod): The return packet arrives at the gateway node via the
   WireGuard tunnel with `src=242.0.0.x dst=10.42.x.x` (the pod's real cluster IP). Submariner's
   nftables forwards it onto `vx-submariner` toward the destination node. The kernel FORWARD chain
   accepts it. But when it arrives at the destination node's `cilium_host` interface, Cilium's
   `cil_to_host` BPF program (tcx/ingress on `cilium_host`) treats it as an **unsolicited
   world-to-pod packet** because the source IP (`242.0.0.x`) maps to Cilium identity
   `reserved:world` and there is no matching conntrack entry on the receiving node's BPF CT. The
   packet is silently dropped.

### Evidence

Traced with `cilium monitor`, `cilium bpf ct list`, `tcpdump` on `vx-submariner`, and nftables
counters across multiple nodes:

- On the gateway (`adeo-gpu-03`): return traffic arrives via the `submariner` WireGuard interface,
  is forwarded onto `vx-submariner` (TX counter climbs), and `SUBMARINER-FORWARD` nftables counter
  climbs. A policy route fix (`ip route ... table 151` + `ip rule ... priority 90`) was needed to
  route return traffic onto `vx-submariner` instead of `cilium_host`.
- On the receiving node (`adeo-gpu-01`): `vx-submariner` RX counter climbs (packet arrives), kernel
  FORWARD chain accepts it (`CILIUM_FORWARD: any->cilium_host`), but Cilium BPF CT shows the
  outbound SYN entry with `Packets=0 RxFlagsSeen=0x00` (no return ever processed). Cilium monitor
  shows only outbound traffic, no return.
- A nftables SNAT probe (rewrite return source from `242.0.0.x` to the receiving node's
  `cilium_host` IP) was installed to test whether Cilium would accept the packet if it appeared to
  come from a known local identity. The probe counter stayed at 0; the rule never matched because
  `10.42.x.x` is local to the node, so the packet goes to INPUT, not FORWARD/postrouting.

### Workaround

**Run any service that needs cross-cluster connectivity on the gateway node (`adeo-gpu-03`).** This
is the only reliable solution with the current Submariner 0.24 + Cilium 1.18 (kube-proxy-replacement
+ tunnel mode) stack. Pin the Deployment with `nodeSelector` and `tolerations`:

```yaml
spec:
  template:
    spec:
      nodeSelector:
        kubernetes.io/hostname: adeo-gpu-03
      tolerations:
        - key: tenant
          value: adeo
          effect: NoSchedule
        - key: tenant
          value: adeo
          effect: NoExecute
        - key: target
          value: k8s
          effect: NoSchedule
        - key: target
          value: k8s
          effect: NoExecute
        - key: nvidia.com/gpu
          effect: NoSchedule
```

For multi-replica services (e.g. LiteLLM proxy with 2 replicas), use `topologySpreadConstraints`
with `whenUnsatisfiable: ScheduleAnyway` so both replicas land on gpu-03 but spread across
availability zones if the node has multiple. Since there is only one gateway node, both replicas
will co-locate on gpu-03.

### What does NOT work

- Policy route fix on the gateway (table 151 + ip rule priority 90): routes return traffic onto
  `vx-submariner` correctly, but Cilium BPF on the receiving node still drops it.
- `rp_filter` adjustments (loose mode on `vx-submariner` and `submariner` interfaces): does not
  affect the drop because it happens in BPF, not the kernel.
- SNAT of return traffic to the receiving node's `cilium_host` IP: the packet never reaches
  postrouting because the destination is local.
- Cilium NetworkPolicies: none are present; the drop is in BPF conntrack, not policy enforcement.

---

## 13. Cleanup + hardening
- Delete debug artefacts: `kubectl -n oik8s-cilium-system delete pod host-debug wgcheck --ignore-not-found; kubectl -n oik8s-cilium-system delete configmap wg-binary --ignore-not-found`.
- `rm -f /tmp/broker-blob.txt broker-creds.yaml /tmp/wg.b64` (hold the token/PSK).
- **Persist out-of-Helm fixes** so `helm upgrade --reuse-values` doesn't revert them: PSK + air-gapped in values; re-apply globalnet RBAC after any upgrade; UFW 4800 rule is host-side (already persistent). The `nodeSelector`/`tolerations` on the Submariner CR (§7e) and `brokerK8sInsecure` (§7f) are in the CR spec, so they survive operator restarts but NOT `helm upgrade` (Helm re-applies the Submariner CR from values). Add these to your Helm values file:
  ```yaml
  submariner:
    spec:
      brokerK8sInsecure: true
      nodeSelector:
        kubernetes.io/hostname: adeo-gpu-03
      tolerations:
      - operator: Exists
  ```
- **Remove the old NAT gateway** if upgrading from the manual approach: delete the iptables MASQUERADE + FORWARD rules on `adeo-gpu-03`, and delete the static route `10.10.128.0/24 via 10.34.104.19` on all other nodes (use the routeagent pods which have hostNetwork access). See Appendix A.9.
- Rotate the broker token (exposed during debugging).

---

# APPENDIX A — Full working command reference (nothing truncated)

Every command that was actually run to diagnose and fix the three-layer outage, in phase order.
These are the complete, verbatim commands — use them as copy-paste reference. Substitute pod names
where noted (they're dynamic).

## A.1 Initial connectivity testing (does Al Ain reach Abu Dhabi at all?)

Context + Submariner resources:
```bash
kubectl config current-context && echo "---NODES---" && kubectl get nodes -o wide
echo "=== GATEWAYS ==="  && kubectl get gateways -n submariner-operator -o wide 2>/dev/null
echo "=== ENDPOINTS ===" && kubectl get endpoints.submariner.io -A 2>/dev/null
echo "=== CRDs ==="      && kubectl get crd 2>/dev/null | grep -iE "submar|lighthouse|globalnet|broker|multicluster"
```

Debug pod on the gateway node with all its taints tolerated:
```bash
kubectl run nettest --image=nicolaka/netshoot --restart=Never --overrides='{
  "spec":{
    "hostNetwork":true,
    "nodeSelector":{"kubernetes.io/hostname":"adeo-gpu-03"},
    "tolerations":[
      {"key":"tenant","value":"adeo","effect":"NoSchedule"},
      {"key":"tenant","value":"adeo","effect":"NoExecute"},
      {"key":"target","value":"k8s","effect":"NoSchedule"},
      {"key":"target","value":"k8s","effect":"NoExecute"},
      {"key":"nvidia.com/gpu","effect":"NoSchedule"}
    ]
  }
}' --command -- sleep infinity
```

TCP 6443 reachability to the broker (Abu Dhabi API server):
```bash
kubectl exec nettest -- python3 -c '
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(5)
t0 = time.time()
try:
    s.connect(("10.10.128.71", 6443)); dt=(time.time()-t0)*1000
    print(f"TCP 6443 -> 10.10.128.71: CONNECTED in {dt:.0f}ms  [FIREWALL: OPEN]"); s.close()
except socket.timeout: print("TCP 6443: TIMEOUT [BLOCKED or host down]")
except ConnectionRefusedError: print("TCP 6443: REFUSED [OPEN, nothing listening]")
'
```

UDP reachability probe (note: 51820 is NOT used by Submariner — this was an early misconception;
the real ports are 4500/4490):
```bash
kubectl exec nettest -- python3 -c '
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(4)
s.sendto(b"\x00"*32, ("10.10.128.72", 4500))
try:
    data, addr = s.recvfrom(1500); print(f"UDP 4500 -> got reply from {addr}")
except socket.timeout: print("UDP 4500 -> no reply (silent drop OR open-but-no-listener)")
'
```

## A.2 WireGuard handshake debugging (packets arrive at NIC but WG RX stays 0)

Gateway pod env + hostNetwork check (substitute the real pod name):
```bash
GW=$(kubectl -n submariner-operator get pod -l app=submariner-gateway -o name | head -1); GW=${GW#pod/}
kubectl -n submariner-operator get pod $GW -o jsonpath='{range .spec.containers[*].env[*]}{.name}={.value}{"\n"}{end}' \
  | grep -iE "port|server|endpoint|nat|force|ce_IP|public|cluster"
kubectl -n submariner-operator get pod $GW -o jsonpath='{.spec.hostNetwork}'
```

WireGuard interface + UDP sockets inside the gateway pod:
```bash
kubectl -n submariner-operator exec $GW -c submariner-gateway -- sh -c '
which wg 2>/dev/null && wg show 2>/dev/null || echo "wg tool not found";
ip -s link show submariner 2>/dev/null;
ss -ulpn 2>/dev/null | grep -E "4500|4490";
'
```

Compare endpoint public keys across clusters (rules out key mismatch):
```bash
kubectl get endpoints.submariner.io -A -o jsonpath='{range .items[*]}{"\n"}{.metadata.name}{"\n  publicKey="}{.spec.backend_config.publicKey}{"\n  clusterID="}{.spec.cluster_id}{"\n"}{end}'
```

**tcpdump — proof that return packets physically arrive at the Al Ain node** (this disproved the
"firewall return-path blocked" theory):
```bash
kubectl run sniff -n oik8s-cilium-system --restart=Never --image-pull-policy=IfNotPresent -i --rm \
  --image=registry.adeoaiengine.ecouncil.ae/submariner/nettest:0.24.0 \
  --overrides='{"spec":{"hostNetwork":true,"nodeSelector":{"kubernetes.io/hostname":"adeo-gpu-03"},"tolerations":[{"operator":"Exists"}],"containers":[{"name":"sniff","image":"registry.adeoaiengine.ecouncil.ae/submariner/nettest:0.24.0","command":["sh","-c","timeout 20 /usr/sbin/tcpdump -n -i any \"udp and src host 10.10.128.72 and (dst port 4490 or dst port 4500)\" 2>&1"],"securityContext":{"privileged":true,"capabilities":{"add":["NET_RAW","NET_ADMIN"]}}}]}}'
```

iptables INPUT inspection (privileged hostNetwork pod):
```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata: { name: ipt-check, namespace: oik8s-cilium-system }
spec:
  hostNetwork: true
  nodeSelector: { kubernetes.io/hostname: adeo-gpu-03 }
  tolerations: [{ operator: Exists }]
  containers:
  - name: ipt
    image: registry.adeoaiengine.ecouncil.ae/submariner/nettest:0.24.0
    command: ["sh","-c","iptables -t filter -L INPUT -n -v; iptables-save | grep -iE '4500|4490'; sleep 300"]
    securityContext: { privileged: true }
  restartPolicy: Never
EOF
kubectl -n oik8s-cilium-system exec ipt-check -- sh -c 'iptables -t filter -L INPUT -n -v; iptables-save | grep -iE "4500|4490|submariner"'
```

conntrack — why 4490 works but 4500 doesn't (run inside the cilium agent):
```bash
CILIUM_POD=$(kubectl -n oik8s-cilium-system get pod --field-selector spec.nodeName=adeo-gpu-03 -o name | head -1)
kubectl -n oik8s-cilium-system exec $CILIUM_POD -c cilium-agent -- sh -c 'conntrack -L | grep "4490"'
kubectl -n oik8s-cilium-system exec $CILIUM_POD -c cilium-agent -- sh -c 'conntrack -L | grep "4500"'
```

iptables TRACE to follow the packet path through netfilter:
```bash
kubectl -n oik8s-cilium-system exec $CILIUM_POD -c cilium-agent -- sh -c \
  'iptables -t raw -I PREROUTING -p udp --dport 4500 -s 10.10.128.72 -j TRACE'
sleep 8
kubectl -n oik8s-cilium-system exec $CILIUM_POD -c cilium-agent -- sh -c 'dmesg | grep -i "TRACE" | grep "4500" | tail -20'
```

Cilium datapath config (is it bypassing iptables?):
```bash
kubectl -n oik8s-cilium-system exec $CILIUM_POD -c cilium-agent -- cilium config \
  | grep -iE "host|firewall|iptables|bpf|kube-proxy|datapath|tunnel|enable"
kubectl -n oik8s-cilium-system get cm cilium-config -o yaml \
  | grep -iE "host-firewall|enable-host|kube-proxy|datapath|tunnel|bpf|iptables|mode"
```

Hexdump packets to identify the WireGuard message type (02 = handshake response):
```bash
kubectl run sniff-hex -n oik8s-cilium-system --restart=Never -i --rm \
  --image=registry.adeoaiengine.ecouncil.ae/submariner/nettest:0.24.0 \
  --overrides='{"spec":{"hostNetwork":true,"nodeSelector":{"kubernetes.io/hostname":"adeo-gpu-03"},"tolerations":[{"operator":"Exists"}],"containers":[{"name":"sniff","image":"registry.adeoaiengine.ecouncil.ae/submariner/nettest:0.24.0","command":["sh","-c","timeout 10 /usr/sbin/tcpdump -n -xx -i any \"udp and src host 10.10.128.72 and dst port 4500\" -c 2 2>&1"],"securityContext":{"privileged":true,"capabilities":{"add":["NET_RAW","NET_ADMIN"]}}}]}}'
```

Distinguish the two WireGuard interfaces (Cilium's `cilium_wg0` vs Submariner's `submariner`):
```bash
kubectl -n oik8s-cilium-system exec $CILIUM_POD -c cilium-agent -- sh -c '
cat /sys/class/net/submariner/statistics/rx_packets;
cat /sys/class/net/cilium_wg0/statistics/rx_packets;
ip -d link show submariner;
ip -d link show cilium_wg0;
'
```

## A.3 Getting the `wg` binary onto the node (ConfigMap + chroot technique)

```bash
# on aitdev00 (has apt):
sudo apt-get install -y wireguard-tools && wg --version

# encode the binary and ship it via ConfigMap:
base64 /usr/bin/wg > /tmp/wg.b64
kubectl -n oik8s-cilium-system create configmap wg-binary --from-file=wg.b64=/tmp/wg.b64

# privileged pod mounting host root at /host AND the wg ConfigMap:
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata: { name: wg-debug, namespace: oik8s-cilium-system }
spec:
  hostNetwork: true
  hostPID: true
  nodeSelector: { kubernetes.io/hostname: adeo-gpu-03 }
  tolerations: [{ operator: Exists }]
  containers:
  - name: debug
    image: registry.adeoaiengine.ecouncil.ae/submariner/nettest:0.24.0
    command: ["sleep","600"]
    securityContext: { privileged: true }
    volumeMounts:
    - { name: host, mountPath: /host }
    - { name: wg-cm, mountPath: /wg-cm }
  volumes:
  - { name: host,  hostPath: { path: /, type: Directory } }
  - { name: wg-cm, configMap: { name: wg-binary } }
  restartPolicy: Never
EOF

# decode into a runnable binary on the host, then run wg show against the host netns:
kubectl -n oik8s-cilium-system exec wg-debug -- sh -c 'base64 -d /wg-cm/wg.b64 > /tmp/wg && chmod +x /tmp/wg && cp /tmp/wg /host/tmp/wg'
kubectl -n oik8s-cilium-system exec wg-debug -- sh -c 'chroot /host /tmp/wg show submariner'
```
Interpretation: a `latest handshake:` line + RX climbing = tunnel up. No handshake + RX ~0 =
handshake failing. `preshared key: (hidden)` = a PSK is configured on this peer (PSK-mismatch hint).

## A.4 FIX — PSK mismatch (root cause #1)

Discover:
```bash
kubectl -n submariner-operator get submariner submariner -o jsonpath='{.spec.ceIPSecPSK}{"\n"}'   # Al Ain has one
# On Abu Dhabi, the same command returned EMPTY -> mismatch.
GW=$(kubectl -n submariner-operator get pod -l app=submariner-gateway -o name | head -1); GW=${GW#pod/}
kubectl -n submariner-operator get pod $GW -o jsonpath='{range .spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' | grep -i psk
```
Fix — set BOTH clusters to the identical PSK. On **Abu Dhabi**:
```bash
kubectl -n submariner-operator patch submariner submariner --type=merge \
  -p '{"spec":{"ceIPSecPSK":"JjzOfQTMcwbnDDHiVJC1bs+/Jyr56FsGlIkuaknrVy6jFjUVB4CJ1AShlfSsi0v2"}}'
kubectl -n submariner-operator get submariner submariner -o jsonpath='{.spec.ceIPSecPSK}{"\n"}'   # verify
kubectl -n submariner-operator delete pod -l app=submariner-gateway   # restart to re-read
```
(Al Ain already had this value; if generating fresh, set the same string on both.)

## A.5 FIX — UFW blocking UDP 4500/4490 (host firewall, discovered via chroot)

Privileged pod with host filesystem (this is the workhorse pod for all host-firewall work):
```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata: { name: ufw-config, namespace: oik8s-cilium-system }
spec:
  hostNetwork: true
  hostPID: true
  nodeSelector: { kubernetes.io/hostname: adeo-gpu-03 }
  tolerations: [{ operator: Exists }]
  containers:
  - name: ufw
    image: registry.adeoaiengine.ecouncil.ae/submariner/nettest:0.24.0
    command: ["sleep","infinity"]
    securityContext:
      privileged: true
      capabilities: { add: ["NET_ADMIN","NET_RAW","SYS_MODULE","SYS_CHROOT"] }
    volumeMounts: [{ name: host, mountPath: /host }]
  volumes: [{ name: host, hostPath: { path: /, type: Directory } }]
  restartPolicy: Never
EOF
```
Check + fix (chroot runs the host's own ufw, writing persistent /etc/ufw/user.rules):
```bash
kubectl -n oik8s-cilium-system exec ufw-config -- sh -c 'chroot /host /usr/sbin/ufw status verbose'
kubectl -n oik8s-cilium-system exec ufw-config -- sh -c '
chroot /host /usr/sbin/ufw allow from 10.10.128.72 to any port 4500 proto udp comment "Submariner WireGuard data tunnel (Abu Dhabi)"
chroot /host /usr/sbin/ufw allow from 10.10.128.72 to any port 4490 proto udp comment "Submariner NAT discovery (Abu Dhabi)"
'
kubectl -n oik8s-cilium-system exec ufw-config -- sh -c 'chroot /host /usr/sbin/ufw status verbose' | grep -E "4500|4490|Submariner"
# persistence proof:
kubectl -n oik8s-cilium-system exec ufw-config -- sh -c 'grep "4500\|4490" /host/etc/ufw/user.rules'
```
Restart the gateway for a fresh handshake:
```bash
kubectl -n submariner-operator delete pod -l app=submariner-gateway --force --grace-period=0
sleep 5
kubectl -n submariner-operator wait --for=condition=Ready pod -l app=submariner-gateway --timeout=60s
```

## A.6 FIX — UFW blocking UDP 4800 (intra-cluster VXLAN, root cause #2)

Discover (handshake now works, but pinger fails; `vx-submariner` RX=0):
```bash
CILIUM_POD=$(kubectl -n oik8s-cilium-system get pod --field-selector spec.nodeName=adeo-gpu-03 -o name | head -1)
kubectl -n oik8s-cilium-system exec $CILIUM_POD -c cilium-agent -- cilium status | grep -iE 'masquerad|routing|tunnel|kubeproxy|encryption'

RA=$(kubectl -n submariner-operator get pod -l app=submariner-routeagent --field-selector spec.nodeName=adeo-master-01 -o name | head -1)
kubectl -n submariner-operator logs "$RA" -c submariner-routeagent --tail=100 | grep -iE 'cni|network.plugin|generic|cilium|vx-submariner|route.*table|iptables'

RA_GW=$(kubectl -n submariner-operator get pod -l app=submariner-routeagent --field-selector spec.nodeName=adeo-gpu-03 -o name | head -1)
kubectl -n submariner-operator logs "$RA_GW" -c submariner-routeagent --tail=100 | grep -iE 'vxlan|vx-submariner|4800|cilium|generic|route'

kubectl -n submariner-operator get submariner submariner -o jsonpath='{.status.networkPlugin}'
kubectl -n submariner-operator exec "$RA"    -c submariner-routeagent -- ip -s link show vx-submariner
kubectl -n submariner-operator exec "$RA_GW" -c submariner-routeagent -- ip -s link show vx-submariner

kubectl -n oik8s-cilium-system exec ufw-config -- sh -c 'chroot /host /usr/sbin/ufw status | grep 4800 || echo "NO RULE FOR 4800"'
```
Fix (allow UDP 4800 on the gateway node):
```bash
kubectl -n oik8s-cilium-system exec ufw-config -- sh -c 'chroot /host /usr/sbin/ufw allow from 10.34.104.0/24 to any port 4800 proto udp comment "Submariner intra-cluster VXLAN"'
kubectl -n oik8s-cilium-system exec ufw-config -- sh -c 'chroot /host /usr/sbin/ufw allow to any port 4800 proto udp comment "Submariner intra-cluster VXLAN"'
kubectl -n oik8s-cilium-system exec ufw-config -- sh -c 'chroot /host /usr/sbin/ufw status | grep 4800'
sleep 10
kubectl -n submariner-operator exec "$RA_GW" -c submariner-routeagent -- ip -s link show vx-submariner | grep -A2 'RX:\|TX:'   # RX should now climb
kubectl -n submariner-operator delete pod -l app=submariner-gateway --force --grace-period=0
```

## A.7 FIX — Missing globalnet RBAC (root cause #3)

Discover (handshake + VXLAN work, but ping 242.0.0.254 = 100% loss; submariner iface has no IP):
```bash
kubectl -n oik8s-cilium-system exec wg-debug -- sh -c 'chroot /host /sbin/ip addr show submariner'      # no IP
kubectl -n oik8s-cilium-system exec wg-debug -- sh -c 'chroot /host /sbin/ip route show table 150'
kubectl -n oik8s-cilium-system exec wg-debug -- sh -c 'ping -c 3 -W 2 242.0.0.254'                      # 100% loss

kubectl -n submariner-operator get pod -l app=submariner-globalnet                                      # DESIRED 1 CURRENT 0
kubectl -n submariner-operator describe ds submariner-globalnet | tail -30
#   -> "serviceaccount \"submariner-globalnet\" not found"
kubectl -n submariner-operator get sa | grep globalnet                                                  # missing!
kubectl get clusterrole,clusterrolebinding -o name | grep submariner                                    # no globalnet
```
Fix — create all 5 RBAC objects (verbatim, works without the source dir):
```bash
cat <<'ENDOFYAML' | kubectl apply -f -
---
apiVersion: v1
kind: ServiceAccount
metadata: { name: submariner-globalnet, namespace: submariner-operator }
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: submariner-globalnet, namespace: submariner-operator }
rules:
  - { apiGroups: [""],                 resources: [configmaps], verbs: [get, list, watch] }
  - { apiGroups: [submariner.io],      resources: [gateways],   verbs: [get, list, watch, update] }
  - { apiGroups: [coordination.k8s.io], resources: [leases],    verbs: [get, list, watch, create, update, delete] }
---
kind: RoleBinding
apiVersion: rbac.authorization.k8s.io/v1
metadata: { name: submariner-globalnet, namespace: submariner-operator }
subjects: [{ kind: ServiceAccount, name: submariner-globalnet, namespace: submariner-operator }]
roleRef: { kind: Role, name: submariner-globalnet, apiGroup: rbac.authorization.k8s.io }
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: { name: submariner-globalnet }
rules:
  - { apiGroups: [""],            resources: [pods],                verbs: [get, list, watch] }
  - { apiGroups: [""],            resources: [services, endpoints], verbs: [create, get, list, watch, update, delete] }
  - { apiGroups: [submariner.io], resources: [clusters, endpoints], verbs: [get, list, watch] }
  - apiGroups: [submariner.io]
    resources: [clusterglobalegressips, clusterglobalegressips/status, globalegressips, globalegressips/status, globalingressips, globalingressips/status]
    verbs: [create, get, list, watch, update, delete, deletecollection]
  - { apiGroups: [multicluster.x-k8s.io], resources: [serviceexports],      verbs: [get, list, watch] }
  - { apiGroups: [network.openshift.io],  resources: [service/externalips], verbs: [create, get, list, delete] }
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: { name: submariner-globalnet }
subjects: [{ kind: ServiceAccount, name: submariner-globalnet, namespace: submariner-operator }]
roleRef: { kind: ClusterRole, name: submariner-globalnet, apiGroup: rbac.authorization.k8s.io }
ENDOFYAML

kubectl -n submariner-operator rollout restart ds submariner-globalnet
```
Verify — the full success chain:
```bash
kubectl -n submariner-operator get pods -l app=submariner-globalnet -o wide          # Running
kubectl -n oik8s-cilium-system exec wg-debug -- sh -c 'chroot /host /sbin/ip addr show submariner'   # now has 242.0.1.x IP
kubectl -n oik8s-cilium-system exec wg-debug -- sh -c 'ping -c 3 -W 2 242.0.0.254'    # 0% loss, ~4ms
kubectl -n submariner-operator get submariner submariner -o jsonpath='{.status.gateways[0].connections[0].status}{"\n"}'  # connected
kubectl -n oik8s-cilium-system exec wg-debug -- sh -c 'chroot /host /tmp/wg show submariner'  # latest handshake: Ns ago
subctl show connections   # STATUS connected, RTT ~4.5ms
```

## A.8 Cleanup of all debug artefacts
```bash
kubectl -n oik8s-cilium-system delete pod nettest sniff sniff-hex ipt-check ufw-config ufw-check2 wg-debug host-debug wgcheck --ignore-not-found --force --grace-period=0
kubectl -n oik8s-cilium-system delete configmap wg-binary --ignore-not-found
rm -f /tmp/wg.b64 /tmp/broker-blob.txt broker-creds.yaml
```

## A.9 Remove the old manual NAT gateway (replaced by §7e)

If you previously set up a manual NAT gateway (iptables MASQUERADE + static routes on every node)
before switching to the clean `nodeSelector` approach (§7e), remove the old rules. Use the `nat-debug`
pod on `adeo-gpu-03` (or any privileged hostPath pod on that node) and the routeagent pods on other
nodes (they run with `hostNetwork` so they see the host routing table):

```bash
# 1) Remove iptables MASQUERADE + FORWARD rules on adeo-gpu-03:
kubectl -n oik8s-cilium-system exec nat-debug -- chroot /host sh -c '
  iptables -t nat -D POSTROUTING -d 10.10.128.0/24 -j MASQUERADE 2>/dev/null
  iptables -D FORWARD -s 10.34.104.0/24 -d 10.10.128.0/24 -j ACCEPT 2>/dev/null
  iptables -D FORWARD -s 10.10.128.0/24 -d 10.34.104.0/24 -j ACCEPT 2>/dev/null
  echo "Remaining custom NAT/FORWARD rules for 10.10.128.0/24:"
  iptables -t nat -S | grep "10.10.128.0/24" | grep -v CILIUM
  iptables -S FORWARD | grep "10.10.128.0/24"
'   # both outputs should be empty

# 2) Remove static routes on all non-gateway nodes (routeagent pods have hostNetwork):
for pod in $(kubectl -n submariner-operator get pods -l app=submariner-routeagent -o jsonpath='{.items[*].metadata.name}'); do
    node=$(kubectl -n submariner-operator get pod $pod -o jsonpath='{.spec.nodeName}')
    [ "$node" != "adeo-gpu-03" ] && \
      echo "Cleaning $node" && \
      kubectl -n submariner-operator exec $pod -- ip route del 10.10.128.0/24 via 10.34.104.19 dev bond0 2>/dev/null
done

# 3) Verify no node still has the static route:
for pod in $(kubectl -n submariner-operator get pods -l app=submariner-routeagent -o jsonpath='{.items[*].metadata.name}'); do
    node=$(kubectl -n submariner-operator get pod $pod -o jsonpath='{.spec.nodeName}')
    route=$(kubectl -n submariner-operator exec $pod -- ip route show 10.10.128.0/24 2>/dev/null)
    echo "$node: ${route:-CLEAN}"
done
```

## A.10 Verify lighthouse broker sync after pinning pods (§7e)

After pinning lighthouse pods to `adeo-gpu-03`, confirm the agent can still sync ServiceImports
from the broker:

```bash
# lighthouse agent logs should show ServiceImport sync (not connection errors):
kubectl -n submariner-operator logs -l app=submariner-lighthouse-agent --tail=20 | grep -iE 'serviceimport|Ready|broker|error'

# cross-cluster connectivity (from any pod on adeo-gpu-03):
kubectl -n oik8s-cilium-system exec nat-debug -- curl -s -o /dev/null -w '%{http_code}\n' \
  --connect-timeout 5 http://242.0.0.253:8080/health   # should return 200
```

## A.11 Running kubectl against Abu Dhabi from adeo-gpu-03

Only `adeo-gpu-03` (`10.34.104.19`) has the firewall rule allowing TCP 6443 to the Abu Dhabi API
server (`10.10.128.71`). You can't reach Abu Dhabi's kubectl from `aitdev00` or any other Al Ain
node. To run kubectl against Abu Dhabi, create a privileged debug pod on `adeo-gpu-03` with the
kubeconfig mounted, then use the host's `kubectl` binary via chroot.

**Step 1 — Export the Abu Dhabi kubeconfig from `aitdev00`:**

The kubeconfig must point at `10.10.128.71:6443` (the broker API server, which is the only Abu
Dhabi API server reachable through the firewall). If it points at a different server IP (e.g.
`10.10.128.75`), fix it:
```bash
# on aitdev00:
sed 's|10.10.128.75:6443|10.10.128.71:6443|g' /tmp/abudhabi-kubeconfig > /tmp/abudhabi-kubeconfig-fixed
grep server /tmp/abudhabi-kubeconfig-fixed   # must show 10.10.128.71:6443
```

**Step 2 — Ship the kubeconfig into a pod on `adeo-gpu-03` via ConfigMap:**

The nettest image lacks `tar` so `kubectl cp` doesn't work. Use a ConfigMap instead:
```bash
kubectl -n oik8s-cilium-system create configmap abudhabi-kubeconfig \
  --from-file=abudhabi-kubeconfig-fixed=/tmp/abudhabi-kubeconfig-fixed

cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata: { name: ad-debug, namespace: oik8s-cilium-system }
spec:
  hostNetwork: true
  hostPID: true
  nodeSelector: { kubernetes.io/hostname: adeo-gpu-03 }
  tolerations: [{ operator: Exists }]
  containers:
  - name: debug
    image: registry.adeoaiengine.ecouncil.ae/submariner/nettest:0.24.0
    command: ["sleep","600"]
    securityContext: { privileged: true }
    volumeMounts:
    - { name: host, mountPath: /host }
    - { name: kubeconfig, mountPath: /kubeconfig }
  volumes:
  - { name: host, hostPath: { path: /, type: Directory } }
  - { name: kubeconfig, configMap: { name: abudhabi-kubeconfig } }
  restartPolicy: Never
EOF

kubectl -n oik8s-cilium-system wait --for=condition=Ready pod/ad-debug --timeout=30s
```

**Step 3 — Run kubectl against Abu Dhabi via the host's binary:**

The host has `kubectl` at `/usr/bin/kubectl`. Copy the kubeconfig to the host filesystem so chroot
can read it, then run any kubectl command:
```bash
# copy kubeconfig into the host filesystem:
kubectl -n oik8s-cilium-system exec ad-debug -- \
  sh -c 'cp /kubeconfig/abudhabi-kubeconfig-fixed /host/tmp/abudhabi-kubeconfig'

# now run kubectl against Abu Dhabi (chroot uses the host's kubectl binary):
kubectl -n oik8s-cilium-system exec ad-debug -- chroot /host sh -c \
  'KUBECONFIG=/tmp/abudhabi-kubeconfig kubectl -n submariner-operator get pods -o wide'

# examples:
kubectl -n oik8s-cilium-system exec ad-debug -- chroot /host sh -c \
  'KUBECONFIG=/tmp/abudhabi-kubeconfig kubectl get nodes -o wide'

kubectl -n oik8s-cilium-system exec ad-debug -- chroot /host sh -c \
  'KUBECONFIG=/tmp/abudhabi-kubeconfig kubectl -n submariner-operator get servicediscovery -o yaml'

kubectl -n oik8s-cilium-system exec ad-debug -- chroot /host sh -c \
  'KUBECONFIG=/tmp/abudhabi-kubeconfig kubectl get globalingressip -A'
```

**Step 4 — Clean up when done:**
```bash
kubectl -n oik8s-cilium-system delete pod ad-debug --ignore-not-found --force --grace-period=0
kubectl -n oik8s-cilium-system delete configmap abudhabi-kubeconfig --ignore-not-found
rm -f /tmp/abudhabi-kubeconfig-fixed
# /tmp/abudhabi-kubeconfig remains on the adeo-gpu-03 host (contains broker token; delete it if concerned):
#   kubectl -n oik8s-cilium-system exec ad-debug -- chroot /host sh -c 'rm -f /tmp/abudhabi-kubeconfig'
```

> **Why not `kubectl cp`?** The nettest image doesn't include `tar`, which `kubectl cp` requires.
> The ConfigMap approach avoids this entirely. If you have an image with `kubectl` built in
> (e.g. `bitnami/kubectl`), you can skip the chroot and run directly in the container.
>
> **Why `10.10.128.71` and not `10.10.128.75`?** The firewall rule only allows
> `10.34.104.19 -> 10.10.128.71:6443`. Other Abu Dhabi API server IPs (`.72`-`.76`) are not
> reachable from Al Ain. The kubeconfig may list a different server; always fix it to `.71`.

**Reference — the full Abu Dhabi kubeconfig (server already fixed to `10.10.128.71`):**

Save this as `/tmp/abudhabi-kubeconfig` on `aitdev00` and skip Step 1 entirely:
```yaml
apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSUJlVENDQVIrZ0F3SUJBZ0lCQURBS0J
nZ3Foa2pPUFFRREFqQWtNU0l3SUFZRFZRUUREQmx5YTJVeUxYTmwKY25abGNpMWpZVUF4TnpVNU1UVXpPREkwTUI0WERUSTFNRGt5T1
RFek5UQXlORm9YRFRNMU1Ea3lOekV6TlRBeQpORm93SkRFaU1DQUdBMVVFQXd3WmNtdGxNaTF6WlhKMlpYSXRZMkZBTVRjMU9URTFNe
md5TkRCWk1CTUdCeXFHClNNNDlBZ0VHQ0NxR1NNNDlBd0VIQTBJQUJPWXJUMUhUSFpOT2xndHRVRDV2L2EwYWNQOUVWWFdjcWFxQlc2
MnEKT3JzcDg4NzJ5UGRxbEk0amlkd3dNTUNmWEFjTFVSYzBQMjAvZWdZZEF5YzRpb1dqUWpCQU1BNEdBMVVkRHdFQgovd1FFQXdJQ3B
EQVBCZ05WSFJNQkFmOEVCVEFEQVFIL01CMEdBMVVkRGdRV0JCVGNxRVhCZGwxOW5tdktraWhPCnluRERMZHNrelRBS0JnZ3Foa2pPUF
FRREFnTklBREJGQWlCaFVvdFhvdTJpQk9pL0lkNkdWTWNBU2FjcC9LazIKYkFyOThPL2RtOHdIT0FJaEFKSDZRTE5yb3BGNERkUENRN
GFodDBSVGN5aTZwc3MxUHdjWDd4YWtXYk9yCi0tLS0tRU5EIENFUlRJRklDQVRFLS0tLS0K
    server: https://10.10.128.71:6443
  name: default
contexts:
- context:
    cluster: default
    user: default
  name: default
current-context: default
kind: Config
preferences: {}
users:
- name: default
  user:
    client-certificate-data: LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSUJrekNDQVRpZ0F3SUJBZ0lJSnZZNHc0L0
txLzR3Q2dZSUtvWkl6ajBFQXdJd0pERWlNQ0FHQTFVRUF3d1oKY210bE1pMWpiR2xsYm5RdFkyRkFNVGMxT1RFMU16Z3lOREFlRncwe
U5UQTVNamt4TXpVd01qUmFGdzB5TmpBNQpNamt4TXpVd01qUmFNREF4RnpBVkJnTlZCQW9URG5ONWMzUmxiVHB0WVhOMFpYSnpNUlV3
RXdZRFZRUURFd3h6CmVYTjBaVzA2WVdSdGFXNHdXVEFUQmdjcWhrak9QUUlCQmdncWhrak9QUU1CQndOQ0FBVEMyaXhyeklVMEhKMHg
KVCtWSVhOWTJyOUJXN3hJRTdSUnlxeGoySHNrKzdvdGM0MkpKYk92djdmREpEYVJlNkRvR1k3WksvaVl3a2ZBZQpnQTFnTU5mOW8wZ3
dSakFPQmdOVkhROEJBZjhFQkFNQ0JhQXdFd1lEVlIwbEJBd3dDZ1lJS3dZQkJRVUhBd0l3Ckh3WURWUjBqQkJnd0ZvQVVhSDY5Q0tOT
XIzcXVYU1A2UDRHR2FVQVNVZ0F3Q2dZSUtvWkl6ajBFQXdJRFNRQXcKUmdJaEFPNXpDVk9PcFFxcnhYSXhTTFZrU1REVndzTEtEcm42
NDkyazl0aFdwc2FWQWlFQTEvR0IxZVhHV0w3awprMmwxU1hEc3hTenl2cXN6MmhMM3ByMm9pTHBPTTU4PQotLS0tLUVORCBDRVJUSUZ
JQ0FURS0tLS0tCi0tLS0tQkVHSU4gQ0VSVElGSUNBVEUtLS0tLQpNSUlCZURDQ0FSK2dBd0lCQWdJQkFEQUtCZ2dxaGtqT1BRUURBak
FrTVNJd0lBWURWUVFEREJseWEyVXlMV05zCmFXVnVkQzFqWVVBeE56VTVNVFV6T0RJME1CNFhEVEkxTURreU9URXpOVEF5TkZvWERU
TTFNRGt5TnpFek5UQXkKTkZvd0pERWlNQ0FHQTFVRUF3d1pjbXRsTWkxamJHbGxiblF0WTJGQU1UYzFPVEUxTXpneU5EQlpNQk1HQnlx
RwpTTTQ5QWdFR0NDcUdTTTQ5QXdFSEEwSUFCSXo2WHhwdWhuY0gyYUd6V29Dc3JvM0puK21XQXFhalh0VmNoY1I2CjljTUVZSmhKOUN
1cW9VVUlXV1VYUlVhK2NQdE1RcXVoemlEa2N3RzZqTEhvZFhLalFqQkFNQTRHQTFVZER3RUIKL3dRRUF3SUNwREFQQmdOVkhSTUJBZj
hFQlRBREFRSC9NQjBHQTFVZERnUVdCQlJvZnIwSW8weXZlcTVkSS9vLwpnWVpwUUJKU0FEQUtCZ2dxaGtqT1BRUURBZ05IQURCRUFpQ
VZjQ3htMFNKZEpUMXYrMHNzbzNEWDZXeVlBdXplCktOcmtvWXpDNXc0bjNBSWdVOGhCR0o3VlBLa2NKZ2hZVWV1N09NNDJLYWUxd1d2
VjhpZUY1YzNwV1MwPQotLS0tLUVORCBDRVJUSUZJQ0FURS0tLS0tCg==
    client-key-data: LS0tLS1CRUdJTiBFQyBQUklWQVRFIEtFWS0tLS0tCk1IY0NBUUVFSUx5MEtXa2V2YTBXOTZZNlZEZGMyN3
NVeG1kbnJtZW1MOWpWQ3dpazhWTzlvQW9HQ0NxR1NNNDkKQXdFSG9VUURRZ0FFd3Rvc2E4eUZOQnlkTVUvbFNGeldOcS9RVnU4U0JP
MFVjcXNZOWg3SlB1NkxYT05pU1d6cgo3KzN3eVEya1h1ZzZCbU8yU3Y0bU1KSHdIb0FOWUREWC9RPT0KLS0tLS1FTkQgRUMgUFJJVk
FURSBLRVktLS0tLQo=
```
> This kubeconfig uses client certificate/key auth (not a bearer token). The certificate is tied to
> the Abu Dhabi cluster's CA. If the cert expires, re-export from an Abu Dhabi master node.
