# Submariner 0.24.0 — Shared Reference (Cluster-Agnostic Fixes)

Both cluster guides ([Abu Dhabi](submariner-ABUDHABI-guide.md) and [Al Ain](submariner-ALAIN-guide.md))
hit the same set of Submariner 0.24.0 bugs and air-gapped-deployment quirks. This file holds the
cluster-agnostic material that is identical on both sides so neither guide has to duplicate it.
Each cluster guide cross-references the relevant section here and provides only the cluster-specific
invocation (namespace, node name, Harbor host, context, etc).

---

## Table of Contents

1. [PSK (ceIPSecPSK)](#1-psk-ceipsecpck)
2. [Air-gapped deployment flag](#2-air-gapped-deployment-flag)
3. [Missing globalnet RBAC](#3-missing-globalnet-rbac)
4. [brokerK8sInsecure (TLS certificate mismatch)](#4-brokerk8sinsecure-tls-certificate-mismatch)
5. [UFW blocking intra-cluster VXLAN (UDP 4800)](#5-ufw-blocking-intra-cluster-vxlan-udp-4800)
6. [ServiceExport re-creation](#6-serviceexport-re-creation)
7. [Consolidated root-cause list](#7-consolidated-root-cause-list)

---

## 1. PSK (ceIPSecPSK)

Submariner's WireGuard cable driver uses a pre-shared key (`ceIPSecPSK`) for the tunnel handshake.
Both clusters MUST use the identical string. If one side has a PSK and the other does not (or they
differ), WireGuard silently drops handshake responses (MAC verification fails) and the connection
stays in `connecting`/`error` with no error message in the logs.

Generate once, share to both clusters:

```bash
subctl diagnose deploy-print-broker-psk   # or read from the broker secret
```

The PSK used in this deployment is:
`JjzOfQTMcwbnDDHiVJC1bs+/Jyr56FsGlIkuaknrVy6jFjUVB4CJ1AShlfSsi0v2`

Pass it to Helm on both clusters via `--set submariner.ceIPSecPSK="<value>"`. If `ceIPSecPSK` ends
up null (e.g. omitted or quoted wrong), the CR is rejected with
`spec.ceIPSecPSK ... must be of type string: "null"`.

> **Security note:** This PSK is a WireGuard shared secret, not a private key. Rotating it requires
> re-deploying both gateways. Treat it as sensitive; do not paste it through terminals that may
> inject whitespace (use the same double-base64 method as the broker credentials, or pass via Helm
> `--set` which handles it cleanly).

---

## 2. Air-gapped deployment flag

In an air-gapped environment the submariner-gateway pod CrashLoops with
`could not determine public IPv4` because it tries to reach an external IP detection service. The
fix is to set `airGappedDeployment=true` on the Submariner CR:

```bash
kubectl -n submariner-operator get submariner -o jsonpath='{.items[0].spec.airGappedDeployment}{"\n"}'
kubectl -n submariner-operator patch submariner submariner --type=merge \
  -p '{"spec":{"airGappedDeployment":true}}'
kubectl -n submariner-operator rollout restart daemonset/submariner-gateway
```

This can also be set at install time via `--set submariner.airGappedDeployment=true`. To make it
survive `helm upgrade`, add it to your Helm values file:

```yaml
submariner:
  airGappedDeployment: true
```

---

## 3. Missing globalnet RBAC

The Submariner 0.24.0 Helm chart **never creates the globalnet RBAC**. The globalnet daemonset
shows `DESIRED=1 CURRENT=0` and can't schedule, so the `submariner` interface never gets its global
IP and the health-check ping fails. This affects both clusters identically.

If you have the operator source cloned, the simplest fix:

```bash
kubectl apply -f submariner-operator/config/rbac/submariner-globalnet/
```

If you do NOT have the source dir, apply the full 5-object manifest below. These are the verbatim
objects from the source tree:

```yaml
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
```

After applying, restart the daemonset and verify:

```bash
kubectl -n submariner-operator rollout restart ds submariner-globalnet
kubectl -n submariner-operator get pods -l app=submariner-globalnet -o wide   # Running
```

> **Persist this fix.** A future `helm upgrade --reuse-values` will NOT recreate these objects.
> Re-apply them after any upgrade.

---

## 4. brokerK8sInsecure (TLS certificate mismatch)

If the broker API server's TLS certificate does not match the IP the lighthouse agent connects to
(common when using an IP address instead of a hostname), the agent gets
`x509: certificate signed by unknown authority` or `x509: hostname mismatch` errors. The fix is to
set `brokerK8sInsecure=true` on the Submariner CR:

```bash
kubectl -n submariner-operator patch submariner submariner --type=merge \
  -p '{"spec":{"brokerK8sInsecure":true}}'
# verify it propagated to the ServiceDiscovery CR:
kubectl -n submariner-operator get servicediscovery service-discovery \
  -o jsonpath='{.spec.brokerK8sInsecure}{"\n"}'   # true
# restart lighthouse agent to pick up the change:
kubectl -n submariner-operator rollout restart deploy/submariner-lighthouse-agent
```

The Submariner controller's `serviceDiscoveryReconciler` rebuilds the ServiceDiscovery spec from
`submariner.spec` on every reconcile, so set `brokerK8sInsecure` on the **Submariner CR**, not the
ServiceDiscovery CR (fields set directly on the SD CR get overwritten). To survive `helm upgrade`,
add it to your Helm values file:

```yaml
submariner:
  spec:
    brokerK8sInsecure: true
```

---

## 5. UFW blocking intra-cluster VXLAN (UDP 4800)

The route-agent's VXLAN overlay (`vx-submariner`) uses **UDP 4800** (not 8472). If the gateway node
runs UFW with `policy DROP`, VXLAN packets from other cluster nodes are dropped before reaching the
gateway, so `vx-submariner` RX stays at 0 and the pinger fails with "more than 5 packets lost".

The fix is to allow UDP 4800 on the gateway node's UFW. Since the gateway node may not be reachable
via SSH, use a privileged hostPath pod + chroot to run the host's own `ufw` (this writes persistent
host rules in `/etc/ufw/user.rules` that survive reboot).

### Generic privileged debug pod manifest

Substitute `<GATEWAY_NODE>`, `<DEBUG_NAMESPACE>`, and `<HARBOR_HOST>` for your cluster:

```yaml
apiVersion: v1
kind: Pod
metadata: { name: host-debug, namespace: <DEBUG_NAMESPACE> }
spec:
  hostNetwork: true
  hostPID: true
  nodeSelector: { kubernetes.io/hostname: <GATEWAY_NODE> }
  tolerations: [{ operator: Exists }]
  containers:
  - name: debug
    image: <HARBOR_HOST>/submariner/nettest:0.24.0
    command: ["sleep","600"]
    securityContext: { privileged: true }
    volumeMounts: [{ name: host, mountPath: /host }]
  volumes: [{ name: host, hostPath: { path: /, type: Directory } }]
  restartPolicy: Never
```

### Check + fix UFW on the host via chroot

```bash
kubectl -n <DEBUG_NAMESPACE> exec host-debug -- sh -c 'chroot /host /usr/sbin/ufw status numbered'
kubectl -n <DEBUG_NAMESPACE> exec host-debug -- sh -c \
  'chroot /host /usr/sbin/ufw allow from <NODE_CIDR> to any port 4800 proto udp'
kubectl -n <DEBUG_NAMESPACE> exec host-debug -- sh -c \
  'chroot /host /usr/sbin/ufw allow to any port 4800 proto udp'
kubectl -n <DEBUG_NAMESPACE> exec host-debug -- sh -c \
  'chroot /host /usr/sbin/ufw status numbered | grep 4800'
```

Only the **gateway node** needs 4800 inbound for basic tunnel health (non-gateway nodes only send
*to* it). If you add a second gateway for HA later, that node needs the same rule. Note that even
with 4800 open on all nodes, non-gateway pods may still be unable to reach globalnet IPs due to
CNI-specific datapath limitations; see the Al Ain guide §12.1 for the Cilium BPF limitation
details.

### Verify the fix

```bash
RA=$(kubectl -n submariner-operator get pod -l app=submariner-routeagent \
  --field-selector spec.nodeName=<GATEWAY_NODE> -o name | head -1)
kubectl -n submariner-operator exec "$RA" -c submariner-routeagent -- \
  ip -s link show vx-submariner | grep -A2 'RX:\|TX:'   # RX should now climb
kubectl -n submariner-operator delete pod -l app=submariner-gateway --force --grace-period=0
```

---

## 6. ServiceExport re-creation

If the ServiceExport is deleted (or never created), the globalnet daemon won't allocate a global
ingress IP and cross-cluster DNS resolution fails. This applies to any service you want to expose
cross-cluster, on whichever cluster the service lives.

Check and re-create:

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
# verify the globalnet IP was allocated (on the exporting cluster):
kubectl get globalingressip -A | grep <service-name>
# verify from the remote cluster:
kubectl get serviceimport -A | grep <service-name>
kubectl -n <DEBUG_NAMESPACE> exec <debug-pod> -- \
  nslookup <service-name>.<namespace>.svc.clusterset.local
```

---

## 7. Consolidated root-cause list

Six independent bugs, stacked. Each hid the next, so fixing one alone still showed `error`:

1. **PSK mismatch** — one cluster had a PSK, the other did not. WireGuard silently dropped
   handshake responses (MAC verify fails). Fix: identical `ceIPSecPSK` on both clusters
   (see [§1](#1-psk-ceipsecpck)).

2. **UFW blocking UDP 4800** — intra-cluster VXLAN dropped on the gateway node, so the pinger
   failed. Fix: `ufw allow ... 4800/udp` on the gateway node via chroot
   (see [§5](#5-ufw-blocking-intra-cluster-vxlan-udp-4800)).

3. **Missing globalnet RBAC** — the chart never created it, so the globalnet pod never scheduled
   and the `submariner` interface had no global IP. Health check failed. Fix: apply the 5 RBAC
   objects from source (see [§3](#3-missing-globalnet-rbac)).

4. **TLS certificate mismatch** — broker API cert didn't match the IP, so the lighthouse agent got
   x509 errors. Fix: `brokerK8sInsecure=true` on the Submariner CR
   (see [§4](#4-brokerk8sinsecure-tls-certificate-mismatch)).

5. **ServiceExport lost** — no globalnet IP allocated, cross-cluster DNS failed. Fix: re-create the
   ServiceExport on the exporting cluster (see [§6](#6-serviceexport-re-creation)).

6. **Datapath-specific cross-cluster drop** — on Abu Dhabi (Canal/Calico), Calico dropped
   cross-cluster FORWARD traffic; fix: Calico GlobalNetworkPolicy `allow-submariner-cross-cluster`.
   On Al Ain (Cilium BPF kube-proxy-replacement), non-gateway pods cannot reach globalnet IPs at
   all; fix: run cross-cluster services on the gateway node. See each cluster guide for the
   cluster-specific fix.

Empirical lessons that cracked it: tcpdump proved packets *were* arriving (ruled out "firewall
return-path"); `wg show` (via chroot) proved the handshake state; reading the operator RBAC from
source revealed the chart gap; checking UFW on the actual host (not the pod) found the 4800 block.
`subctl diagnose all` independently flagged the VXLAN(4800) and CNI issues. Ground-truth inspection
beat reasoning-from-symptoms every time.
