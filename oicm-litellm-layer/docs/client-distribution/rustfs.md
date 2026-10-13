# RustFS (Object Storage Backend)

The artifact store and (later) spend-log archival both live in a single
RustFS instance in the `adeo-litellm` namespace. This doc covers the
deployment, the buckets, and the one shared S3 client so nothing is
duplicated.

## Why RustFS, and why not MinIO

v3 chose a PVC. v4 moved to object storage for two reasons: it removes the
multi-replica problem entirely (object storage is shared by nature, so the
gateway can scale out without an RWX PVC or baked-in artifacts), and it earns
its keep twice because LiteLLM already ships S3 logging support
(`integrations/s3.py`, `integrations/s3_v2.py`) that currently points at
nothing. One store, two tenants.

The backend is **RustFS**, not MinIO. MinIO Community Edition is end of
life: the `minio/minio` repository was archived read-only in April 2026, and
the Docker Hub images were deleted in September 2026 with quay.io closed to
anonymous pulls, so there is no pullable `minio/minio` at any version. RustFS
is an Apache-2.0, actively maintained, S3-compatible object store written in
Rust, and S3 compatibility is the only contract anything in this design
depends on: LiteLLM's logging path builds a stock
`boto3.client("s3", endpoint_url=...)` (`litellm/integrations/s3.py:74`) and
the v2 path hand-rolls SigV4 over raw HTTP against `{endpoint}/{bucket}/{key}`.
Neither has any MinIO-specific code, so the swap is an endpoint and
credential change, not a design change.

The pinned image, mirrored into the internal Harbor (public `rustfs`
project):

```
registry.adeoaiengine.ecouncil.ae/rustfs/rustfs:1.0.1
sha256:38fcb422c93174dada595d1aedec0e942a16a2a01a0ecca896c4a563e1f463b5
```

## Buckets

| Bucket | Tenant | Contents |
|--------|--------|----------|
| `litellm-client-mirror` | the client-distribution slice | uv artifacts, wheelhouse, Python builds, `manifest.json` |
| `litellm-logs` | LiteLLM's S3 logging callback (later) | spend logs / audit logs, via `s3_callback_params` |

Two buckets, not two RustFS instances. A separate `mc alias` or a separate
bucket path is all the isolation needed; both tenants talk to the same
endpoint with the same client code.

## Deployment

Deployed in `adeo-litellm` alongside the gateway, Redis, and Postgres:

- Single-node RustFS (StatefulSet + PVC) is sufficient for this workload; it
  is internal, low-throughput, and not a system of record for anything the
  cluster cannot regenerate (artifacts can be re-pushed, logs are archival).
- Service `rustfs.adeo-litellm.svc.cluster.local:9000` (S3 API).
- Credentials in a `litellm-rustfs-credentials` Secret (`access-key`,
  `secret-key`), referenced by both tenants via `secretKeyRef`. Never in a
  ConfigMap, never committed. RustFS reads them as `RUSTFS_ACCESS_KEY` /
  `RUSTFS_SECRET_KEY`.
- No ingress for RustFS itself. It is cluster-internal; the gateway is the
  only thing that reaches it, and it reaches it over the cluster network.
  The dev machine never talks to RustFS directly (that is the whole point
  of the gateway proxy).
- A `NetworkPolicy` allowing ingress only from the gateway pods (prod and
  dev labels) plus the bootstrap Job.
- Runtime contract, verified by running the pinned image: non-root
  `10001:10001`, `readOnlyRootFilesystem: true` with only `/data` mounted,
  no writable `/tmp` needed, console disabled, probes `/health` (liveness)
  and `/health/ready` (readiness), and `RUSTFS_OBS_LOG_DIRECTORY` set
  explicitly empty because the image bakes `RUSTFS_OBS_LOG_DIRECTORY=/logs`
  and telemetry initialization fails fatally when it cannot create it.
- The data PVC uses the existing `longhorn-crypto-global` StorageClass
  (encrypted, Delete reclaim). No `StorageClass` is created by this
  deployment; the class is referenced by name. It was chosen over the cluster
  default `longhorn-crypto-retain` so a deleted StatefulSet does not leak its
  volume.
- The manifest is `deploy/base/shared/rustfs.yaml`, shared by both overlays
  unsuffixed (one store, both gateways read it), and is the single write
  path: nothing about the store is created by hand. See the idempotency notes
  in [implementation.md](implementation.md) Phase 1 for the re-apply
  semantics, including the Job-template immutability caveat.

Bucket creation: a one-shot Job at bootstrap creates both buckets with the
`mc` client (Harbor's
`registry.adeoaiengine.ecouncil.ae/minio/mc:latest`,
`sha256:512a1cb685940ded1b0ff55470a226a3830e16305a3e0f1bfbb77056171601fd`),
which works unchanged against RustFS. RustFS has no
`MINIO_DEFAULT_BUCKETS`-style startup hook, so the Job is required. It is
idempotent: `mc mb --ignore-existing` succeeds on an existing bucket, and
RustFS's own duplicate-bucket response is success rather than error. The Job
pins `runAsUser: 65532` (the `mc` image runs as root by default, which would
trip `runAsNonRoot`) and carries `ttlSecondsAfterFinished: 3600` so a
completed run reaps itself, matching the repo's other Jobs.

## The DRY S3 client

Both tenants use S3. Rather than two hand-rolled boto3 clients, there is **one**
shared client factory, and both the logging callback and the slice read their
parameters through it.

The upstream logging path already does exactly this pattern:
`litellm/integrations/s3.py` builds its client from `litellm.s3_callback_params`
(reading `s3_endpoint_url`, `s3_bucket_name`, the credential keys, and
`os.environ/...` indirection for secrets — verified in the source). We reuse
that, not replace it:

- **Logging tenant:** config only, no code. Add `s3_callback_params` to the
  gateway's `litellm_settings` with `s3_bucket_name: litellm-logs`,
  `s3_endpoint_url: os.environ/RUSTFS_ENDPOINT_URL`, and the credential keys
  pointing at the Secret-backed env vars. LiteLLM's existing `S3Logger` does
  the rest. This is the "add that file syncing logic in a DRY manner" answer
  for logs: it is already written upstream, we just configure it.
- **Mirror tenant:** the slice's admin API writes objects and the public route
  reads them. It uses the same `s3_endpoint_url` / bucket / credential
  parameters, sourced from the same env vars, through a thin shared helper so
  the slice does not re-declare them. If the helper and the callback ever
  disagree, the shared env var is the single source of truth.

Concretely, one set of env vars in the gateway Deployment feeds both:

```
RUSTFS_ENDPOINT_URL   -> http://rustfs.adeo-litellm.svc.cluster.local:9000
RUSTFS_ACCESS_KEY     -> from litellm-rustfs-credentials
RUSTFS_SECRET_KEY     -> from litellm-rustfs-credentials
```

and the two tenants reference them by name. The slice's helper and the
`s3_callback_params` both read those names, so there is one place to change the
endpoint or credentials.

## What the slice does NOT do

It does not implement object-storage semantics. Put/get/list/delete are
RustFS's, via boto3 (already a litellm dependency, `boto3>=1.43.1`), using
SigV4 with path-style addressing. The slice only adds the auth boundary
(master key), the path containment, the hashing, and the manifest consistency
check — the parts RustFS cannot do for us. That is the resolution of the v3
conflict: the admin does not point `mc` at RustFS directly (which would desync
the manifest/DTO shape), it calls the slice, and the slice talks to RustFS.

## Failure behaviour

RustFS down must not take the gateway with it, same rule as every other
dependency here:

- The slice's public reads return `503` (or `404` for a missing object) when
  RustFS is unreachable; the gateway still boots and `/v1` still serves.
- The admin API returns `503`; the admin retries the idempotent push loop.
- The logging tenant: if `s3_callback_params` points at a down RustFS,
  LiteLLM's own callback error handling applies (it is a logging path, not
  the request path), so requests are unaffected.
