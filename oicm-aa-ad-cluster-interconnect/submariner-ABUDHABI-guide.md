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

See `submariner-SHARED-reference.md` §1 for why both clusters must share the identical PSK.
Generate once, use on both:

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
See `submariner-SHARED-reference.md` §2 for why the `--set` flag can be silently dropped.
Patch the CR directly:
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
See `submariner-SHARED-reference.md` §3 for why the chart omits this RBAC and the persistence
warning. Create all 5 objects from source (§3):
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
See `submariner-SHARED-reference.md` §5 for why UDP 4800 matters and the privileged hostPath pod
+ chroot technique. Check and fix on the Abu Dhabi gateway node:
```bash
# check (via a privileged pod pinned to the gateway node, host mounted at /host):
kubectl -n <privileged-ns> exec <hostpath-pod> -- sh -c 'chroot /host /usr/sbin/ufw status numbered'
# fix (writes PERSISTENT host iptables rules via the host's own ufw binary):
kubectl -n <privileged-ns> exec <hostpath-pod> -- sh -c \
  'chroot /host /usr/sbin/ufw allow from 10.10.128.0/24 to any port 4800 proto udp'
```
Abu Dhabi's gateway (`prd-oi-k8worker01`): confirm whether UFW is active here too; if so, allow
4800 from the Al Ain and local subnets.

### 7f. **Calico dropping cross-cluster traffic (data-plane failure)** — REQUIRED for Canal/Calico clusters
When Submariner is healthy (tunnel up, ServiceImport synced, DNS resolving) but `curl` to a
globalnet IP times out, the root cause is Calico's default FORWARD policy. Packets arrive via
flannel.1 with a remote-cluster globalnet source IP (e.g. `242.0.1.1` from Al Ain), and Calico's
`cali-FORWARD` chain drops them because the source is not in the local pod CIDR (`10.42.0.0/16`)
and there is no explicit allow policy. The packet flow is: Al Ain sends to `242.0.0.253:8080` via
WireGuard, the Abu Dhabi gateway DNATs to `10.42.6.80:8080` via kube-proxy, flannel.1 delivers the
packet to the pod's node, but Calico drops it at the FORWARD chain before it reaches the pod.

**Do NOT work around this with manual `iptables -I` rules.** A raw iptables rule is fragile: it
must be inserted at position 1 (before `cali-FORWARD`), it won't survive a reboot, and Calico can
overwrite it during reconciliation. Use a Calico GlobalNetworkPolicy instead, which is managed by
the Calico controller, applies to all nodes automatically, and persists across reboots:
```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: crd.projectcalico.org/v1
kind: GlobalNetworkPolicy
metadata:
  name: allow-submariner-cross-cluster
spec:
  order: 100
  selector: all()
  types:
  - Ingress
  ingress:
  - action: Allow
    source:
      nets:
      - 242.0.0.0/24   # Abu Dhabi globalnet (for symmetric return traffic)
      - 242.0.1.0/24   # Al Ain globalnet (remote cluster)
EOF

# verify:
kubectl get globalnetworkpolicy -A
```
> **Why both CIDRs:** The policy allows ingress from both clusters' globalnet ranges. Al Ain's
> `242.0.1.0/24` is the remote source that needs to reach Abu Dhabi pods. Abu Dhabi's own
> `242.0.0.0/24` is needed for return traffic from the gateway back to pods during DNAT.
>
> **Verification:** after applying, `curl http://242.0.0.253:8080/v1/models` from Al Ain should
> return HTTP 200. Run 5 rapid curls to confirm no alternating/unstable behavior.

### 7g. **`brokerK8sInsecure=true` — TLS certificate hostname mismatch**
See `submariner-SHARED-reference.md` §4 for why this TLS mismatch happens. Patch the Submariner CR
on the consuming cluster (Al Ain in this case, but documented here for completeness):
```bash
kubectl -n submariner-operator patch submariner submariner --type=merge \
  -p '{"spec":{"brokerK8sInsecure":true}}'
# restart the lighthouse agent to pick up the change:
kubectl -n submariner-operator delete pod -l app=submariner-lighthouse-agent
```
> This skips TLS verification for broker API calls only. It is acceptable in air-gapped
> environments where the broker IP is reached via firewall NAT and the cert was issued for
> a different hostname.

### 7h. **ServiceExport disappearing — globalnet IP not allocated**
See `submariner-SHARED-reference.md` §6 for why a missing ServiceExport prevents globalnet IP
allocation. Re-create it:
```bash
kubectl apply -f - <<'EOF'
apiVersion: multicluster.x-k8s.io/v1alpha1
kind: ServiceExport
metadata:
  name: s-766b1720-f516-4077-b22c-6ce97c045470
  namespace: adeo
EOF

# verify the globalnet IP was allocated and the internal service was created:
kubectl get globalingressips.submariner.io -A
kubectl -n adeo get svc   # should show a submariner-* service with EXTERNAL-IP 242.0.0.253
```
> The internal service name is auto-generated (e.g. `submariner-adh66xzl7h2o7p723odni2axzs2heyu3`).
> It has the globalnet IP as its ExternalIP and the same selector as the original service, so
> kube-proxy's `KUBE-SERVICES` chain DNATs `242.0.0.253:8080` to the pod IP.

### 7i. **Auto-export all services in the `adeo` namespace (CronJob)**

Submariner 0.24.0 has no built-in mechanism to auto-export every service in a namespace.
`ServiceExport` objects must be created manually per-service, and if a service is deleted the
orphaned export lingers. This CronJob runs every 1 minute to:
1. Create a `ServiceExport` for every service in `adeo` that doesn't have one yet
2. Delete any `ServiceExport` whose backing service no longer exists

The manifest is at `oicm-aa-ad-cluster-interconnect/auto-export-adeo-services.yaml`. It deploys
a ServiceAccount + Role + RoleBinding (scoped to the `adeo` namespace) and the CronJob itself.

**How it works**: the nettest image doesn't ship `kubectl`, so the pod mounts the host filesystem
at `/host` and calls `/host/usr/bin/kubectl` directly (verified on RKE2 nodes at that path). Auth
uses the in-cluster ServiceAccount token (auto-mounted at
`/var/run/secrets/kubernetes.io/serviceaccount/`), so no kubeconfig or ConfigMap is needed.

**Deploy**:
```bash
kubectl apply -f auto-export-adeo-services.yaml
```

**Trigger a manual sync**:
```bash
kubectl -n submariner-operator create job --from=cronjob/auto-export-adeo-services manual-sync-1
kubectl -n submariner-operator logs job/manual-sync-1
```

**Verify**:
```bash
kubectl -n adeo get serviceexports          # every service should have one
kubectl get serviceimports -A               # visible from the peer cluster (Al Ain)
```

**Customize the namespace**: change the `NAMESPACE=adeo` variable in the CronJob command, and
update the Role/RoleBinding namespace to match.

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

## 10. The root causes (why this was hard) — reference

The outage was **multiple independent bugs stacked**, each hiding the next; fixing one alone
left `subctl` still showing `error` or cross-cluster traffic still failing:

1. **PSK mismatch** — Abu Dhabi had no PSK, Al Ain did → WireGuard silently dropped handshakes. Fix: identical `ceIPSecPSK` on both (§4).
2. **UFW blocking UDP 4800** — intra-cluster VXLAN dropped by host firewall → pinger failed. Fix: allow 4800 on the gateway node (§7e).
3. **Missing globalnet RBAC** — chart never created it → globalnet pod never scheduled → no global IP → health check failed. Fix: apply the 5 RBAC objects from source (§7d).
4. **TLS certificate mismatch** — broker API reached via firewall NAT IP, cert issued for different hostname → Lighthouse agent couldn't sync ServiceImports. Fix: `brokerK8sInsecure=true` on the Submariner CR (§7g).
5. **ServiceExport lost** — globalnet daemon had no ServiceExport to process → no globalnet IP allocated → no DNAT rule → packets routed out physical interface. Fix: re-create the ServiceExport (§7h).
6. **Calico dropping cross-cluster traffic** — packets arrived via flannel.1 with remote globalnet source IP, Calico's default FORWARD policy dropped them before they reached the pod. Fix: Calico GlobalNetworkPolicy allowing globalnet CIDRs (§7f). This was the final data-plane blocker.

Diagnostic lessons: for a CrashLooper, `kubectl logs --previous` has the truth; trust the CRDs and
`ip -s link show submariner` counters (RX frozen while TX climbs = return path / crypto reject) over
pod status; `subctl diagnose all` explicitly flags the VXLAN(4800) and CNI issues. For data-plane
issues (tunnel up, DNS resolves, but curl times out), use `tcpdump -i any -n "port 8080 and host
<globalnet-ip>"` on both the gateway and the pod's node simultaneously to trace where packets die.
If packets arrive on `flannel.1` but never appear on the pod's `cali` interface, Calico is dropping
them in the FORWARD chain.

---

## 11. Post-deployment hardening
- **Rotate the broker token** (exposed in logs during debugging): delete + recreate `submariner-k8s-broker-client-token`.
- **Persist out-of-Helm fixes**: the PSK, air-gapped flag, and globalnet RBAC were applied via patch/apply. Put PSK + air-gapped in your values file; re-apply globalnet RBAC after any `helm upgrade`.
- **Calico GlobalNetworkPolicy**: the `allow-submariner-cross-cluster` policy (§7f) is applied
  outside Helm. Re-apply after any Calico upgrade or cluster rebuild. Add it to a GitOps manifest
  set for persistence.
- **Auto-export CronJob** (§7i): runs every 1 minute to ensure all `adeo` services have
  `ServiceExport` objects and cleans up orphans. Apply the manifest from
  `auto-export-adeo-services.yaml` after any cluster rebuild.
- Clean up any credential temp files and debug pods.

---

# APPENDIX A — Full working command reference (Abu Dhabi side)

Abu Dhabi was mostly the "correct" cluster, but several of the root causes touch it directly:
the **PSK must be set here** (it was empty — root cause #1), its gateway node **may also have
UFW** blocking the tunnel/VXLAN ports, the **Calico policy** must be applied for data-plane
traffic, and the **ServiceExport** must exist for the globalnet IP to be allocated. Full commands below.

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

## A.5 Calico GlobalNetworkPolicy — allow Submariner cross-cluster traffic (root cause #6)

This is the fix that enabled cross-cluster data-plane traffic. Without it, packets arrive at the
pod's node via flannel.1 but Calico drops them in the FORWARD chain because the source IP
(`242.0.1.1` from Al Ain) is not in the local pod CIDR. Apply the policy once; Calico distributes
it to all nodes automatically:
```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: crd.projectcalico.org/v1
kind: GlobalNetworkPolicy
metadata:
  name: allow-submariner-cross-cluster
spec:
  order: 100
  selector: all()
  types:
  - Ingress
  ingress:
  - action: Allow
    source:
      nets:
      - 242.0.0.0/24   # Abu Dhabi globalnet
      - 242.0.1.0/24   # Al Ain globalnet
EOF

# verify:
kubectl get globalnetworkpolicy -A
```

### Verifying the data-plane end-to-end
After applying the policy, verify from the Al Ain side (from a pod or node that can reach the
WireGuard tunnel):
```bash
# globalnet IP (Submariner allocated):
curl -sv --connect-timeout 10 http://242.0.0.253:8080/v1/models

# DNS-based (from a pod using cluster DNS):
curl -sv --connect-timeout 10 \
  http://s-766b1720-f516-4077-b22c-6ce97c045470.adeo.svc.clusterset.local:8080/v1/models

# stability check (5 rapid curls, all should return 200):
for i in 1 2 3 4 5; do
  curl -s -o /dev/null -w "%{http_code}\n" --connect-timeout 5 http://242.0.0.253:8080/v1/models
done
```

### Data-plane debugging commands (if curl still times out)
Trace the packet flow across both Abu Dhabi nodes simultaneously to find where packets die:
```bash
# 1. On the Abu Dhabi gateway node, confirm packets arrive via WireGuard and get DNATed:
tcpdump -i any -n -c 20 "host 242.0.0.253 and port 8080"
#    expect: "submariner In IP 242.0.1.1.xxxx > 242.0.0.253.8080: Flags [S]"
#    then:   "flannel.1 Out IP 242.0.1.1.xxxx > 10.42.6.80.8080: Flags [S]"  (DNATed)

# 2. On the pod's node, confirm packets arrive via flannel.1:
tcpdump -i any -n -c 20 "port 8080 and host 242.0.1.1"
#    expect: "flannel.1 In IP 242.0.1.1.xxxx > 10.42.6.80.8080: Flags [S]"
#    if nothing arrives, the gateway's DNAT or route is broken

# 3. On the pod's node, check if packets reach the pod's cali interface:
tcpdump -i <cali-interface> -n -c 20 "host 242.0.1.1"
#    if 0 packets, Calico is dropping them in FORWARD. Apply the GlobalNetworkPolicy (above).

# 4. Check Calico FORWARD chain for drops:
iptables -L FORWARD -n -v | head -10
iptables -L cali-FORWARD -n -v | head -20
```

## A.6 ServiceExport — re-create if globalnet IP is not allocated (root cause #5)

If `kubectl get globalingressips.submariner.io -A` returns no resources, the ServiceExport was
lost and the globalnet daemon has nothing to process. Without it, no internal service with the
globalnet ExternalIP is created, so kube-proxy has no DNAT rule for `242.0.0.253`:
```bash
# check current state:
kubectl get serviceexport -A
kubectl get globalingressips.submariner.io -A
kubectl -n adeo get svc   # look for a submariner-* service with EXTERNAL-IP 242.0.0.253

# if missing, re-create:
kubectl apply -f - <<'EOF'
apiVersion: multicluster.x-k8s.io/v1alpha1
kind: ServiceExport
metadata:
  name: s-766b1720-f516-4077-b22c-6ce97c045470
  namespace: adeo
EOF

# verify (within a few seconds):
kubectl get globalingressips.submariner.io -A
kubectl -n adeo get svc   # should now show submariner-* with EXTERNAL-IP 242.0.0.253
```

## A.7 `brokerK8sInsecure=true` — TLS cert mismatch on broker API (root cause #4)

If the Lighthouse agent can't sync ServiceImports due to TLS errors, the broker API is being
reached via a different IP than the cert was issued for (firewall NAT). Skip TLS verification
for broker calls:
```bash
# check current value:
kubectl -n submariner-operator get submariner submariner -o jsonpath='{.spec.brokerK8sInsecure}{"\n"}'

# patch:
kubectl -n submariner-operator patch submariner submariner --type=merge \
  -p '{"spec":{"brokerK8sInsecure":true}}'

# restart lighthouse agent:
kubectl -n submariner-operator delete pod -l app=submariner-lighthouse-agent

# verify ServiceImports are syncing:
kubectl get serviceimport -A
```

## A.8 Connecting to the Abu Dhabi cluster from a remote node

Only specific remote nodes have firewall rules allowing TCP 6443 to the Abu Dhabi API server
(`10.10.128.71:6443`). For example, in the Al Ain cluster, only the gateway node `adeo-gpu-03`
(`10.34.104.19`) can reach it; `aitdev00` and other Al Ain nodes cannot. This section documents how
to run `kubectl` against Abu Dhabi from such a remote node when you do not have direct SSH access to
the Abu Dhabi bastion.

### Prerequisites

- A remote node with a firewall rule allowing TCP 6443 to `10.10.128.71` (in the Al Ain case, this
  is `adeo-gpu-03`, pinned via the lighthouse `nodeSelector` in the Al Ain guide §7e)
- The Abu Dhabi kubeconfig exported from a machine that has it (e.g. the Al Ain bastion `aitdev00`,
  which received it during the broker credential transfer in Al Ain guide §5)
- A privileged debug pod on the remote node (the remote node's host `kubectl` binary is used via
  chroot, since the submariner nettest image lacks `kubectl`)

### Step 1 — Export the Abu Dhabi kubeconfig

The kubeconfig must point at `10.10.128.71:6443` (the broker API server, which is the only Abu
Dhabi API server reachable through the firewall). If it points at a different server IP (e.g.
`10.10.128.75`), fix it:
```bash
# on the bastion that has the kubeconfig (e.g. aitdev00):
sed 's|10.10.128.75:6443|10.10.128.71:6443|g' /tmp/abudhabi-kubeconfig > /tmp/abudhabi-kubeconfig-fixed
grep server /tmp/abudhabi-kubeconfig-fixed   # must show 10.10.128.71:6443
```

### Step 2 — Ship the kubeconfig into a pod on the remote node via ConfigMap

The nettest image lacks `tar` so `kubectl cp` doesn't work. Use a ConfigMap instead. The example
below uses the Al Ain cluster's namespace and node; substitute for your remote cluster:
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

### Step 3 — Run kubectl against Abu Dhabi via the host's binary

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

### Step 4 — Clean up when done
```bash
kubectl -n oik8s-cilium-system delete pod ad-debug --ignore-not-found --force --grace-period=0
kubectl -n oik8s-cilium-system delete configmap abudhabi-kubeconfig --ignore-not-found
rm -f /tmp/abudhabi-kubeconfig-fixed
# /tmp/abudhabi-kubeconfig remains on the remote host (contains client cert/key; delete it if concerned):
#   kubectl -n oik8s-cilium-system exec ad-debug -- chroot /host sh -c 'rm -f /tmp/abudhabi-kubeconfig'
```

> **Why not `kubectl cp`?** The nettest image doesn't include `tar`, which `kubectl cp` requires.
> The ConfigMap approach avoids this entirely. If you have an image with `kubectl` built in
> (e.g. `bitnami/kubectl`), you can skip the chroot and run directly in the container.
>
> **Why `10.10.128.71` and not `10.10.128.75`?** The firewall rule only allows the remote gateway
> node to reach `10.10.128.71:6443`. Other Abu Dhabi API server IPs (`.72`-`.76`) are not reachable
> from the remote cluster. The kubeconfig may list a different server; always fix it to `.71`.

### Reference — Abu Dhabi kubeconfig template

The kubeconfig uses client certificate/key auth (not a bearer token). The certificate is tied to
the Abu Dhabi cluster's CA. Re-export from an Abu Dhabi master node if the cert expires. The
full kubeconfig with real credentials is below; this is a private repo so the guide is self-contained:

```yaml
apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSUJlVENDQVIrZ0F3SUJBZ0lCQURBS0JnZ3Foa2pPUFFRREFqQWtNU0l3SUFZRFZRUUREQmx5YTJVeUxYTmwKY25abGNpMWpZVUF4TnpVNU1UVXpPREkwTUI0WERUSTFNRGt5T1RFek5UQXlORm9YRFRNMU1Ea3lOekV6TlRBeQpORm93SkRFaU1DQUdBMVVFQXd3WmNtdGxNaTF6WlhKMlpYSXRZMkZBTVRjMU9URTFNemd5TkRCWk1CTUdCeXFHClNNNDlBZ0VHQ0NxR1NNNDlBd0VIQTBJQUJPWXJUMUhUSFpOT2xndHRVRDV2L2EwYWNQOUVWWFdjcWFxQlc2MnEKT3JzcDg4NzJ5UGRxbEk0amlkd3dNTUNmWEFjTFVSYzBQMjAvZWdZZEF5YzRpb1dqUWpCQU1BNEdBMVVkRHdFQgovd1FFQXdJQ3BEQVBCZ05WSFJNQkFmOEVCVEFEQVFIL01CMEdBMVVkRGdRV0JCVGNxRVhCZGwxOW5tdktraWhPCnluRERMZHNrelRBS0JnZ3Foa2pPUFFRREFnTklBREJGQWlCaFVvdFhvdTJpQk9pL0lkNkdWTWNBU2FjcC9LazIKYkFyOThPL2RtOHdIT0FJaEFKSDZRTE5yb3BGNERkUENRNGFodDBSVGN5aTZwc3MxUHdjWDd4YWtXYk9yCi0tLS0tRU5EIENFUlRJRklDQVRFLS0tLS0K
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
    client-certificate-data: LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSUJrekNDQVRpZ0F3SUJBZ0lJSnZZNHc0L0txLzR3Q2dZSUtvWkl6ajBFQXdJd0pERWlNQ0FHQTFVRUF3d1oKY210bE1pMWpiR2xsYm5RdFkyRkFNVGMxT1RFMU16Z3lOREFlRncweU5UQTVNamt4TXpVd01qUmFGdzB5TmpBNQpNamt4TXpVd01qUmFNREF4RnpBVkJnTlZCQW9URG5ONWMzUmxiVHB0WVhOMFpYSnpNUlV3RXdZRFZRUURFd3h6CmVYTjBaVzA2WVdSdGFXNHdXVEFUQmdjcWhrak9QUUlCQmdncWhrak9QUU1CQndOQ0FBVEMyaXhyeklVMEhKMHgKVCtWSVhOWTJyOUJXN3hJRTdSUnlxeGoySHNrKzdvdGM0MkpKYk92djdmREpEYVJlNkRvR1k3WksvaVl3a2ZBZQpnQTFnTU5mOW8wZ3dSakFPQmdOVkhROEJBZjhFQkFNQ0JhQXdFd1lEVlIwbEJBd3dDZ1lJS3dZQkJRVUhBd0l3Ckh3WURWUjBqQkJnd0ZvQVVhSDY5Q0tOTXIzcXVYU1A2UDRHR2FVQVNVZ0F3Q2dZSUtvWkl6ajBFQXdJRFNRQXcKUmdJaEFPNXpDVk9PcFFxcnhYSXhTTFZrU1REVndzTEtEcm42NDkyazl0aFdwc2FWQWlFQTEvR0IxZVhHV0w3awprMmwxU1hEc3hTenl2cXN6MmhMM3ByMm9pTHBPTTU4PQotLS0tLUVORCBDRVJUSUZJQ0FURS0tLS0tCi0tLS0tQkVHSU4gQ0VSVElGSUNBVEUtLS0tLQpNSUlCZURDQ0FSK2dBd0lCQWdJQkFEQUtCZ2dxaGtqT1BRUURBakFrTVNJd0lBWURWUVFEREJseWEyVXlMV05zCmFXVnVkQzFqWVVBeE56VTVNVFV6T0RJME1CNFhEVEkxTURreU9URXpOVEF5TkZvWERUTTFNRGt5TnpFek5UQXkKTkZvd0pERWlNQ0FHQTFVRUF3d1pjbXRsTWkxamJHbGxiblF0WTJGQU1UYzFPVEUxTXpneU5EQlpNQk1HQnlxRwpTTTQ5QWdFR0NDcUdTTTQ5QXdFSEEwSUFCSXo2WHhwdWhuY0gyYUd6V29Dc3JvM0puK21XQXFhalh0VmNoY1I2CjljTUVZSmhKOUN1cW9VVUlXV1VYUlVhK2NQdE1RcXVoemlEa2N3RzZqTEhvZFhLalFqQkFNQTRHQTFVZER3RUIKL3dRRUF3SUNwREFQQmdOVkhSTUJBZjhFQlRBREFRSC9NQjBHQTFVZERnUVdCQlJvZnIwSW8weXZlcTVkSS9vLwpnWVpwUUJKU0FEQUtCZ2dxaGtqT1BRUURBZ05IQURCRUFpQVZjQ3htMFNKZEpUMXYrMHNzbzNEWDZXeVlBdXplCktOcmtvWXpDNXc0bjNBSWdVOGhCR0o3VlBLa2NKZ2hZVWV1N09NNDJLYWUxd1d2VjhpZUY1YzNwV1MwPQotLS0tLUVORCBDRVJUSUZJQ0FURS0tLS0tCg==
    client-key-data: LS0tLS1CRUdJTiBFQyBQUklWQVRFIEtFWS0tLS0tCk1IY0NBUUVFSUx5MEtXa2V2YTBXOTZZNlZEZGMyN3NVeG1kbnJtZW1MOWpWQ3dpazhWTzlvQW9HQ0NxR1NNNDkKQXdFSG9VUURRZ0FFd3Rvc2E4eUZOQnlkTVUvbFNGeldOcS9RVnU4U0JPMFVjcXNZOWg3SlB1NkxYT05pU1d6cgo3KzN3eVEya1h1ZzZCbU8yU3Y0bU1KSHdIb0FOWUREWC9RPT0KLS0tLS1FTkQgRUMgUFJJVkFURSBLRVktLS0tLQo=
```
