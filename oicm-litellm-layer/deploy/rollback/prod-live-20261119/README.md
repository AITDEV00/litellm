# Prod rollback: 2026-11-19 running state

Restores the `adeo-litellm` namespace to exactly what it was running before the
MCP source broker, digest-pinned images, and ingress buffering change went out.

## The single command

```bash
bash oicm-litellm-layer/deploy/rollback/prod-live-20261119/rollback.sh
```

Run it from the repo root with `KUBECONFIG` pointing at the Al Ain cluster
(`~/.kube/alain-oicm.conf`). It takes roughly one to two minutes and returns only
after the workloads report a completed rollout.

## What it does

1. Deletes the objects the new rollout creates and that do not exist in the
   captured state: `Deployment/litellm-mcp-broker`, `Service/litellm-mcp-broker`,
   `NetworkPolicy/litellm-mcp-broker`, `NetworkPolicy/litellm-rustfs-source-broker`.
   Leaving them would strand a broker whose Service and policies point at nothing.
2. Applies the nine captured objects: gateway and controller Deployments, the
   rustfs StatefulSet, both Services, the Ingress, and the three ConfigMaps.
3. Waits for all three rollouts, then prints the running gateway image IDs so you
   can confirm the bytes.

It is idempotent. Re-running applies unchanged objects as no-ops and tolerates the
deletes already having happened.

## Why the snapshot is trustworthy

Every object was read from the live cluster with `kubectl get -o yaml` at
2026-11-19T11:24Z, not reconstructed from manifests in the repo. The checked-in
manifests had drifted from reality in two ways that a manifest-derived rollback
would have silently reintroduced:

- `deploy/rollback/gateway.yaml` pinned `litellm-src:jya0-v1.106.6`, but prod was
  running `jya0-v1.108.5`. Rolling back to the old file would have downgraded the
  gateway by two minor versions.
- The controller in the old rollback file was `7a1e97a`, which matched the live
  one, so that part was fine, but the gateway was not.

## Images are pinned to digests

The captured Deployments carried the mutable tag `litellm-src:jya0-v1.108.5`, and
at capture time two live pods were running **two different digests** under that
tag:

| digest | state |
|---|---|
| `sha256:3b2b536335...` | what the registry tag resolves to, and what a fresh pull gets |
| `sha256:b1f24865...` | a stale survivor from the previous rollout |

The snapshot pins `3b2b536335`, because that is what the tag resolves to and what
a redeploy today produces. Running the rollback also converged the two pods onto
that one digest, which is the correct end state.

Pinned references:

```text
litellm-src:jya0-v1.108.5@sha256:3b2b536335efac64e73311eb484e7f038bc2c8dfdf2ed84022c4ae7e10493eaf
oicm-discovery-controller:0.1.0-20261111-7a1e97a@sha256:eeaa84b14fa920771122adf7a2ef8107ed358e12d9e3fc71b8cab8525fb3e8da
rustfs/rustfs:1.0.1@sha256:38fcb422c93174dada595d1aedec0e942a16a2a01a0ecca896c4a563e1f463b5
```

## What it does not touch

Secrets are deliberately excluded. The rollout adds
`litellm-mcp-source-broker` and changes `litellm-master-key` and the rustfs
credentials only in that the new manifests reference them; the Secret contents are
not part of the deploy diff. Restoring a Secret from a snapshot would be a
credential rollback, which is almost never what you want during an incident. The
`litellm-mcp-source-broker` Secret can be left in place; nothing reads it once the
broker Deployment is gone.

Also untouched: the CNPG `Cluster` CRs, Postgres Services, PVCs, the janitor
CronJobs, the ServiceMonitor, TLS secrets, and every `-dev` object.

## Verified

The command was executed against the live cluster on 2026-11-19 as a test. Result:
both gateway replicas on `3b2b536335`, `Deployment` and `StatefulSet` rollouts
complete, two ready endpoints, and `/health/liveliness` returning 200 both through
`https://litellm.ecouncil.ae` and through the Service.

## The ingress caveat

The Ingress in this snapshot carries
`nginx.ingress.kubernetes.io/proxy-max-temp-file-size: "0"`, the fix from the same
change set. A rollback reverts the broker and the image pins but keeps the
unbuffered ingress, because the buffering fix is independent of the broker work and
reverting it would reintroduce the latency defect in
`docs/incidents/2026-11-18-nemotron-gateway-transcription-latency/`. If you ever
need the old buffering behavior back, edit that file explicitly; do not expect this
rollback to do it.
