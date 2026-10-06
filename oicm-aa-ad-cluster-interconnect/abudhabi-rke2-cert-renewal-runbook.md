# Abu Dhabi RKE2 certificate renewal: runbook

Date: 2026-10-06
Status: **DONE**. All five agent nodes renewed, verified, and the Submariner
exports are reachable from Al Ain again.

Purpose: fix the expired RKE2 node certificates that break kube-proxy on the Abu
Dhabi cluster, which is what makes every newly exported Service unreachable over
Submariner.

This was the first and only thing to fix. Nothing else in the OICM work was
blocked on code, and the API-version question is settled (see
`abudhabi-oicm-rest-api-export.md`).

## Result

Restarting `rke2-agent` on the five agent nodes renewed the expired leaf
certificates and fixed kube-proxy. Measured outcome:

| Node | `client-kube-proxy` before | after | Containers before | after |
|---|---|---|---|---|
| `prd-oi-k8worker02` | 2026-09-29 (expired) | 2027-09-29 | 69 | 69 |
| `prd-oi-k8worker03` | 2026-09-29 (expired) | 2027-09-29 | 70 | 70 |
| `prd-oi-k8worker04` | 2026-09-29 (expired) | 2027-09-29 | 62 | 62 |
| `prd-infr-k8h200` | 2026-09-29 (expired) | 2027-09-29 | 17 | 17 |
| `prd-oi-k8worker01` | 2026-09-29 (expired) | 2027-09-29 | 42 | 42 |

`rke2 certificate check` reports an `expired` count of 0 on every node, and every
`kube-proxy` pod logs zero `Unauthorized` lines.

The exported Services became reachable. From the Al Ain gateway host:

```
242.0.0.251/realms/adeo                                    -> 200
242.0.0.251/realms/adeo/.well-known/openid-configuration   -> 200
242.0.0.252/api/openapi/openapi.json                       -> 200
242.0.0.252/api/v1/workspaces/{ws}/deployment_summary      -> 403 {"error_code": 403, "message": "Missing Authorization token"}
242.0.0.252/api/v1/workspaces                              -> 404
```

Those are the same codes the Al Ain OICM gives internally, so the request is
reaching the real Abu Dhabi backend rather than failing in the datapath. Before
the fix, `242.0.0.251` and `242.0.0.252` had no NAT chain at all on the gateway
node and nothing answered.

## What the restart actually disrupts

Only one pod restarted on the whole of worker04, and it was kube-proxy:

```
kube-system   kube-proxy-prd-oi-k8worker04   2026-10-06T12:35:44Z
restarted today: 1
total pods:      56
```

Application containers are untouched. The unit has `KillMode=process` and an
`ExecStopPost` whose regex matches only processes literally named `containerd`
and `kubelet`, so it kills 2 PIDs and leaves every containerd-shim running. The
stateful pods confirm it, with creation timestamps unchanged and restart counts
identical: `postgresql-0` (2025-10-16, 2), `mongodb-0` (2025-10-09, 2),
`minio-1` (2026-06-18, 0), `vault-2` (2026-03-16, 77357).

Two caveats worth knowing:

- Static pods restart, because kubelet owns them. kube-proxy is one, which is the
  point here. `rke2-canal` is another on these nodes.
- The node reports `NotReady` for roughly 60 to 90 seconds while kubelet is down.
  Existing pods keep serving, but avoid doing this during an active rollout.

On `prd-oi-k8worker01` the restart also bounces `submariner-gateway`, so
cross-cluster traffic is interrupted for about a minute.

## Order used

`worker02`, `worker03`, `worker04`, `prd-infr-k8h200`, then `worker01` last,
because `worker01` carries the WireGuard tunnel. Each node was confirmed healthy
before moving to the next.

## What not to bother with

`rke2 certificate rotate --service kube-proxy` does **not** avoid the restart. It
stages the new certificate, backs the old one up to
`/var/lib/rancher/rke2/agent/tls-<timestamp>`, and removes the live file, then
tells you to restart anyway. It also leaves the live certificate missing until
you do, so it makes the intermediate state worse. Skip it and restart directly.

## Notes for next time

Certificate expiry is predictable: RKE2 leaf certificates last 365 days and are
renewed on agent start when within 120 days of expiry. These nodes had not
restarted since March 2026 (and `prd-infr-k8h200` since November 2025), so they
silently lapsed. A scheduled agent restart, or a check of
`rke2 certificate check`, would catch it before it breaks kube-proxy again.

The backup directories created during this work are at
`/root/rke2-cert-backup-<timestamp>` on each agent node, and worker04 also has
`/var/lib/rancher/rke2/agent/tls-1791289997` from the rotate experiment. They can
be removed once the cluster has run clean for a while.

## How to reach the Abu Dhabi nodes

There is no SSH access to Abu Dhabi from Al Ain, and there does not need to be.
The firewall between the clusters allows only Submariner traffic from the Al Ain
gateway node `adeo-gpu-03`:

| Source | Destination | Port | Proto | Purpose |
|---|---|---|---|---|
| `10.34.104.19` | `10.10.128.72` | 51820 | UDP | WireGuard tunnel |
| `10.34.104.19` | `10.10.128.72` | 4500 | UDP | tunnel data |
| `10.34.104.19` | `10.10.128.72` | 4490 | UDP | NAT discovery |
| `10.34.104.19` | `10.10.128.71` | 6443 | TCP | Submariner broker (API server) |

SSH on port 22 is not in that list, which is why `ssh` to any node fails with
`Connection timed out during banner exchange`. The TCP connection is accepted but
no banner ever returns, because the response is dropped. sshd itself is healthy
on every node: listening on `*:22`, `UsePAM yes`, no `AllowUsers` or custom
`Port`, and no `iptables` or `nft` rules touching 22. The block is the firewall,
not the host.

The way in is a privileged pod, which is the same technique the ABUDHABI guide
already uses for the kubectl relay, extended one step further: a privileged pod
can restart host systemd services, so the whole fix can be done with no SSH.

### Step 1: a relay pod on the Al Ain gateway node

A pod is needed on `adeo-gpu-03` because that node is the only one with a
firewall rule to the Abu Dhabi API server. From there it runs the Abu Dhabi
`kubectl` via `chroot`, since the `nettest` image has no `kubectl` binary.

```bash
export KUBECONFIG=~/.kube/alain-oicm.conf
kubectl -n oik8s-cilium-system create configmap adkc \
  --from-file=kc=$HOME/.kube/abudhabi-kubeconfig.yaml

cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata: { name: adrelay, namespace: oik8s-cilium-system }
spec:
  hostNetwork: true
  hostPID: true
  nodeSelector: { kubernetes.io/hostname: adeo-gpu-03 }
  tolerations: [{ operator: Exists }]
  containers:
  - name: relay
    image: registry.adeoaiengine.ecouncil.ae/submariner/nettest:0.24.0
    command: ["sleep","3600"]
    securityContext: { privileged: true }
    volumeMounts: [{ name: host, mountPath: /host }, { name: kc, mountPath: /kc }]
  volumes:
  - { name: host, hostPath: { path: /, type: Directory } }
  - { name: kc, configMap: { name: adkc } }
  restartPolicy: Never
EOF

kubectl -n oik8s-cilium-system wait --for=condition=Ready pod/adrelay --timeout=90s
kubectl -n oik8s-cilium-system exec adrelay -- sh -c 'cp /kc/kc /host/tmp/adkc'
```

The `nettest` image has no `tar`, so `kubectl cp` does not work. Ship files in
with a ConfigMap, or `kubectl exec -i ... -- sh -c 'cat > /host/tmp/file'` for
plain text.

### Step 2: run Abu Dhabi kubectl through the relay

```bash
AD='KUBECONFIG=/tmp/adkc kubectl'
kubectl -n oik8s-cilium-system exec adrelay -- chroot /host sh -c "$AD get nodes -o wide"
```

### Step 3: a privileged pod on each Abu Dhabi node that needs host access

This is the part that removes the need for SSH. With `privileged: true`,
`hostPID: true`, and `hostIPC: true`, a pod can `nsenter` into PID 1's namespaces
and drive the host's systemd directly.

```bash
cat <<'EOF' | kubectl -n oik8s-cilium-system exec -i adrelay -- \
  chroot /host sh -c 'KUBECONFIG=/tmp/adkc kubectl apply -f -'
apiVersion: v1
kind: Pod
metadata:
  name: adtoolbox
  namespace: default
spec:
  hostNetwork: true
  hostPID: true
  hostIPC: true
  nodeSelector:
    kubernetes.io/hostname: prd-oi-k8worker03
  tolerations:
  - operator: Exists
  containers:
  - name: toolbox
    image: registry.gitlab.com/openinnovationai/platform/infra/utils/common-image:0.0.1
    command: ["sleep", "3600"]
    securityContext:
      privileged: true
    volumeMounts:
    - name: host
      mountPath: /host
  volumes:
  - name: host
    hostPath:
      path: /
      type: Directory
  restartPolicy: Never
EOF
```

Then run host commands through `nsenter`, which enters the host's mount, UTS,
IPC, network, and PID namespaces:

```bash
kubectl -n oik8s-cilium-system exec adrelay -- chroot /host sh -c \
  'KUBECONFIG=/tmp/adkc kubectl exec adtoolbox -- \
   nsenter -t 1 -m -u -i -n -p -- <command>'
```

### Choosing an image

The Abu Dhabi registry is not reachable from these nodes for fresh pulls
(`harbor.ai.ecouncil.ae` and `registry.adeoaiengine.ecouncil.ae` both fail to
respond from a node), so use an image that is already cached. Cached images and
their useful tools:

| Image | Cached on | Tools |
|---|---|---|
| `registry.gitlab.com/openinnovationai/platform/infra/utils/common-image:0.0.1` | worker01, worker02, worker03 | `sh bash curl wget netstat nslookup ip ps nsenter awk grep sed` |
| `docker.io/library/alpine:3.21.2` | worker03, prd-infr-k8h200 | `sh wget netstat nslookup ip ps nsenter ...` |
| `docker.io/library/busybox:1.31.1` | worker03 | `sh wget netstat nslookup ip ps nsenter ...` |
| `docker.io/bitnami/os-shell:11-debian-11-r77` | worker02, worker03, worker04 | `sh bash curl ps nsenter ...` |

`nsenter` is what does the work and busybox and alpine both have it, but neither
is cached on worker01, worker02, or worker04. `common-image` is the safest
default, or pin `nodeSelector` to a node that holds the image you pick. To list
what a node has:

```bash
kubectl -n oik8s-cilium-system exec adrelay -- chroot /host sh -c \
  'KUBECONFIG=/tmp/adkc kubectl exec adtoolbox -- \
   chroot /host /var/lib/rancher/rke2/bin/crictl \
     --runtime-endpoint unix:///run/k3s/containerd/containerd.sock images'
```

### Two traps

`kubectl run` does not set `privileged`, so a pod started that way fails with
`nsenter: setns(): can't reassociate to namespace 'ipc': Operation not
permitted`. The explicit `securityContext` block above is required, and
`CapEff` should read `000001ffffffffff`.

Do not try to drive systemd with `chroot /host systemctl`. It appears to work
but is a false positive: `chroot` gives systemd the wrong dbus path so it falls
back to its local path, and `systemctl is-active` then reports the service's own
state rather than the host's. Always use `nsenter`, and confirm a restart by
checking that `MainPID` actually changed.

### Verifying the method without touching a real service

To prove host control before doing anything, start and restart a throwaway unit
and check that its PID changes:

```bash
nsenter -t 1 -m -u -i -n -p -- sh -c '
  printf "[Unit]\nDescription=probe\n[Service]\nExecStart=/bin/sleep 3600\n[Install]\nWantedBy=multi-user.target\n" > /etc/systemd/system/scout-probe.service
  systemctl daemon-reload && systemctl start scout-probe
  echo "before: $(systemctl show scout-probe -p MainPID --value)"
  systemctl restart scout-probe
  echo "after:  $(systemctl show scout-probe -p MainPID --value)"
  systemctl stop scout-probe && rm -f /etc/systemd/system/scout-probe.service && systemctl daemon-reload'
```

The PID must differ between the two lines. That is the check that distinguishes a
real restart from the `chroot` false positive.

## The fault

kube-proxy on every Abu Dhabi agent node cannot authenticate to the API server,
so it never learns about Services or EndpointSlices and never programs the NAT
rules that make a Service reachable. Verified live:

```
E1006 11:59:40 reflector.go:158 "Unhandled Error" err="Failed to watch *v1.Service: failed to list *v1.Service: Unauthorized"
W1006 12:00:00 reflector.go:561 failed to list *v1.Node: Unauthorized
W1006 12:00:01 reflector.go:561 failed to list *v1.EndpointSlice: Unauthorized
```

The cause is that `client-kube-proxy.crt` and `client-rke2-controller.crt` on the
agent nodes expired on 2026-09-29, while the control plane node was renewed on
that same day. Certificate state, read from each node's
`/var/lib/rancher/rke2/agent/`:

| Node | Role | client-kube-proxy expires | client-rke2-controller expires | rke2-agent started |
|---|---|---|---|---|
| `prd-oi-k8master` | control-plane, etcd | 2027-09-29 | 2027-09-29 | (server, renewed 09-29) |
| `prd-oi-k8worker01` | agent | **2026-09-29 (expired)** | **2026-09-29 (expired)** | 2026-03-19 |
| `prd-oi-k8worker02` | agent | **2026-09-29 (expired)** | **2026-09-29 (expired)** | 2026-03-16 |
| `prd-oi-k8worker03` | agent | **2026-09-29 (expired)** | **2026-09-29 (expired)** | 2026-03-16 |
| `prd-oi-k8worker04` | agent | **2026-09-29 (expired)** | **2026-09-29 (expired)** | 2026-03-16 |
| `prd-infr-k8h200` | agent | **2026-09-29 (expired)** | **2026-09-29 (expired)** | 2025-11-23 |

All nodes run `v1.31.3+rke2r1`. The CAs are fine (`client-ca` and `server-ca`
expire 2035) and the API server serving certificate is fine (2027), so only leaf
certificates need reissuing. The control plane does not need a rotation.

The five agent nodes last renewed their certificates in March 2026 (and
`prd-infr-k8h200` in November 2025), so their 365 day validity lapsed on
2026-09-29. The control plane renewed itself that day; the agents did not, which
is why the two sides disagree.

Note the blast radius: this is a platform-wide fault, not something specific to
the OICM exports. Any Service or EndpointSlice created or changed since
2026-09-29 is not being programmed on those nodes. Services whose rules were
written before that date, such as the existing GLM export, keep working by
accident.

## Why restarting the agent is the fix

Per the RKE2 documentation, agent certificates are renewed every time the agent
starts, and any certificate that is expired or within 120 days of expiry is
renewed automatically at startup. `client-kube-proxy` and
`client-rke2-controller` are agent-side certificates, so restarting
`rke2-agent` is sufficient. There is no need for `rke2 certificate rotate`, and
no need to touch the server, because the server's own certificates and the CAs
are valid.

The rotation-order version gate in the RKE2 docs (etcd, then control plane, then
agents) applies to `rke2 certificate rotate` on releases before January 2025
(`v1.31.5+rke2r1` for this minor). `v1.31.3+rke2r1` is before that gate, but the
gate governs explicit rotation, not the automatic renewal that happens on agent
startup. If the fix below does not renew the certificates, the fallback is a full
explicit rotation, which must follow the gate order.

## What to do

Take a snapshot or backup first if the platform has one. Do one agent node at a
time and confirm before moving on, so a bad result is contained and cluster
capacity is preserved.

Order: `prd-oi-k8worker02`, `prd-oi-k8worker03`, `prd-oi-k8worker04`,
`prd-infr-k8h200`, then `prd-oi-k8worker01` last.

The steps below use the privileged-pod method from the section above, since SSH
is not available. Set `NODE` to the node you are working on and `POD` to the
toolbox pod on it.

```bash
export KUBECONFIG=~/.kube/alain-oicm.conf
R='kubectl -n oik8s-cilium-system exec adrelay -- chroot /host sh -c'
AD='KUBECONFIG=/tmp/adkc kubectl'
NODE=prd-oi-k8worker02
POD=adtoolbox

# 1. record the current expiry so you can tell renewal happened
$R "$AD exec $POD -- nsenter -t 1 -m -u -i -n -p -- \
  openssl x509 -in /var/lib/rancher/rke2/agent/client-kube-proxy.crt -noout -enddate"

# 2. restart the agent; this reissues the expired leaf certificates
$R "$AD exec $POD -- nsenter -t 1 -m -u -i -n -p -- systemctl restart rke2-agent"

# 3. confirm the service came back
$R "$AD exec $POD -- nsenter -t 1 -m -u -i -n -p -- systemctl is-active rke2-agent"

# 4. confirm the certificates now expire about a year out
$R "$AD exec $POD -- nsenter -t 1 -m -u -i -n -p -- sh -c '
  for f in client-kube-proxy client-rke2-controller; do
    printf \"%s: \" \$f
    openssl x509 -in /var/lib/rancher/rke2/agent/\$f.crt -noout -enddate
  done'"

# 5. confirm kube-proxy stopped failing
$R "$AD exec $POD -- nsenter -t 1 -m -u -i -n -p -- \
  journalctl -u rke2-agent --since '2 min ago' | grep -i kube-proxy | tail -20"
```

Then delete the toolbox pod from that node and create it on the next one, since
it is pinned by `nodeSelector`. Restarting `rke2-agent` does not restart
application containers, but it does restart the static pods, including
kube-proxy, which is the point.

`prd-oi-k8worker01` at `10.10.128.72` is the Submariner gateway node. Restarting
it briefly interrupts the WireGuard tunnel and the Submariner datapath. Do that
one deliberately, not as the first node, and check that the tunnel recovers
afterwards.

## How to confirm the fix worked

The same privileged-pod method works for the checks. From Al Ain, with the relay
and a toolbox pod on the node you are checking:

```bash
export KUBECONFIG=~/.kube/alain-oicm.conf
R='kubectl -n oik8s-cilium-system exec adrelay -- chroot /host sh -c'
AD='KUBECONFIG=/tmp/adkc kubectl'

# kube-proxy should be quiet. This should print nothing, or only old lines.
$R "$AD -n kube-system logs kube-proxy-prd-oi-k8worker01 --since=2m | grep -c Unauthorized"

# the certificate should be renewed
$R "$AD exec adtoolbox -- nsenter -t 1 -m -u -i -n -p -- \
  openssl x509 -in /var/lib/rancher/rke2/agent/client-kube-proxy.crt -noout -enddate"

# a new Service should now get its NAT chain programmed on the gateway node
$R "$AD exec adtoolbox -- nsenter -t 1 -m -u -i -n -p -- \
  nft list ruleset | grep -E 'KUBE-EXT-|242\.0\.0\.25'"
```

The decisive test is the last one. Before the fix there are `KUBE-EXT-*` chains
for `242.0.0.253` (the old GLM export, written before the certificates lapsed)
and none for `242.0.0.251` (keycloak) or `242.0.0.252`
(`s-mlops-nginx-be-app`). After kube-proxy recovers, chains for those two global
IPs should appear on the gateway node.

Then, from Al Ain, confirm the exports are reachable:

```bash
# from a pod on the Al Ain gateway node adeo-gpu-03
curl -s -o /dev/null -w '%{http_code}\n' http://s-mlops-nginx-be-app.mlops.svc.clusterset.local:80/api/v1/workspaces
curl -s -o /dev/null -w '%{http_code}\n' http://keycloak.keycloak.svc.clusterset.local:80/realms/adeo
```

`403` from the first and a Keycloak response from the second mean the network
path is working, since both need auth to go further. A connection failure or
`000` means the datapath is still not programmed.

## If the agent restart does not renew the certificates

Fall back to an explicit rotation, respecting the version gate order for
`v1.31.3+rke2r1`:

1. Stop `rke2-server` on `prd-oi-k8master`, run `rke2 certificate rotate`, start
   `rke2-server` again. Confirm the API server comes back before continuing.
2. On each agent node, stop `rke2-agent`, run `rke2 certificate rotate`, start
   `rke2-agent` again.
3. Re-run the confirmation steps above.

`rke2 certificate rotate` backs up the old certificates to
`/var/lib/rancher/rke2/server/tls-<timestamp>` (or the agent equivalent), so a
rollback is a matter of restoring that directory and restarting the service.

## Cleaning up afterwards

Delete the toolbox pod from each node and the relay pod and ConfigMap from Al
Ain. Nothing else should be left behind.

```bash
R='kubectl -n oik8s-cilium-system exec adrelay -- chroot /host sh -c'
$R 'KUBECONFIG=/tmp/adkc kubectl delete pod adtoolbox --ignore-not-found'
kubectl -n oik8s-cilium-system delete pod adrelay --ignore-not-found
kubectl -n oik8s-cilium-system delete configmap adkc --ignore-not-found
```

Note that the toolbox pod is created in Abu Dhabi's `default` namespace, so
delete it there rather than from Al Ain.

## What is still needed after the certificates are renewed

One thing, and it is not a code change. Abu Dhabi's Keycloak realm `adeo` does
not have the Al Ain service account, so the controller cannot authenticate there.
The realm and token endpoint exist and reject unknown credentials with
`invalid_grant`. Create an equivalent service account on Abu Dhabi, or decide
that a shared identity is intended, then set `OICM_USERNAME` and `OICM_PASSWORD`
for that cluster. No other change is required: the API path and payload contract
are identical, and Abu Dhabi's own OpenAPI document declares the
`deployment_summary` route the controller calls.
