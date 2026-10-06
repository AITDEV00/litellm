# Abu Dhabi RKE2 certificate renewal: runbook

Date: 2026-10-06
Purpose: fix the expired RKE2 node certificates that break kube-proxy on the Abu
Dhabi cluster, which is what makes every newly exported Service unreachable over
Submariner.

This is the first and only thing to fix. Nothing else in the OICM work is
blocked on code, and the API-version question is settled (see
`abudhabi-oicm-rest-api-export.md`).

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

Take a snapshot or backup first if the platform has one. Rotate one agent node at
a time and confirm before moving on, so a bad result is contained and cluster
capacity is preserved.

For each of `prd-oi-k8worker01`, `prd-oi-k8worker02`, `prd-oi-k8worker03`,
`prd-oi-k8worker04`, and `prd-infr-k8h200`, in that order:

```bash
# 1. record the current expiry so you can tell renewal happened
openssl x509 -in /var/lib/rancher/rke2/agent/client-kube-proxy.crt -noout -enddate

# 2. restart the agent; this reissues the expired leaf certificates
systemctl restart rke2-agent

# 3. confirm the service came back
systemctl is-active rke2-agent

# 4. confirm the certificate now expires about a year out
openssl x509 -in /var/lib/rancher/rke2/agent/client-kube-proxy.crt -noout -enddate
openssl x509 -in /var/lib/rancher/rke2/agent/client-rke2-controller.crt -noout -enddate

# 5. confirm kube-proxy stopped failing
journalctl -u rke2-agent --since "2 min ago" | grep -i "kube-proxy" | tail -20
```

Restarting `rke2-agent` does not restart application containers, but it does
restart the static pods, including kube-proxy, which is the point.

`prd-oi-k8worker01` at `10.10.128.72` is the Submariner gateway node. Restarting
it briefly interrupts the WireGuard tunnel and the Submariner datapath. Do that
one deliberately, not as the first node, and check that the tunnel recovers
afterwards.

## How to confirm the fix worked

From a shell on any Abu Dhabi node:

```bash
# kube-proxy should be quiet. This should print nothing, or only old lines.
kubectl -n kube-system logs kube-proxy-prd-oi-k8worker01 --since=2m | grep -c Unauthorized

# the certificate should be renewed
openssl x509 -in /var/lib/rancher/rke2/agent/client-kube-proxy.crt -noout -enddate

# a new Service should now get its NAT chain programmed on the gateway node
nft list ruleset | grep -E 'KUBE-EXT-|242\.0\.0\.25'
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

## What is still needed after the certificates are renewed

One thing, and it is not a code change. Abu Dhabi's Keycloak realm `adeo` does
not have the Al Ain service account, so the controller cannot authenticate there.
The realm and token endpoint exist and reject unknown credentials with
`invalid_grant`. Create an equivalent service account on Abu Dhabi, or decide
that a shared identity is intended, then set `OICM_USERNAME` and `OICM_PASSWORD`
for that cluster. No other change is required: the API path and payload contract
are identical, and Abu Dhabi's own OpenAPI document declares the
`deployment_summary` route the controller calls.
