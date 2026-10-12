# MinIO (Object Storage Backend)

The artifact store and (later) spend-log archival both live in a single MinIO
instance in the `adeo-litellm` namespace. This doc covers the deployment, the
buckets, and the one shared S3 client so nothing is duplicated.

## Why MinIO, and why now

v3 chose a PVC. v4 moves to MinIO for two reasons: it removes the
multi-replica problem entirely (object storage is shared by nature, so the
gateway can scale out without an RWX PVC or baked-in artifacts), and it earns
its keep twice because LiteLLM already ships S3 logging support
(`integrations/s3.py`, `integrations/s3_v2.py`) that currently points at
nothing. One store, two tenants.

## Buckets

| Bucket | Tenant | Contents |
|--------|--------|----------|
| `litellm-client-mirror` | the client-distribution slice | uv artifacts, wheelhouse, Python builds, `manifest.json` |
| `litellm-logs` | LiteLLM's S3 logging callback (later) | spend logs / audit logs, via `s3_callback_params` |

Two buckets, not two MinIO instances. A separate `mc alias` or a separate
bucket path is all the isolation needed; both tenants talk to the same
endpoint with the same client code.

## Deployment

Deployed in `adeo-litellm` alongside the gateway, Redis, and Postgres:

- Single-node MinIO (StatefulSet + PVC) is sufficient for this workload; it is
  internal, low-throughput, and not a system of record for anything the
  cluster cannot regenerate (artifacts can be re-pushed, logs are archival).
- Service `minio.adeo-litellm.svc.cluster.local:9000` (S3 API).
- Credentials in a `litellm-minio-credentials` Secret (`access-key`,
  `secret-key`), referenced by both tenants via `secretKeyRef`. Never in a
  ConfigMap, never committed.
- No ingress for MinIO itself. It is cluster-internal; the gateway is the only
  thing that reaches it, and it reaches it over the cluster network. The
  dev machine never talks to MinIO directly (that is the whole point of the
  gateway proxy).
- A `NetworkPolicy` allowing ingress only from the gateway pod.

Bucket creation: a one-shot Job (or `mc mb`) at bootstrap creates both
buckets and is idempotent (`mc mb --ignore-existing`).

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
  `s3_endpoint_url: os.environ/MINIO_ENDPOINT_URL`, and the credential keys
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
MINIO_ENDPOINT_URL   -> minio.adeo-litellm.svc.cluster.local:9000
MINIO_ACCESS_KEY     -> from litellm-minio-credentials
MINIO_SECRET_KEY     -> from litellm-minio-credentials
```

and the two tenants reference them by name. The slice's helper and the
`s3_callback_params` both read those names, so there is one place to change the
endpoint or credentials.

## What the slice does NOT do

It does not implement object-storage semantics. Put/get/list/delete are
MinIO's, via boto3 (already a litellm dependency, `boto3>=1.43.1`). The slice
only adds the auth boundary (master key), the path containment, the hashing,
and the manifest consistency check — the parts MinIO cannot do for us. That is
the resolution of the v3 conflict: the admin does not point `mc` at MinIO
directly (which would desync the manifest/DTO shape), it calls the slice, and
the slice talks to MinIO.

## Failure behaviour

MinIO down must not take the gateway with it, same rule as every other
dependency here:

- The slice's public reads return `503` (or `404` for a missing object) when
  MinIO is unreachable; the gateway still boots and `/v1` still serves.
- The admin API returns `503`; the admin retries the idempotent push loop.
- The logging tenant: if `s3_callback_params` points at a down MinIO, LiteLLM's
  own callback error handling applies (it is a logging path, not the request
  path), so requests are unaffected.
