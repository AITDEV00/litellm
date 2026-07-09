# Submariner Deployment — ABU DHABI Cluster (Broker Host + First Member)

Complete, self-contained runbook for standing up Submariner 0.24.0 on the air-gapped
Abu Dhabi OICM cluster. Abu Dhabi hosts the **Broker** and joins itself as the first
clusterset member. This is the foundation the Al Ain cluster later joins.

> **Read this alongside the Al Ain guide.** Several problems only surface on the second
> cluster, but two of them (the **PSK** and the **globalnet RBAC**) must be understood
> here because their correct values/objects have to match across both clusters.

---

## 0. Environment

| Item | Value |
|---|---|
| Role | **Broker host** + first member |
| Distro / K8s | RKE2 v1.31.3 |
| CNI | Canal (Flannel + Calico) — **detected correctly** by Submariner |
| Pod CIDR / Service CIDR | `10.42.0.0/16` / `10.43.0.0/16` (identical to Al Ain → Globalnet required) |
| Global CIDR (this cluster) | `242.0.0.0/24` |
| Master (API / Broker) | `prd-oi-k8master` — `10.10.128.71:6443` |
| Gateway node (chosen) | `prd-oi-k8worker01` — `10.10.128.72` (non-GPU worker) |
| Harbor | `harbor.ai.ecouncil.ae` |
| Bastion (kubeconfig host) | `prd-oi-bstn` (10.10.128.70), context `default` |
| Cable driver | WireGuard — tunnel ports **UDP 4500 (data) + 4490 (NAT discovery)**, NOT 51820 |

---

## 1. Tooling on the bastion (air-gapped — carry binaries in)

`helm`, `subctl`, and the two chart tarballs must be transferred in (no internet on the bastion).

```bash
# ON AN INTERNET HOST — download linux-amd64 builds:
curl -fLO https://get.helm.sh/helm-v3.16.3-linux-amd64.tar.gz
curl -fsSL https://api.github.com/repos/submariner-io/subctl/releases \
  | grep -E 'browser_download_url.*linux-amd64' | grep -E '0\.24' | head   # get the real asset URL
curl -fLO https://github.com/submariner-io/subctl/releases/download/subctl-release-0.24/subctl-release-0.24-linux-amd64.tar.xz

# Helm charts (needs internet to the chart repo):
helm repo add submariner-latest https://submariner-io.github.io/submariner-charts/charts
helm repo update
helm pull submariner-latest/submariner-k8s-broker --version 0.24.0
helm pull submariner-latest/submariner-operator   --version 0.24.0
```

Transfer all four files to `prd-oi-bstn`, then install the binaries (no root needed):

```bash
tar -xzf helm-v3.16.3-linux-amd64.tar.gz
mkdir -p ~/bin && install -m 0755 linux-amd64/helm ~/bin/helm
tar -xJf subctl-release-0.24-linux-amd64.tar.xz    # if tar lacks -J: unxz first then tar -xf
install -m 0755 "$(find . -maxdepth 2 -name subctl -type f | head -1)" ~/bin/subctl
export PATH="$HOME/bin:$PATH"
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.bashrc
helm version && subctl version
```

---

## 2. Mirror images to Harbor

### 2.1 The exact image set (ground truth, not memory)
The Helm chart does NOT list operand images — the operator builds them at runtime from a fixed
name set in `pkg/names/names.go`. Key finding for 0.24.0: **there is NO `metrics-proxy` image**;
the operator maps metrics-proxy → `nettest`. The complete set is **8 images**:

```
quay.io/submariner/submariner-operator:0.24.0
quay.io/submariner/submariner-gateway:0.24.0
quay.io/submariner/submariner-route-agent:0.24.0
quay.io/submariner/submariner-globalnet:0.24.0
quay.io/submariner/lighthouse-agent:0.24.0
quay.io/submariner/lighthouse-coredns:0.24.0
quay.io/submariner/nettest:0.24.0
quay.io/submariner/subctl:0.24.0
```

### 2.2 Pull → carry → push into `harbor.ai.ecouncil.ae/submariner/`
Layout MUST be `harbor.ai.ecouncil.ae/submariner/<image>:0.24.0` so `submariner.images.repository` resolves.

```bash
# ON INTERNET HOST (force amd64 if pulling from Apple Silicon):
TAG=0.24.0
for i in submariner-operator submariner-gateway submariner-route-agent submariner-globalnet \
         lighthouse-agent lighthouse-coredns nettest subctl; do
  podman pull --arch amd64 quay.io/submariner/$i:$TAG
done
podman save --format oci-archive -o submariner-0.24.0.tar \
  quay.io/submariner/submariner-operator:$TAG quay.io/submariner/submariner-gateway:$TAG \
  quay.io/submariner/submariner-route-agent:$TAG quay.io/submariner/submariner-globalnet:$TAG \
  quay.io/submariner/lighthouse-agent:$TAG quay.io/submariner/lighthouse-coredns:$TAG \
  quay.io/submariner/nettest:$TAG quay.io/submariner/subctl:$TAG

# ON INSIDE HOST that can reach Harbor:
podman load -i submariner-0.24.0.tar
podman login harbor.ai.ecouncil.ae
for i in submariner-operator submariner-gateway submariner-route-agent submariner-globalnet \
         lighthouse-agent lighthouse-coredns nettest subctl; do
  podman tag  quay.io/submariner/$i:$TAG harbor.ai.ecouncil.ae/submariner/$i:$TAG
  podman push harbor.ai.ecouncil.ae/submariner/$i:$TAG
done
```

### 2.3 Verify the cluster can pull
```bash
kubectl run pulltest --image=harbor.ai.ecouncil.ae/submariner/nettest:0.24.0 \
  --restart=Never --command -- sleep 15
kubectl describe pod pulltest | grep -A5 -i events   # want: Successfully pulled image
kubectl delete pod pulltest --ignore-not-found
```

---

## 3. Clone the operator source (needed for the globalnet RBAC — see §7)

The Helm chart is missing the globalnet RBAC objects. You WILL need them (on both clusters).
Get the source now so the YAML is on hand:

```bash
# on an internet host, then carry the config/rbac dir across, OR clone if the bastion can reach github:
git clone --depth 1 --branch v0.24.0 https://github.com/submariner-io/submariner-operator
ls submariner-operator/config/rbac/submariner-globalnet/
#   service_account.yaml  role.yaml  role_binding.yaml  cluster_role.yaml  cluster_role_binding.yaml
```

---

## 4. Generate a PSK (used by BOTH clusters — save it)

WireGuard's cable driver derives its preshared key as `sha256(CE_IPSEC_PSK)`. **Both clusters
MUST use the identical PSK string**, or the WireGuard handshake is silently dropped at MAC
verification (this was root-cause #1 of the multi-hour outage). Generate once, use on both:

```bash
openssl rand -base64 48 | tr -d '\n'; echo
# EXAMPLE (use your own): JjzOfQTMcwbnDDHiVJC1bs+/Jyr56FsGlIkuaknrVy6jFjUVB4CJ1AShlfSsi0v2
```
Save this value. It goes in the operator install below AND in the Al Ain install.

---

## 5. Install the Broker (CRDs only — no images)

```bash
helm install submariner-k8s-broker ./submariner-k8s-broker-0.24.0.tgz \
  --namespace submariner-k8s-broker --create-namespace \
  --set globalnet=true
kubectl get crds | grep -iE 'submariner|multicluster.x-k8s.io'
kubectl -n submariner-k8s-broker get secrets   # expect submariner-k8s-broker-client-token
```

Extract broker credentials (needed by the operator, and later by Al Ain):
```bash
BROKER_NS=submariner-k8s-broker
SUBMARINER_BROKER_URL=$(kubectl -n default get endpoints kubernetes \
  -o jsonpath="{.subsets[0].addresses[0].ip}:{.subsets[0].ports[?(@.name=='https')].port}")
SUBMARINER_BROKER_CA=$(kubectl -n "$BROKER_NS" get secret submariner-k8s-broker-client-token \
  -o jsonpath="{.data.ca\.crt}")
SUBMARINER_BROKER_TOKEN=$(kubectl -n "$BROKER_NS" get secret submariner-k8s-broker-client-token \
  -o jsonpath="{.data.token}" | base64 --decode)
echo "URL=$SUBMARINER_BROKER_URL  CAlen=${#SUBMARINER_BROKER_CA}  TOKlen=${#SUBMARINER_BROKER_TOKEN}"
# expect URL=10.10.128.71:6443  CAlen≈760  TOKlen≈1013
```
> Set `BROKER_NS` on its own line. A run-on leaves it empty and `kubectl get secret -client-token`
> fails with `unknown shorthand flag: 'c'`.

---

## 6. Install the Operator (all discovered fixes applied)

```bash
helm install submariner-operator ./submariner-operator-0.24.0.tgz \
  --namespace submariner-operator --create-namespace \
  --set operator.image.repository=harbor.ai.ecouncil.ae/submariner/submariner-operator \
  --set operator.image.tag=0.24.0 \
  --set submariner.images.repository=harbor.ai.ecouncil.ae/submariner \
  --set submariner.images.tag=0.24.0 \
  --set submariner.clusterId=abudhabi \
  --set submariner.clusterCidr=10.42.0.0/16 \
  --set submariner.serviceCidr=10.43.0.0/16 \
  --set submariner.globalnet=true \
  --set submariner.globalCidr=242.0.0.0/24 \
  --set submariner.serviceDiscovery=true \
  --set submariner.natEnabled=false \
  --set submariner.cableDriver=wireguard \
  --set submariner.airGappedDeployment=true \
  --set submariner.ceIPSecPSK="<PSK-FROM-STEP-4>" \
  --set broker.namespace=submariner-k8s-broker \
  --set broker.globalnet=true \
  --set broker.server="${SUBMARINER_BROKER_URL}" \
  --set broker.token="${SUBMARINER_BROKER_TOKEN}" \
  --set broker.ca="${SUBMARINER_BROKER_CA}"

kubectl label node prd-oi-k8worker01 submariner.io/gateway=true
```

> **Critical `--set` keys** — each was a real failure the first time round:
> - `submariner.images.repository` — NESTED. A flat `submariner.repository` is silently ignored → operands pull from quay.
> - `broker.namespace=submariner-k8s-broker` — chart default is the placeholder `xyz`; without this the operator looks in a non-existent namespace and never joins.
> - `submariner.globalnet=true` — operator-side flag (separate from `broker.globalnet`); without it no globalnet/global CIDR.
> - `submariner.airGappedDeployment=true` — without it the gateway tries public-IP discovery over the internet and CrashLoops (`could not determine public IPv4`).
> - `submariner.ceIPSecPSK` — MUST be set and MUST match Al Ain (see §4). If Helm's `--set` doesn't stick, patch the CR (see §7).

---

## 7. Post-install fixes that may be required (apply if symptoms appear)

### 7a. air-gapped flag didn't take (gateway CrashLoop: `could not determine public IPv4`)
The `--set submariner.airGappedDeployment` sometimes doesn't propagate (the key isn't in the chart
`values.yaml`; it passes through to the CR but a `--set` can be dropped). Patch the CR directly:
```bash
kubectl -n submariner-operator get submariner submariner -o jsonpath='{.items[0].spec.airGappedDeployment}{"\n"}'
kubectl -n submariner-operator patch submariner submariner --type=merge -p '{"spec":{"airGappedDeployment":true}}'
kubectl -n submariner-operator rollout restart daemonset/submariner-gateway
```
The crash reason only appears in the **previous** container: `kubectl logs -l app=submariner-gateway --previous`.

### 7b. Operands still pulling from quay.io
```bash
kubectl -n submariner-operator get pods \
  -o jsonpath='{range .items[*]}{.spec.containers[*].image}{"\n"}{end}' | sort -u   # any quay.io = bad
helm upgrade submariner-operator ./submariner-operator-0.24.0.tgz --namespace submariner-operator --reuse-values \
  --set submariner.images.repository=harbor.ai.ecouncil.ae/submariner --set submariner.images.tag=0.24.0
kubectl -n submariner-operator rollout restart daemonset/submariner-gateway daemonset/submariner-routeagent daemonset/submariner-metrics-proxy
```

### 7c. Broker namespace stuck as `xyz` (no cluster/gateway registration despite valid token)
```bash
kubectl -n submariner-operator get submariner -o jsonpath='{.items[0].spec.brokerK8sRemoteNamespace}{"\n"}'  # if xyz:
helm upgrade submariner-operator ./submariner-operator-0.24.0.tgz --namespace submariner-operator --reuse-values \
  --set broker.namespace=submariner-k8s-broker
```

### 7d. **Globalnet pod won't schedule — `serviceaccount "submariner-globalnet" not found`**
**The Helm chart does NOT create the globalnet RBAC** (every other component has it; globalnet's is
missing). This blocks the globalnet pod, which means the `submariner` interface never gets its
global IP and cross-cluster health checks fail. **Do NOT wait for it to self-heal — it never will.**
Create all 5 objects from source (§3):
```bash
kubectl apply -f submariner-operator/config/rbac/submariner-globalnet/
# (service_account, role, role_binding, cluster_role, cluster_role_binding)
kubectl -n submariner-operator rollout restart ds submariner-globalnet
kubectl -n submariner-operator get pods -l app=submariner-globalnet -o wide   # should now be Running
```
> **Helm-persistence warning:** these RBAC objects are applied outside Helm. A future
> `helm upgrade` won't recreate them and could interfere. Re-apply after any upgrade, or add
> them to a chart supplement.

### 7e. **UFW blocking intra-cluster VXLAN (UDP 4800)** — only if the gateway node runs UFW
Submariner's route-agent VXLAN overlay uses **UDP 4800** (`vx-submariner`), NOT the standard 8472.
If the gateway node has UFW with `policy DROP`, non-gateway nodes can't reach the gateway and the
pinger fails ("more than 5 packets lost") even with a working tunnel. Check and fix on the gateway
node. **If you have SSH**, just run `ufw` directly. **If you don't** (air-gapped), use a privileged
hostPath pod + chroot:
```bash
# check (via a privileged pod pinned to the gateway node, host mounted at /host):
kubectl -n <privileged-ns> exec <hostpath-pod> -- sh -c 'chroot /host /usr/sbin/ufw status numbered'
# fix (writes PERSISTENT host iptables rules via the host's own ufw binary):
kubectl -n <privileged-ns> exec <hostpath-pod> -- sh -c \
  'chroot /host /usr/sbin/ufw allow from 10.10.128.0/24 to any port 4800 proto udp'
```
(See the Al Ain guide §8 for the full hostPath+chroot pod manifest and the technique.)
Abu Dhabi's gateway (`prd-oi-k8worker01`) — confirm whether UFW is active here too; if so, allow
4800 from the Al Ain and local subnets.

---

## 8. Verify Abu Dhabi is healthy

```bash
kubectl -n submariner-operator get pods -o wide                       # all Running, gateway 0 restarts
kubectl -n submariner-operator get pods \
  -o jsonpath='{range .items[*]}{.spec.containers[*].image}{"\n"}{end}' | sort -u   # all harbor, no quay
kubectl -n submariner-operator get gateways.submariner.io -o wide     # prd-oi-k8worker01 active
kubectl -n submariner-operator get submariner -o jsonpath='{.items[0].spec.airGappedDeployment}{"\n"}'  # true
kubectl -n submariner-k8s-broker get clusters.submariner.io           # abudhabi registered
subctl show all --context default
```
Correct pre-Al-Ain state: gateway `active` on `prd-oi-k8worker01` (wireguard, endpoint
`10.10.128.72`, global `242.0.0.0/24`), `abudhabi` at the broker, **no connections** (no peer yet).

---

## 9. Firewall (hand to the network team)

**Bidirectional** UDP between the two gateway IPs — a unidirectional rule does NOT work (WireGuard's
handshake reply must return; a stateful rule can also expire during the ~30s handshake gaps).

| Source | Destination | Port | Proto | Purpose |
|---|---|---|---|---|
| `10.34.104.19` ↔ `10.10.128.72` | (both ways) | **4500** | UDP | WireGuard tunnel data |
| `10.34.104.19` ↔ `10.10.128.72` | (both ways) | **4490** | UDP | NAT-traversal discovery |
| `10.34.104.19` → `10.10.128.71` | | **6443** | TCP | Broker (metadata) |

Plus **intra-cluster** on each side: UDP **4800** between local nodes (route-agent VXLAN) — this is
the UFW rule in §7e, enforced host-side, not usually a perimeter-firewall item.
**51820 is NOT used** (that's standalone WireGuard's port).

---

## 10. The three root causes (why this was hard) — reference

The multi-hour outage was **three independent bugs stacked**, each hiding the next; fixing one alone
left `subctl` still showing `error`:

1. **PSK mismatch** — Abu Dhabi had no PSK, Al Ain did → WireGuard silently dropped handshakes. Fix: identical `ceIPSecPSK` on both (§4).
2. **UFW blocking UDP 4800** — intra-cluster VXLAN dropped by host firewall → pinger failed. Fix: allow 4800 on the gateway node (§7e).
3. **Missing globalnet RBAC** — chart never created it → globalnet pod never scheduled → no global IP → health check failed. Fix: apply the 5 RBAC objects from source (§7d).

Diagnostic lessons: for a CrashLooper, `kubectl logs --previous` has the truth; trust the CRDs and
`ip -s link show submariner` counters (RX frozen while TX climbs = return path / crypto reject) over
pod status; `subctl diagnose all` explicitly flags the VXLAN(4800) and CNI issues.

---

## 11. Post-deployment hardening
- **Rotate the broker token** (exposed in logs during debugging): delete + recreate `submariner-k8s-broker-client-token`.
- **Persist out-of-Helm fixes**: the PSK, air-gapped flag, and globalnet RBAC were applied via patch/apply. Put PSK + air-gapped in your values file; re-apply globalnet RBAC after any `helm upgrade`.
- Clean up any credential temp files and debug pods.

---

# APPENDIX A — Full working command reference (Abu Dhabi side)

Abu Dhabi was mostly the "correct" cluster, but two of the three root causes touch it directly:
the **PSK must be set here** (it was empty — root cause #1), and its gateway node **may also have
UFW** blocking the tunnel/VXLAN ports. Full commands below.

## A.1 PSK — set on Abu Dhabi to match Al Ain (root cause #1)

The original Abu Dhabi install never set a PSK, so `ceIPSecPSK` was empty while Al Ain had one →
WireGuard silently dropped every handshake response. Confirm and fix:
```bash
# confirm current (was empty):
kubectl -n submariner-operator get submariner submariner -o jsonpath='{.spec.ceIPSecPSK}{"\n"}'

# set it to the SAME value used on Al Ain:
kubectl -n submariner-operator patch submariner submariner --type=merge \
  -p '{"spec":{"ceIPSecPSK":"JjzOfQTMcwbnDDHiVJC1bs+/Jyr56FsGlIkuaknrVy6jFjUVB4CJ1AShlfSsi0v2"}}'

# verify + restart gateway to re-read:
kubectl -n submariner-operator get submariner submariner -o jsonpath='{.spec.ceIPSecPSK}{"\n"}'
kubectl -n submariner-operator delete pod -l app=submariner-gateway
```
Going forward, bake `--set submariner.ceIPSecPSK="<PSK>"` into the install (§6) so it's never empty.

## A.2 UFW on the Abu Dhabi gateway node (if active)

If `prd-oi-k8worker01` runs UFW with default-DROP, it needs the same rules as Al Ain — inbound tunnel
ports from Al Ain, and intra-cluster VXLAN. No SSH? Use the same privileged hostPath + chroot pod
(swap the nodeSelector to the Abu Dhabi gateway and the image to the Abu Dhabi Harbor):
```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata: { name: ufw-config, namespace: kube-system }
spec:
  hostNetwork: true
  hostPID: true
  nodeSelector: { kubernetes.io/hostname: prd-oi-k8worker01 }
  tolerations: [{ operator: Exists }]
  containers:
  - name: ufw
    image: harbor.ai.ecouncil.ae/submariner/nettest:0.24.0
    command: ["sleep","infinity"]
    securityContext:
      privileged: true
      capabilities: { add: ["NET_ADMIN","NET_RAW","SYS_MODULE","SYS_CHROOT"] }
    volumeMounts: [{ name: host, mountPath: /host }]
  volumes: [{ name: host, hostPath: { path: /, type: Directory } }]
  restartPolicy: Never
EOF

# check:
kubectl -n kube-system exec ufw-config -- sh -c 'chroot /host /usr/sbin/ufw status verbose'

# allow tunnel ports inbound from the Al Ain gateway, and intra-cluster VXLAN:
kubectl -n kube-system exec ufw-config -- sh -c '
chroot /host /usr/sbin/ufw allow from 10.34.104.19 to any port 4500 proto udp comment "Submariner WireGuard (Al Ain)"
chroot /host /usr/sbin/ufw allow from 10.34.104.19 to any port 4490 proto udp comment "Submariner NAT discovery (Al Ain)"
chroot /host /usr/sbin/ufw allow from 10.10.128.0/24 to any port 4800 proto udp comment "Submariner intra-cluster VXLAN"
'
kubectl -n kube-system exec ufw-config -- sh -c 'chroot /host /usr/sbin/ufw status | grep -E "4500|4490|4800"'
kubectl -n kube-system delete pod ufw-config --ignore-not-found --force --grace-period=0
```

## A.3 Broker-side diagnostics used during the outage

Confirm both clusters are registered and their advertised endpoints/keys:
```bash
kubectl -n submariner-k8s-broker get clusters.submariner.io
kubectl -n submariner-k8s-broker get endpoints.submariner.io -o jsonpath='{range .items[*]}{.spec.cluster_id}{" key="}{.spec.backend_config.publicKey}{" ip="}{.spec.private_ip}{" ports="}{.spec.backend_config.udp-port}{"/"}{.spec.backend_config.natt-discovery-port}{"\n"}{end}'
```

Gateway health + connection status from the Abu Dhabi side:
```bash
kubectl -n submariner-operator get gateways.submariner.io -o wide
kubectl -n submariner-operator get submariner submariner -o jsonpath='{.status.gateways[0].connections[0].status}{"\n"}'
subctl show all --context default
```

Broker token length (for comparing against what Al Ain holds — must match exactly):
```bash
kubectl -n submariner-k8s-broker get secret submariner-k8s-broker-client-token \
  -o jsonpath='{.data.token}' | base64 -d | wc -c
```

## A.4 The `wg show` technique also works here

Same ConfigMap + chroot approach as the Al Ain guide §A.3, substituting `prd-oi-k8worker01` and the
Abu Dhabi Harbor image. Use it to confirm `latest handshake` appears once the PSK matches on both sides.
