# Deployment (v3 — gateway slice, PVC, admin sync API)

> v3: no artifact pod, no ingress changes, no MinIO. Storage is a PVC filled
> by an admin-facing REST API on the gateway (see [admin-sync.md](admin-sync.md)).

## Components

1. **Artifact PVC.** A PersistentVolumeClaim holding the `/mirror/*` tree
   (uv artifacts, wheelhouse, Python builds, `manifest.json`).
2. **Gateway pod change.** Mount the PVC into the existing `litellm-proxy`
   deployment (one `volumeMounts` + one `volumes` entry in
   `deploy/base/gateway/proxy/deployment.yaml`, next to the existing
   `config`, `hooks`, and `assets` mounts).
3. **Slice wiring.** The `proxy_server.py` lines pinned by the drop-detection
   tests (see [architecture.md](architecture.md)): the public `/install`
   router, the public `/mirror` static mount, and the admin
   `/client-mirror/v1` router. These ride the normal image build.
4. **Admin sync API.** The write surface in [admin-sync.md](admin-sync.md).
   The admin pushes artifacts and the manifest over REST with the master key.

Nothing else. No new Deployment, Service, Ingress, or NetworkPolicy. No
MinIO, no S3, no CronJob.

## Volume mount on the gateway

```yaml
# in deploy/base/gateway/proxy/deployment.yaml, alongside the existing mounts
volumeMounts:
  - name: client-artifacts
    mountPath: /app/client_mirror
    # NOT readOnly: the admin sync API writes here. The public /mirror mount
    # serves reads from the same directory.
volumes:
  - name: client-artifacts
    persistentVolumeClaim:
      claimName: litellm-client-artifacts
```

Writable, unlike the earlier draft: the admin sync API is a write path, so the
volume cannot be `readOnly`. This is a deliberate change from v2 and is the
one place where the gateway now writes to a mounted volume. The slice only
ever writes under the mirror root (path containment enforced, see
[admin-sync.md](admin-sync.md)).

The slice reads its base directory from `LITELLM_CLIENT_MIRROR_DIR` (default
`/app/client_mirror`), so dev overlays can point it at a local directory. If
the directory is absent, the slice logs and serves 404s rather than crashing
the app — the gateway must never depend on the artifact store to boot.

PVC sizing from [mirror-content.md](mirror-content.md): ~5 Gi with a few
manifest versions retained for rollback. Access mode `ReadWriteOnce` is fine
while the gateway is single-replica; see the multi-replica caveat in
[architecture.md](architecture.md).

## Filling the PVC: the admin sync API

The admin runs the idempotent push loop from [admin-sync.md](admin-sync.md)
on a machine with internet access: fetch the pinned uv artifacts, resolve the
wheelhouse, fetch the Python builds, compute hashes, `PUT` each artifact,
then `PUT` the manifest last. The gateway does the hashing on write and skips
unchanged objects, so re-running is cheap and safe.

This replaces the earlier "sync CronJob" and "`kubectl cp`" approaches. The
job that fetches from the internet can still be a script the admin runs, but
its output is delivered via the API, not by cluster copy. The fetch script
itself lives in `examples/` per the layer's doc conventions (runnable things
are not docs), and follows the repo's CI supply-chain rules (download to
file, verify checksum, never `curl | bash`, pin by full URL + version).

## Rollout sequence

1. Phase 0 proof still applies exactly as written in
   [rollout.md](rollout.md) — the containerized `--network none` test of the
   install chain does not involve the cluster at all.
2. Create the PVC, mount it on the gateway deployment, roll out the image
   that includes the slice wiring.
3. Admin syncs the artifacts via the API
   (`curl ... /client-mirror/v1/artifact/...`, then the manifest).
4. Verify from the workstation: `curl -fsSL https://litellm.ecouncil.ae/install`
   returns the generated script with the right base URL;
   `curl -fsSL .../mirror/manifest.json` returns the manifest.
5. Pilot: 2-3 internal users run the full journey (`/install` -> `lite login`
   -> `lite configure claude` -> a day of real agent use).
6. Fix what the pilot surfaces (likely: PATH refresh in current shells,
   keyring on headless hosts, MIME types for `.tar.gz` under StaticFiles).
7. Announce; retire hand-rolled per-team setup scripts.

## Rollback

- Unmount the PVC and/or comment the wiring lines -> `/install`, `/mirror/*`,
  and the admin API all 404; nothing else in the gateway is affected. The
  drop-detection tests intentionally fail in this state (the tripwire).
- Installed clients keep working: `lite` talks only to `/v1`; a client whose
  mirror disappears is fully functional, it just cannot update.
- Store rollback: previous manifest retained on the PVC; swap back and the
  next `/install` run pins users to the older version.
- Accidental PVC deletion: recreate the PVC, re-run the admin sync loop; the
  API's idempotency means no partial-state cleanup is needed.
