# Deployment (v4 — gateway slice, MinIO backend)

> v4: no artifact pod, no ingress changes, no PVC. Artifacts live in a
> cluster-internal MinIO instance ([minio.md](minio.md)); the gateway proxies
> reads and writes to it.

## Components

1. **MinIO.** One StatefulSet + PVC in `adeo-litellm`, S3 API on
   `minio.adeo-litellm.svc.cluster.local:9000`, two buckets
   (`litellm-client-mirror`, `litellm-logs`). No ingress, `NetworkPolicy`
   allows only the gateway. Full detail in [minio.md](minio.md).
2. **Gateway pod change.** No volume. Just env vars for the MinIO endpoint and
   credentials (`secretKeyRef`), added to the existing Deployment next to the
   other secret-backed env vars.
3. **Slice wiring.** The `proxy_server.py` line pinned by the drop-detection
   test (see [architecture.md](architecture.md)): `app.include_router(...)`.
   Rides the normal image build.
4. **Admin sync API.** The write surface in [admin-sync.md](admin-sync.md),
   which talks to MinIO on the admin's behalf.

No new ingress. No volume mount. No CronJob.

## Gateway env additions

```yaml
# in deploy/base/gateway/proxy/deployment.yaml, alongside existing env
- name: MINIO_ENDPOINT_URL
  value: "http://minio.adeo-litellm.svc.cluster.local:9000"
- name: MINIO_ACCESS_KEY
  valueFrom: {secretKeyRef: {name: litellm-minio-credentials, key: access-key}}
- name: MINIO_SECRET_KEY
  valueFrom: {secretKeyRef: {name: litellm-minio-credentials, key: secret-key}}
```

The slice's `storage.py` helper and the (later) `s3_callback_params` both read
these names, so the endpoint and credentials have one source of truth. See
[minio.md](minio.md) for the DRY rationale.

If MinIO is unreachable, the slice returns `503` on artifact reads and the
admin API, and the gateway still boots and serves `/v1` — the same
"gateway never depends on the artifact store" rule, now with MinIO as the
dependency instead of a volume.

## Filling the store: the admin sync API

The admin runs the idempotent push loop from [admin-sync.md](admin-sync.md)
on a machine with internet access: fetch the pinned uv artifacts, resolve the
wheelhouse, fetch the Python builds, compute hashes, `PUT` each artifact, then
`PUT` the manifest last. The gateway hashes on write and skips unchanged
objects, so re-running is cheap and safe. The gateway writes to the
`litellm-client-mirror` bucket; the admin never touches MinIO directly.

The fetch script itself lives in `examples/` per the layer's doc conventions
(runnable things are not docs), and follows the repo's CI supply-chain rules
(download to file, verify checksum, never `curl | bash`, pin by full URL +
version).

## Rollout sequence

1. Phase 0 proof still applies as written in [rollout.md](rollout.md) — the
   containerized `--network none` test of the install chain does not involve
   the cluster.
2. Deploy MinIO + buckets + `NetworkPolicy` + the credentials Secret.
3. Add the gateway env vars and roll out the image that includes the slice
   wiring.
4. Admin syncs the artifacts via the API.
5. Verify from the workstation:
   `curl -fsSL https://litellm.ecouncil.ae/install` returns the generated
   script; `curl -fsSL .../install/manifest.json` returns the manifest.
6. Pilot: 2-3 internal users run the full journey.
7. Fix what the pilot surfaces; announce; retire hand-rolled setup scripts.

## Rollback

- Remove the gateway env vars and/or the slice wiring line -> `/install/*`
  404s; nothing else in the gateway is affected. The drop test trips on this
  state by design.
- Installed clients keep working: `lite` talks only to `/v1`; a client whose
  mirror disappears is fully functional, it just cannot update.
- Store rollback: publish the previous manifest (the race-guard retention
  keeps it briefly); the next `/install` run pins users to the older version.
- Bucket loss: re-run the admin sync loop; the API's idempotency means no
  partial-state cleanup is needed.

## Scaling note

The multi-replica constraint from v3 is gone. MinIO is shared, so the gateway
can scale out without an RWX PVC or baked-in artifacts. The admin write path
is a single master-key caller, so concurrent gateway replicas reading from
MinIO is the normal object-storage case, not a coordination problem.
