# Submariner Deployment — AL AIN Cluster (Joins the Abu Dhabi Broker)

Complete, self-contained runbook for joining the air-gapped Al Ain OICM cluster to the
existing Abu Dhabi Submariner broker (0.24.0). Al Ain runs the **operator chart only** —
the broker already exists in Abu Dhabi. End goal: Al Ain's LiteLLM gateway reaches the
GLM 5.2 model in Abu Dhabi over an encrypted tunnel.

> Al Ain is the harder side. It hit **seven** distinct problems the first time. Every one is
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
> Only the **gateway node** needs 4800 inbound (non-gateway nodes only send *to* it). If you add a
> second gateway for HA later, that node needs the same rule.

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

---

## 10. Firewall summary

| Source | Destination | Port | Proto | Purpose |
|---|---|---|---|---|
| `10.34.104.19` ↔ `10.10.128.72` (both ways) | | **4500** | UDP | WireGuard tunnel data |
| `10.34.104.19` ↔ `10.10.128.72` (both ways) | | **4490** | UDP | NAT-traversal discovery |
| `10.34.104.19` → `10.10.128.71` | | **6443** | TCP | Broker (metadata) |
| Al Ain nodes `10.34.104.0/24` → `adeo-gpu-03` | | **4800** | UDP | **Intra-cluster VXLAN (UFW, §7d)** |

Bidirectional is required for 4490/4500 (WireGuard reply must return; stateful rules can expire in
the handshake gaps). **51820 is NOT used.**

---

## 11. The three root causes of the long outage (reference)

Three independent bugs, stacked — each hid the next, so fixing one alone still showed `error`:

1. **PSK mismatch** — Abu Dhabi had no PSK, Al Ain did → WireGuard silently dropped handshake responses (MAC verify fails). Fix: identical `ceIPSecPSK` both clusters.
2. **UFW blocking UDP 4800** — intra-cluster VXLAN dropped on the gateway node → pinger failed. Fix: `ufw allow ... 4800/udp` on `adeo-gpu-03` (via chroot).
3. **Missing globalnet RBAC** — chart never created it → globalnet pod never scheduled → `submariner` interface had no global IP → health check failed. Fix: apply the 5 RBAC objects from source.

Empirical lessons that cracked it: tcpdump proved packets *were* arriving (ruled out "firewall
return-path"); `wg show` (via chroot) proved the handshake state; reading the operator RBAC from
source revealed the chart gap; checking UFW on the actual host (not the pod) found the 4800 block.
`subctl diagnose all` independently flagged the VXLAN(4800) and CNI issues. Ground-truth inspection
beat reasoning-from-symptoms every time.

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

## 13. Cleanup + hardening
- Delete debug artefacts: `kubectl -n oik8s-cilium-system delete pod host-debug wgcheck --ignore-not-found; kubectl -n oik8s-cilium-system delete configmap wg-binary --ignore-not-found`.
- `rm -f /tmp/broker-blob.txt broker-creds.yaml /tmp/wg.b64` (hold the token/PSK).
- **Persist out-of-Helm fixes** so `helm upgrade --reuse-values` doesn't revert them: PSK + air-gapped in values; re-apply globalnet RBAC after any upgrade; UFW 4800 rule is host-side (already persistent).
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
