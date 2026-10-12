# Deployment (v2 — gateway slice)

> v2: no artifact pod, no ingress changes. The gateway serves everything.

## Components

1. **Artifact volume.** A PVC holding the `/mirror/*` tree (uv artifacts,
   wheelhouse, Python builds, `manifest.json`).
2. **Gateway pod change.** Mount the volume read-only into the existing
   `litellm-proxy` deployment (one `volumeMounts` + one `volumes` entry in
   `deploy/base/gateway/proxy/deployment.yaml`, next to the existing
   `config`, `hooks`, and `assets` mounts).
3. **Slice wiring.** The two lines in `proxy_server.py` pinned by the
   drop-detection tests (see [architecture.md](architecture.md)). These ride
   the normal image build; no manifest change.
4. **Sync job.** A CronJob (or manual job run from the connected workstation)
   that refreshes the PVC contents. This job runs on a machine/network that
   has internet; the cluster job only copies bytes in, or the workstation
   pushes via `kubectl cp`/rsync-over-port-forward.

Nothing else. No new Deployment, Service, Ingress, or NetworkPolicy.

## Volume mount on the gateway

```yaml
# in deploy/base/gateway/proxy/deployment.yaml, alongside the existing mounts
volumeMounts:
  - name: client-artifacts
    mountPath: /app/client_mirror
    readOnly: true
volumes:
  - name: client-artifacts
    persistentVolumeClaim:
      claimName: litellm-client-artifacts
```

The slice reads its base directory from an env var (e.g.
`LITELLM_CLIENT_MIRROR_DIR`, default `/app/client_mirror`), so dev overlays
can point it at a local directory and prod at the PVC. If the directory is
absent, the slice logs and serves 404s rather than crashing the app — the
gateway must never depend on the artifact store to boot (the same
"proxy never depends on the artifact pod" property, now enforced inside the
app).

PVC sizing from [mirror-content.md](mirror-content.md): ~5 Gi with a few
manifest versions retained for rollback. Access mode `ReadWriteOnce` is
fine while the gateway is single-replica; see the multi-replica caveat in
[architecture.md](architecture.md).

## Sync job

Runs on the connected workstation (this one), not in the cluster: it needs
internet. Steps (idempotent):

1. Read target versions (upstream release to adopt: `litellm`, pinned `uv`,
   pinned Python).
2. Fetch uv release artifacts + `.sha256` sidecars for the
   [platform matrix](platform-matrix.md); verify against the published
   checksums.
3. Resolve the `litellm[cli]` wheel closure for the target Python range and
   collect wheels (method in [mirror-content.md](mirror-content.md)).
4. Fetch the python-build-standalone archives for the pinned CPython.
5. Compute sha256 for everything, write `manifest.json`, and ship the tree
   to the PVC (`kubectl cp` into a helper pod, or rsync via a port-forward).

CI supply-chain rule from the repo guidelines applies to the sync job:
download to file, verify checksum, never `curl | bash`; pin every tool by
full URL and version. The sync script itself lives in `examples/` per the
layer's doc conventions (runnable things are not docs).

## Rollout sequence

1. Phase 0 proof still applies exactly as written in
   [rollout.md](rollout.md) — the containerized `--network none` test of the
   install chain does not involve the cluster at all.
2. Create the PVC, run the sync job, mount the volume on the gateway
   deployment (a normal image rollout includes the slice wiring).
3. Verify from the workstation: `curl -fsSL https://litellm.ecouncil.ae/install`
   returns the generated script with the right base URL baked in;
   `curl -fsSL .../mirror/manifest.json` returns the manifest.
4. Pilot: 2-3 internal users run the full journey (`/install` -> `lite login`
   -> `lite configure claude` -> a day of real agent use).
5. Fix what the pilot surfaces (likely candidates: PATH refresh in current
   shells, keyring on headless hosts, MIME types for `.tar.gz` under
   StaticFiles).
6. Announce; retire hand-rolled per-team setup scripts.

## Rollback

- Unmount the volume and/or comment the two wiring lines -> `/install` and
  `/mirror/*` 404; nothing else in the gateway is affected (the slice is
  additive; the drop tests confirm removal is detectable).
- Installed clients keep working: `lite` talks only to `/v1`; a client whose
  mirror disappears is fully functional, it just cannot update.
- Store rollback: previous manifest retained on the PVC; swap back and the
  next `/install` run pins users to the older version.
